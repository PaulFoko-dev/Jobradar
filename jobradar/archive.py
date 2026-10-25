"""Archives locales des candidatures : un dossier de fichiers ordinaires par candidature.

Les fichiers font foi : ils restent lisibles sans JobRadar et ``candidature.json`` permet de
reconstruire le suivi. La base ne garde que le nom du dossier, l'état de la capture et
l'instantané figé de l'offre. Aucune fonction de ce module ne supprime un dossier : retirer une
candidature du suivi la déplace dans ``_retirees/``.

La capture de la page d'origine passe par une file unique (un fil de travail, une capture à la
fois). Son état fait foi en base (``pending`` → ``running`` → ``done`` | ``failed``) : une
capture restée en attente ou interrompue est relancée au démarrage suivant du serveur.
"""
from __future__ import annotations

import base64
import binascii
import importlib
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from . import config, db, workflow

LOGGER = logging.getLogger(__name__)

RETIRED = "_retirees"
MAX_NAME_LENGTH = 80
MAX_COMPANY_LENGTH = 30
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
CAPTURE_TIMEOUT_S = 45
BROWSER_STATUS_TTL_S = 300
FINAL_STATUSES = frozenset({"retenu", "refuse", "abandon"})
STATUS_LABELS = {
    "consultee": "Offre consultée",
    "envoyee": "Candidature envoyée",
    "relance": "Relancée",
    "entretien_rh": "Entretien RH",
    "entretien_tech": "Entretien technique",
    "retenu": "Retenu",
    "refuse": "Refusé",
    "abandon": "Abandonnée",
}
# Fidélité des captures : une recapture moins fidèle ne remplace jamais une copie existante.
CAPTURE_RANK = {"indisponible": 0, "reconstituee": 1, "page_navigateur_dedie": 2, "page": 3}
CAPTURE_LABELS = {
    "page": "copie de la page d'origine",
    "page_navigateur_dedie": "copie de la page d'origine (navigateur dédié)",
    "reconstituee": "copie reconstituée à partir du texte enregistré",
    "indisponible": "aucune copie PDF (le texte complet reste dans offre/offre.md)",
}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
_TRANSLITERATION = str.maketrans({"æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ø": "o", "Ø": "O",
                                  "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "’": "-", "'": "-"})
_KINDS = {".pdf": "pdf", ".docx": "docx", ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".md": "md",
          ".json": "json", ".ics": "ics", ".txt": "txt"}
_SIGNATURES = ((b"%PDF-", ".pdf"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))
_ICS_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_entretien-\d+\.ics")
DOCUMENTS_README = (
    "Ce dossier accueillera les versions du CV et de la lettre de motivation préparées pour cette\n"
    "candidature, ainsi qu'une copie exacte des documents réellement envoyés.\n"
)
# Écritures concurrentes d'un même processus (requêtes et fil de capture) : INDEX.md reste cohérent.
_FILES_LOCK = threading.RLock()


class UnsafePath(ValueError):
    """Chemin refusé : il sortirait du dossier de la candidature ou suivrait un lien symbolique."""


class InvalidAttachment(ValueError):
    """Pièce jointe refusée : type, signature ou taille."""


class ExplorerUnavailable(RuntimeError):
    """Aucun moyen d'ouvrir un dossier dans l'explorateur de fichiers."""


def archive_root() -> Path:
    return config.archive_dir()


# ---------------------------------------------------------------- noms et écritures


def _ascii(value: object) -> str:
    text = str(value or "").translate(_TRANSLITERATION)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slug(value: object, fallback: str, limit: int = MAX_NAME_LENGTH) -> str:
    """Mots ASCII séparés par « - » : aucun caractère interdit sous Windows, Linux ou macOS."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", _ascii(value)).strip("-")
    return text[:limit].rstrip("-") or fallback


def portable_name(name: str) -> str:
    """Évite les noms réservés de Windows (``CON``, ``NUL.txt``…) et les points ou espaces finaux."""
    name = name.rstrip(" .") or "_"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def folder_name(day: str, company: object, title: object, taken: set[str]) -> str:
    """``AAAA-MM-JJ_Entreprise_Intitule``, 80 caractères au plus, suffixe ``-2``, ``-3``… si déjà pris.

    ``taken`` contient les noms existants en minuscules (systèmes de fichiers insensibles à la casse).
    """
    base = f"{day}_{slug(company, 'Entreprise', MAX_COMPANY_LENGTH)}_{slug(title, 'Poste')}"
    base = portable_name(base[:MAX_NAME_LENGTH].rstrip("-_"))
    name, number = base, 2
    while name.casefold() in taken:
        suffix = f"-{number}"
        name = base[:MAX_NAME_LENGTH - len(suffix)].rstrip("-_") + suffix
        number += 1
    return name


def write_bytes(path: Path, content: bytes) -> None:
    """Écriture atomique : fichier temporaire du même dossier, puis ``os.replace``."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_text(path: Path, content: str) -> None:
    write_bytes(path, content.encode("utf-8"))


def write_json(path: Path, content: Any) -> None:
    write_text(path, json.dumps(content, ensure_ascii=False, indent=2) + "\n")


def _child(root: Path, name: object) -> Path:
    """Sous-dossier direct de ``root`` ; tout nom composé, absolu ou lien symbolique est refusé."""
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise UnsafePath("Nom de dossier d'archive invalide.")
    path = root / name
    if path.is_symlink():
        raise UnsafePath("Les liens symboliques sont refusés.")
    return path


# ---------------------------------------------------------------- contenu des fichiers


def _text(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _zone():
    return workflow.validate_timezone(config.settings().timezone)


def _local(value: str | None) -> str:
    """Instant ISO stocké → « AAAA-MM-JJ HH:MM » dans le fuseau métier."""
    instant = db.parse_utc(value)
    return instant.astimezone(_zone()).strftime("%Y-%m-%d %H:%M") if instant else (_text(value) or "—")


def _hide_paths(text: str) -> str:
    """Les fichiers Markdown se partagent : aucun chemin de la machine n'y figure."""
    for path in sorted({str(archive_root()), str(config.DATA_DIR), str(Path.home())}, key=len, reverse=True):
        if len(path) > 1:
            text = text.replace(path, "…")
    return text


def _bullets(values: object) -> str:
    items = [_text(v) for v in values if _text(v)] if isinstance(values, list) else []
    return "\n".join(f"- {item}" for item in items) or "- Non renseigné"


def _links(snapshot: dict) -> list[str]:
    lines = [f"- Offre : {snapshot['url']}"] if snapshot.get("url") else []
    for link in snapshot.get("links") or []:
        if isinstance(link, dict) and link.get("url") and link["url"] != snapshot.get("url"):
            label = "Candidater" if link.get("apply") else _text(link.get("source")) or "Lien"
            lines.append(f"- {label} : {link['url']}")
    return lines


def offer_snapshot(row: dict) -> dict:
    """Instantané de l'offre et de son analyse au moment de l'entrée dans le suivi (figé ensuite)."""
    offer = row.get("offer")
    if offer:
        snapshot = {key: offer.get(key) for key in
                    ("title", "company", "location", "contract", "date_posted", "description", "url", "score")}
        snapshot["application_deadline_date"] = offer.get("application_deadline_date")
        snapshot["links"] = json.loads(offer.get("links_json") or "[]")
        snapshot["analysis"] = json.loads(offer["analysis_json"]) if offer.get("analysis_json") else None
    else:
        snapshot = {"title": row["title"], "company": row["company"], "location": row["location"],
                    "contract": None, "date_posted": None, "description": "", "url": row["url"],
                    "score": row["score"], "links": [], "analysis": None}
    return snapshot | {"taken_at_utc": db.utc_iso(db.utcnow(), "taken_at")}


def render_offer(snapshot: dict) -> str:
    """``offre.md`` : texte complet et analyse JobRadar, toujours écrit quel que soit le niveau de capture."""
    analysis = snapshot.get("analysis") if isinstance(snapshot.get("analysis"), dict) else {}
    score = analysis.get("score", snapshot.get("score"))
    parts = [
        f"# {_text(snapshot.get('title')) or 'Offre'}",
        "\n".join([
            f"- **Entreprise** : {_text(snapshot.get('company')) or 'Non renseignée'}",
            f"- **Lieu** : {_text(snapshot.get('location')) or 'Non renseigné'}",
            f"- **Contrat** : {_text(snapshot.get('contract')) or 'Non renseigné'}",
            f"- **Publiée le** : {_text(snapshot.get('date_posted')) or 'Non renseigné'}",
            f"- **Date limite de candidature** : {_text(snapshot.get('application_deadline_date')) or 'Non indiquée'}",
            f"- **Entrée dans le suivi** : {_local(snapshot.get('taken_at_utc'))}",
        ]),
        "## Liens", "\n".join(_links(snapshot)) or "- Aucun lien enregistré",
        "## Description complète", _text(snapshot.get("description")) or "Description non disponible.",
        "## Analyse JobRadar",
    ]
    if not analysis:
        return "\n\n".join(parts + ["Aucune analyse enregistrée pour cette offre."]) + "\n"
    parts += [
        "\n".join([
            f"- **Score** : {score if score is not None else 'Non renseigné'}",
            f"- **Verdict** : {_text(analysis.get('verdict')) or 'Non renseigné'}",
            f"- **Résumé** : {_text(analysis.get('summary')) or 'Non renseigné'}",
        ]),
        "### Missions", _bullets(analysis.get("missions")),
        "### Profil recherché", _bullets(analysis.get("profile")),
        "### Mots-clés pour le CV", _bullets(analysis.get("cv_keywords")),
        "### Conseils pour le CV et la lettre", _bullets(analysis.get("cv_tips")),
        "### Points d'attention", _bullets(analysis.get("red_flags")),
    ]
    return "\n\n".join(parts) + "\n"


def _capture_line(app: dict) -> str:
    status, level = app.get("capture_status"), app.get("capture_level")
    if status in (None, "pending", "running"):
        return "capture en cours (le texte complet est déjà dans offre/offre.md)"
    label = CAPTURE_LABELS.get(level or "", "capture impossible (le texte complet reste dans offre/offre.md)")
    return f"{label}, le {_local(app.get('captured_at_utc'))}"


def _online_line(app: dict) -> str:
    if not app.get("offer_checked_at_utc"):
        return "non vérifiée"
    state = {True: "en ligne", False: "retirée (la copie archivée prend le relais)"}.get(
        app.get("offer_online"), "état inconnu (site injoignable ou protégé)")
    return f"{state}, vérifiée le {_local(app['offer_checked_at_utc'])}"


def render_readme(app: dict, snapshot: dict, interviews: list[dict], documents: list[dict] | None = None) -> str:
    """``LISEZMOI.md`` : la fiche vivante de la candidature."""
    history = [f"- {_local(h.get('at'))} — {STATUS_LABELS.get(h.get('status'), h.get('status'))}"
               for h in app.get("history") or [] if isinstance(h, dict)]
    meetings = [f"- {i['starts_at'][:16].replace('T', ' ')} — {_text(i['title'])}"
                + (" (annulé)" if i["status"] == "cancelled" else "") for i in interviews]
    document_lines = []
    for document in documents or []:
        label = "CV" if document.get("kind") == "cv" else "Lettre"
        status = {"brouillon": "brouillon", "pret": "prêt", "envoye": "envoyé"}.get(
            document.get("status"), _text(document.get("status")))
        files = ", ".join(f"`{item.get('name')}`" for item in document.get("files") or [])
        document_lines.append(f"- {label} v{document.get('version')} — {status}" + (f" — {files}" if files else ""))
    lines = [
        f"- **Entreprise** : {_text(app.get('company')) or 'Non renseignée'}",
        f"- **Poste** : {_text(app.get('title'))}",
        f"- **Lieu** : {_text(app.get('location')) or 'Non renseigné'}",
        f"- **Statut** : {STATUS_LABELS.get(app['status'], app['status'])}",
        f"- **Score JobRadar** : {app['score'] if app.get('score') is not None else 'Non renseigné'}",
        f"- **Entrée dans le suivi** : {_local(app.get('created_at'))}",
        f"- **Candidature envoyée le** : {_local(app['applied_at_utc']) if app.get('applied_at_utc') else 'Non envoyée'}",
        f"- **Dernière mise à jour** : {_local(app.get('updated_at'))}",
        f"- **Lien de candidature** : {_text(app.get('url')) or 'Non renseigné'}",
        f"- **Annonce** : {_online_line(app)}",
        f"- **Copie de l'offre** : {_capture_line(app)}",
    ]
    if app.get("capture_error") and app.get("capture_status") in ("done", "failed"):
        lines.append(f"- **Détail de la capture** : {_hide_paths(_text(app['capture_error']))}")
    parts = [
        f"# {_text(app.get('title'))} — {_text(app.get('company')) or 'Entreprise non renseignée'}",
        "\n".join(lines),
        "## Liens de l'offre", "\n".join(_links(snapshot)) or "- Aucun lien enregistré",
        "## Historique", "\n".join(history) or "- Aucun changement de statut",
        "## Entretiens", "\n".join(meetings) or "- Aucun entretien planifié",
        "## Documents de candidature", "\n".join(document_lines) or "- Aucun document préparé",
        "## Notes", _text(app.get("notes")) or "Aucune note.",
        "## Contenu du dossier",
        "- `offre/` : texte complet et analyse (`offre.md`), données brutes (`offre.json`), copie PDF et "
        "capture d'écran de la page quand elles existent, captures ajoutées à la main\n"
        "- `documents/` : CV et lettres préparés pour cette candidature\n"
        "- `envoi/` : traces de l'envoi\n"
        "- `entretien/` : invitations d'agenda (`.ics`) des entretiens\n"
        "- `candidature.json` : données structurées (permet de reconstruire le suivi dans JobRadar)",
    ]
    return "\n\n".join(parts) + "\n"


_DOCUMENT_FIELDS = ("archive_id", "archive_dir", "title", "company", "location", "url", "score", "status", "notes",
                    "history", "created_at", "updated_at", "applied_at_utc", "capture_status", "capture_level",
                    "capture_error", "captured_at_utc", "offer_online", "offer_checked_at_utc")


def archive_document(app: dict, snapshot: dict, interviews: list[dict]) -> dict:
    """``candidature.json`` : de quoi reconstruire la candidature dans une base vide."""
    return {
        "format": "jobradar-candidature",
        "version": 1,
        "archive_id": app["archive_id"],
        "application": {key: app.get(key) for key in _DOCUMENT_FIELDS},
        "interviews": interviews,
        "offer_snapshot": snapshot,
    }


def regenerate_index(root: Path) -> None:
    """``INDEX.md`` : récapitulatif reconstruit depuis les ``candidature.json`` présents."""
    rows = []
    with _FILES_LOCK:
        for directory in _archive_dirs(root):
            try:
                app = read_document(directory)["application"]
            except ValueError:
                continue
            cells = [_local(app.get("created_at"))[:10], _text(app.get("company")), _text(app.get("title")),
                     STATUS_LABELS.get(app.get("status"), _text(app.get("status")))]
            cells = [c.replace("|", "\\|").replace("\n", " ") for c in cells]
            rows.append((*cells, f"[Ouvrir]({quote(directory.name)}/LISEZMOI.md)"))
        rows.sort(key=lambda row: (row[0], row[1].casefold()), reverse=True)
        lines = ["# Candidatures JobRadar", "", f"{len(rows)} candidature(s) suivie(s). Les candidatures retirées "
                 f"du suivi sont conservées dans `{RETIRED}/`.", "",
                 "| Date | Entreprise | Poste | Statut | Fiche |", "|---|---|---|---|---|"]
        lines += [f"| {' | '.join(row)} |" for row in rows]
        write_text(root / "INDEX.md", "\n".join(lines) + "\n")


def _archive_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.is_symlink() and p.name != RETIRED and not p.name.startswith("."))


def read_document(directory: Path) -> dict:
    try:
        document = json.loads((directory / "candidature.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"candidature.json illisible dans {directory.name}.") from exc
    if not isinstance(document, dict) or not isinstance(document.get("application"), dict):
        raise ValueError(f"candidature.json invalide dans {directory.name}.")
    return document


# ---------------------------------------------------------------- cycle de vie


def ensure_archive(app_id: int) -> bool:
    """Crée le dossier d'une candidature qui n'en a pas ; faux si elle en a déjà un.

    La création est réservée en base avant toute écriture : le serveur et ``cli refresh``
    lancés en même temps ne créent jamais deux dossiers pour la même candidature.
    """
    row = db.archive_row(app_id)
    if row["archive_dir"]:
        return False
    archive_id = str(uuid.uuid4())
    if not db.claim_archive(app_id, row["archive_id"], archive_id):
        return False
    root = archive_root()
    snapshot = offer_snapshot(row)
    with _FILES_LOCK:
        directory = _new_directory(root, row)
        (directory / "offre").mkdir()
        for name in ("documents", "envoi", "entretien"):
            (directory / name).mkdir()
        write_text(directory / "documents" / "README.txt", DOCUMENTS_README)
        write_json(directory / "offre" / "offre.json", snapshot)
        write_text(directory / "offre" / "offre.md", render_offer(snapshot))
        db.set_archive_fields(app_id, {"archive_dir": directory.name, "capture_status": "pending",
                                       "offer_snapshot_json": json.dumps(snapshot, ensure_ascii=False)})
        _refresh(app_id)
    return True


def _new_directory(root: Path, row: dict) -> Path:
    (root / RETIRED).mkdir(parents=True, exist_ok=True)
    created = db.parse_utc(row["created_at"]) or db.utcnow()
    day = created.astimezone(_zone()).date().isoformat()
    while True:
        taken = {p.name.casefold() for p in root.iterdir()}
        directory = root / folder_name(day, row["company"], row["title"], taken)
        try:
            directory.mkdir()
            return directory
        except FileExistsError:  # créé entre-temps par un autre processus
            continue


def refresh(app_id: int) -> None:
    """Régénère LISEZMOI.md, candidature.json, les .ics et INDEX.md ; un échec est journalisé, jamais propagé."""
    try:
        with _FILES_LOCK:
            _refresh(app_id)
    except Exception:
        LOGGER.exception("Archive de la candidature %s non mise à jour", app_id)


def _refresh(app_id: int) -> None:
    row = db.archive_row(app_id)
    if not row["archive_dir"]:
        return
    root = archive_root()
    directory = _child(root, row["archive_dir"])
    if not directory.is_dir():
        LOGGER.warning("Dossier d'archive absent : %s", row["archive_dir"])
        return
    app = db.get_application(app_id) | {"archive_id": row["archive_id"]}
    interviews = db.list_interviews(app_id)
    snapshot = json.loads(row["offer_snapshot_json"] or "{}")
    documents = db.list_documents(app_id)
    write_text(directory / "LISEZMOI.md", render_readme(app, snapshot, interviews, documents))
    write_json(directory / "candidature.json", archive_document(app, snapshot, interviews))
    _write_calendars(directory / "entretien", interviews)
    regenerate_index(root)


def _write_calendars(directory: Path, interviews: list[dict]) -> None:
    directory.mkdir(exist_ok=True)
    expected = set()
    for interview in interviews:
        name = f"{interview['starts_at'][:10]}_entretien-{interview['id']}.ics"
        write_bytes(directory / name, db.interview_calendar(interview["id"])[1])
        expected.add(name)
    for stale in directory.iterdir():  # entretien déplacé à une autre date : l'ancien fichier disparaît
        if _ICS_NAME.fullmatch(stale.name) and stale.name not in expected and not stale.is_symlink():
            stale.unlink()


def retire(archive_dir: str | None) -> str | None:
    """Déplace le dossier d'une candidature retirée du suivi dans ``_retirees/`` (jamais supprimé)."""
    if not archive_dir:
        return None
    try:
        root = archive_root()
        source = _child(root, archive_dir)
        if not source.is_dir():
            LOGGER.warning("Dossier d'archive absent, rien à déplacer : %s", archive_dir)
            return None
        with _FILES_LOCK:
            retired = root / RETIRED
            retired.mkdir(exist_ok=True)
            taken = {p.name.casefold() for p in retired.iterdir()}
            name, number = archive_dir, 2
            while name.casefold() in taken:
                name = f"{archive_dir[:MAX_NAME_LENGTH - 4]}-{number}"
                number += 1
            os.replace(source, retired / name)
            regenerate_index(root)
        return f"{RETIRED}/{name}"
    except Exception:
        LOGGER.exception("Dossier d'archive %s non déplacé", archive_dir)
        return None


def catch_up() -> list[int]:
    """Crée l'archive des candidatures qui n'en ont pas encore (rattrapage) ; renvoie leurs identifiants."""
    created = []
    for app_id in db.applications_without_archive():
        try:
            if ensure_archive(app_id):
                created.append(app_id)
        except Exception:
            LOGGER.exception("Archive de la candidature %s non créée", app_id)
    return created


def reindex() -> dict:
    """Recrée en base les candidatures dont seul le dossier subsiste, sans doublon (``archive_id``)."""
    known = db.known_archive_ids()
    created = skipped = 0
    for directory in _archive_dirs(archive_root()):
        try:
            document = read_document(directory)
            archive_id = str(uuid.UUID(str(document.get("archive_id"))))
        except ValueError:
            skipped += 1
            continue
        if archive_id in known:
            skipped += 1
            continue
        data = document["application"] | {
            "archive_id": archive_id, "archive_dir": directory.name,
            "interviews": document.get("interviews") if isinstance(document.get("interviews"), list) else [],
            "offer_snapshot_json": json.dumps(document.get("offer_snapshot") or {}, ensure_ascii=False),
        }
        app_id = db.restore_application(data)
        known.add(archive_id)
        created += 1
        refresh(app_id)
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------- capture de l'offre


def _capture_module() -> ModuleType:
    """Module de capture importé à l'usage (``import_module`` respecte un faux module installé par les tests)."""
    return importlib.import_module("jobradar.capture")


def _failed(message: str) -> dict:
    return {"capture_status": "failed", "capture_level": "indisponible", "capture_error": message,
            "captured_at_utc": db.utc_iso(db.utcnow(), "captured_at")}


def run_capture(app_id: int) -> None:
    """Capture une offre en attente, de façon synchrone ; une capture prise ailleurs est ignorée."""
    if not db.start_capture(app_id):
        return
    previous = -1
    try:
        row = db.archive_row(app_id)
        previous = _previous_rank(row)
        fields = _capture(row, previous)
    except Exception as exc:  # jamais de capture laissée en « running »
        LOGGER.exception("Échec inattendu de la capture de la candidature %s", app_id)
        fields = _failed(f"Échec inattendu de la capture : {exc}")
    if CAPTURE_RANK.get(fields["capture_level"], 0) < previous:
        fields = _kept(row, fields)
    db.set_archive_fields(app_id, fields)
    refresh(app_id)


def _previous_rank(row: dict) -> int:
    """Fidélité de la copie déjà présente dans ``offre/`` ; -1 s'il n'y en a pas."""
    if not row["archive_dir"] or not (archive_root() / row["archive_dir"] / "offre" / "offre.pdf").is_file():
        return -1
    return CAPTURE_RANK.get(row["capture_level"], -1)


def _kept(row: dict, fields: dict) -> dict:
    """Recapture moins fidèle : la copie précédente et son niveau sont conservés, l'information est enregistrée."""
    detail = f" Détail : {fields['capture_error']}" if fields.get("capture_error") else ""
    return {"capture_status": "done", "capture_level": row["capture_level"],
            "captured_at_utc": row["captured_at_utc"],
            "capture_error": f"Recapture moins fidèle ({fields['capture_level']}) ignorée : la copie précédente "
                             f"({row['capture_level']}) est conservée.{detail}"}


# Recaptures demandées par l'utilisateur : lui seul est devant l'écran, elles seules peuvent ouvrir une fenêtre
# visible (dernier recours contre un mur anti-robot). Les captures automatiques restent toujours invisibles.
_interactive: set[int] = set()
_interactive_lock = threading.Lock()


def _capture(row: dict, previous: int) -> dict:
    directory = _child(archive_root(), row["archive_dir"])
    if not directory.is_dir():
        return _failed("Dossier d'archive introuvable.")
    try:
        capture = _capture_module()
    except ImportError as exc:
        return _failed(f"Module de capture indisponible ({exc}).")
    snapshot = json.loads(row["offer_snapshot_json"] or "{}")
    profile = config.browser_profile_dir().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    offer_dir = (directory / "offre").resolve()
    # Capture à part (même système de fichiers) : offre.pdf et offre.png ne sont remplacés qu'à fidélité égale ou
    # supérieure ; le dossier caché en « .tmp » n'est ni listé ni sauvegardé.
    work = Path(tempfile.mkdtemp(prefix=".capture-", suffix=".tmp", dir=offer_dir))
    try:
        with _interactive_lock:
            allow_headed = row["id"] in _interactive
            _interactive.discard(row["id"])
        result = capture.capture_offer(url=snapshot.get("url") or row["url"], dest_dir=work, offer=snapshot,
                                       browser_profile_dir=profile, timeout_s=CAPTURE_TIMEOUT_S,
                                       allow_headed=allow_headed)
        if CAPTURE_RANK.get(result.level, 0) >= previous:
            new_pdf = (work / "offre.pdf").is_file()
            for name in ("offre.pdf", "offre.png"):
                if (work / name).is_file():
                    os.replace(work / name, offer_dir / name)
                elif name == "offre.png" and new_pdf:
                    # Nouvelle copie sans image (page trop haute pour une image complète et nette) :
                    # l'ancienne image ne correspond plus au PDF qui fait foi.
                    (offer_dir / name).unlink(missing_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"capture_status": "failed" if result.level == "indisponible" else "done",
            "capture_level": result.level, "capture_error": result.error, "captured_at_utc": result.captured_at_utc}


class CaptureQueue:
    """File unique : un fil de travail, une capture à la fois. Elle ne contient que des identifiants."""

    def __init__(self) -> None:
        self._ids: queue.Queue[int | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._work, name="jobradar-capture", daemon=True)

    def start(self) -> int:
        """Démarre le fil et relance les captures en attente ou interrompues par un arrêt."""
        pending = db.pending_captures(reset_running=True)
        for app_id in pending:
            self._ids.put(app_id)
        self._thread.start()
        return len(pending)

    def submit(self, app_id: int) -> None:
        self._ids.put(app_id)

    def stop(self, timeout_s: float = 5) -> None:
        """N'attend pas la fin des captures restantes : elles repartiront au prochain démarrage."""
        self._stop.set()
        self._ids.put(None)
        self._thread.join(timeout_s)

    def wait_idle(self, timeout_s: float = 10) -> bool:
        """Attend que la file soit vide (tests)."""
        deadline = time.monotonic() + timeout_s
        while self._ids.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._ids.unfinished_tasks

    def _work(self) -> None:
        while not self._stop.is_set():
            app_id = self._ids.get()
            try:
                if app_id is not None and not self._stop.is_set():
                    run_capture(app_id)
            except Exception:
                LOGGER.exception("Fil de capture : échec pour la candidature %s", app_id)
            finally:
                self._ids.task_done()


_QUEUE: CaptureQueue | None = None


def start_queue() -> CaptureQueue:
    """Démarrage du serveur : file de capture, rattrapage des archives manquantes et reprise des captures."""
    global _QUEUE
    stop_queue()
    _QUEUE = CaptureQueue()
    _QUEUE.start()
    for app_id in catch_up():
        _QUEUE.submit(app_id)
    return _QUEUE


def stop_queue() -> None:
    global _QUEUE
    if _QUEUE is not None:
        _QUEUE.stop()
        _QUEUE = None


def submit_capture(app_id: int) -> None:
    """Sans serveur démarré, la capture reste « pending » : relancée par le serveur ou ``cli refresh``."""
    if _QUEUE is not None:
        _QUEUE.submit(app_id)


def on_created(app: dict) -> dict:
    """Après ``db.create_application`` : dossier créé tout de suite, capture en arrière-plan."""
    try:
        if ensure_archive(app["id"]):
            submit_capture(app["id"])
    except Exception:
        LOGGER.exception("Archive de la candidature %s non créée (rattrapage au prochain démarrage)", app["id"])
    return db.get_application(app["id"])


def recapture(app_id: int) -> None:
    db.request_capture(app_id)  # 404 sans archive, 409 si une capture est en attente ou en cours
    with _interactive_lock:
        _interactive.add(app_id)
    refresh(app_id)
    submit_capture(app_id)


def run_pending_captures() -> int:
    """Pour ``cli refresh`` : traite ici les captures en attente (celles d'un serveur actif sont ignorées)."""
    pending = db.pending_captures()
    for app_id in pending:
        run_capture(app_id)
    return len(pending)


def check_offers_online(at: datetime | None = None) -> dict:
    """Annonce retirée ? Au plus une vérification par jour et par candidature non finale."""
    at = at or db.utcnow()
    try:
        capture = _capture_module()
    except ImportError as exc:
        LOGGER.warning("Vérification des annonces impossible : module de capture indisponible (%s)", exc)
        return {"checked": 0, "skipped": 0}
    checked = skipped = 0
    today = at.astimezone(_zone()).date()
    for app in db.applications_to_check_online():
        last = db.parse_utc(app["offer_checked_at_utc"])
        if last is not None and last.astimezone(_zone()).date() >= today:
            skipped += 1
            continue
        try:
            online = capture.is_offer_online(app["url"])
        except Exception as exc:
            LOGGER.warning("Vérification de %s impossible : %s", app["url"], exc)
            online = None
        value = None if online is None else int(bool(online))
        db.set_archive_fields(app["id"], {"offer_online": value,
                                          "offer_checked_at_utc": db.utc_iso(at, "offer_checked_at")})
        checked += 1
        refresh(app["id"])
    return {"checked": checked, "skipped": skipped}


_BROWSER_CACHE: tuple[ModuleType, float, dict] | None = None


def browser_status() -> dict:
    """État du navigateur de capture, mis en cache : la vérification lance réellement un navigateur."""
    global _BROWSER_CACHE
    try:
        capture = _capture_module()
    except ImportError as exc:
        return {"available": False, "engine": None, "detail": f"Module de capture indisponible ({exc})."}
    cached = _BROWSER_CACHE
    if cached and cached[0] is capture and time.monotonic() - cached[1] < BROWSER_STATUS_TTL_S:
        return cached[2]
    try:
        status = dict(capture.browser_status())
    except Exception as exc:
        status = {"available": False, "engine": None, "detail": f"Navigateur non vérifiable : {exc}"}
    _BROWSER_CACHE = (capture, time.monotonic(), status)
    return status


# ---------------------------------------------------------------- consultation par l'API


def directory_of(app_id: int) -> Path:
    row = db.archive_row(app_id)
    if not row["archive_dir"]:
        raise db.NotFound("Archive introuvable pour cette candidature.")
    directory = _child(archive_root(), row["archive_dir"])
    if not directory.is_dir():
        raise db.NotFound("Le dossier d'archive de cette candidature n'existe plus.")
    return directory


def list_files(directory: Path) -> list[dict]:
    """Fichiers servis par l'API : types connus, liens symboliques ni suivis ni listés."""
    files = []
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        here = Path(current)
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink() and not d.startswith("."))
        for name in filenames:
            path = here / name
            kind = _KINDS.get(path.suffix.lower())
            if kind is None or name.startswith(".") or path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:  # fichier remplacé pendant le parcours
                continue
            files.append({"path": path.relative_to(directory).as_posix(), "size": info.st_size, "kind": kind,
                          "modified_at": db.utc_iso(datetime.fromtimestamp(info.st_mtime, timezone.utc),
                                                    "modified_at")})
    return sorted(files, key=lambda f: f["path"])


def archive_view(app_id: int) -> dict:
    row = db.archive_row(app_id)
    directory = directory_of(app_id)
    return {
        "dir": directory.name,
        "files": list_files(directory),
        "capture": {"status": row["capture_status"], "level": row["capture_level"], "error": row["capture_error"],
                    "captured_at": row["captured_at_utc"]},
        "offer_online": None if row["offer_online"] is None else bool(row["offer_online"]),
        "offer_checked_at": row["offer_checked_at_utc"],
        "browser": browser_status(),
    }


def resolve_file(directory: Path, relative: str) -> Path:
    """Fichier demandé par l'API : chemin relatif sans « .. », sans lien symbolique, et listé."""
    if not relative or "\x00" in relative or "\\" in relative:
        raise UnsafePath("Chemin de fichier invalide.")
    parts = relative.split("/")
    if re.match(r"[A-Za-z]:", relative) or any(p in ("", ".", "..") for p in parts):
        raise UnsafePath("Chemin refusé : il doit rester dans le dossier de la candidature.")
    path = directory
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise UnsafePath("Les liens symboliques sont refusés.")
    try:
        path.resolve(strict=True).relative_to(directory.resolve())
    except (OSError, ValueError):
        raise db.NotFound("Fichier introuvable dans l'archive.") from None
    if "/".join(parts) not in {f["path"] for f in list_files(directory)}:
        raise db.NotFound("Fichier introuvable dans l'archive.")
    return path


def mime_type(path: Path) -> str:
    if path.suffix.lower() == ".md":
        return "text/markdown; charset=utf-8"
    if path.suffix.lower() in (".txt", ".json"):
        return ("application/json" if path.suffix.lower() == ".json" else "text/plain") + "; charset=utf-8"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def attach(app_id: int, filename: str, content_base64: str) -> dict:
    """Capture fournie par l'utilisateur : PDF, PNG ou JPEG vérifié par signature, 20 Mo au plus."""
    if len(content_base64) > (MAX_ATTACHMENT_BYTES + 2) // 3 * 4 + 4:
        raise InvalidAttachment("Le fichier dépasse 20 Mo.")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidAttachment("Contenu base64 invalide.") from None
    if not content:
        raise InvalidAttachment("Le fichier est vide.")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise InvalidAttachment("Le fichier dépasse 20 Mo.")
    extension = next((ext for magic, ext in _SIGNATURES if content.startswith(magic)), None)
    supplied = Path(filename).suffix.lower()
    if extension is None or supplied not in ({".jpg", ".jpeg"} if extension == ".jpg" else {extension}):
        raise InvalidAttachment("Seuls les fichiers PDF, PNG et JPEG sont acceptés (contenu et extension "
                                "doivent correspondre).")
    offer_dir = directory_of(app_id) / "offre"
    if offer_dir.is_symlink() or not offer_dir.is_dir():
        raise UnsafePath("Dossier « offre » invalide.")
    stamp = db.utcnow().astimezone(_zone()).strftime("%Y%m%d-%H%M%S")
    with _FILES_LOCK:
        target, number = offer_dir / f"capture-utilisateur-{stamp}{extension}", 2
        while target.exists() or target.is_symlink():
            target = offer_dir / f"capture-utilisateur-{stamp}-{number}{extension}"
            number += 1
        write_bytes(target, content)
    refresh(app_id)
    return {"path": f"offre/{target.name}", "size": len(content), "kind": _KINDS[extension]}


def info() -> dict:
    root = archive_root()
    size = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(current)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            try:
                size += (here / name).lstat().st_size
            except OSError:
                continue
    return {"root": str(root.resolve()), "count": len(_archive_dirs(root)), "size_bytes": size,
            "browser": browser_status()}


def open_folder(app_id: int) -> None:
    """Ouvre le dossier dans l'explorateur, sans attendre la fermeture de la fenêtre."""
    directory = directory_of(app_id)
    if sys.platform == "win32":
        try:
            os.startfile(directory)  # type: ignore[attr-defined]
        except OSError as exc:
            raise ExplorerUnavailable(f"Impossible d'ouvrir l'explorateur : {exc}") from exc
        return
    command = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if command is None:
        raise ExplorerUnavailable("Aucun explorateur de fichiers n'est disponible sur cette machine.")
    try:
        process = subprocess.Popen([command, str(directory)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise ExplorerUnavailable(f"Impossible d'ouvrir l'explorateur : {exc}") from exc
    threading.Thread(target=process.wait, daemon=True).start()  # évite un processus zombie
