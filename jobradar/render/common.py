"""Fonctions partagées par le rendu et le contrôle ATS."""

from __future__ import annotations

import html
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import RenderError

SECTION_TITLES = {
    "fr": {
        "experience": "Expérience professionnelle",
        "project": "Projets",
        "education": "Formation",
        "certification": "Certifications",
        "language": "Langues",
        "skills": "Compétences",
        "summary": "Profil",
    },
    "en": {
        "experience": "Professional Experience",
        "project": "Projects",
        "education": "Education",
        "certification": "Certifications",
        "language": "Languages",
        "skills": "Skills",
        "summary": "Summary",
    },
}

SECTION_ALIASES = {
    "experience": (
        "expérience professionnelle",
        "expériences professionnelles",
        "expérience",
        "expériences",
        "parcours professionnel",
        "expérience en entreprise",
        "professional experience",
        "work experience",
        "employment history",
        "experience",
    ),
    "project": (
        "projets",
        "projet",
        "projets personnels",
        "projets académiques",
        "réalisations",
        "projects",
        "project",
        "personal projects",
        "academic projects",
    ),
    "education": ("formation", "formations", "études", "cursus", "diplômes", "education", "academic background"),
    "certification": ("certification", "certifications", "certificats", "certificates", "licenses and certifications"),
    "language": ("langue", "langues", "languages", "language"),
    "skills": (
        "compétence",
        "compétences",
        "compétences techniques",
        "savoir-faire",
        "skills",
        "technical skills",
        "core skills",
    ),
    "summary": ("profil", "profil professionnel", "résumé", "à propos", "summary", "profile", "about me"),
}

# Types de section qu'une composition de CV peut contenir (le résumé est un champ à part).
CV_SECTION_TYPES = ("experience", "project", "education", "certification", "language", "skills")

_ALIAS_INDEX: dict[str, str] = {}

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

_MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
              "novembre", "décembre")
_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")

_PROJECT_SETTINGS = {
    "fr": {"école": "Projet d'école", "ecole": "Projet d'école", "perso": "Projet personnel", "ctf": "CTF",
           "stage": "Projet de stage"},
    "en": {"école": "School project", "ecole": "School project", "perso": "Personal project", "ctf": "CTF",
           "stage": "Internship project"},
}


def string_value(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return ""


def escaped(value: object) -> str:
    return html.escape(string_value(value), quote=True)


def normalize(value: object) -> str:
    """Forme de comparaison : sans accents, minuscules, ponctuation réduite à des espaces."""
    text = unicodedata.normalize("NFKD", string_value(value)).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def normalize_keyword(value: object) -> str:
    """Comme `normalize`, mais garde « + » et « # » (C++, C#) pour ne pas confondre les langages."""
    text = unicodedata.normalize("NFKD", string_value(value)).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.casefold()).split())


def compact_text(value: object) -> str:
    return " ".join(string_value(value).split())


def language_of(document: dict) -> str:
    return "en" if document.get("language") == "en" else "fr"


def colon(language: str) -> str:
    """Deux-points typographique : espace avant en français, aucune en anglais."""
    return ": " if language == "en" else " : "


def section_title(section_type: str, language: str) -> str:
    return SECTION_TITLES.get(language, SECTION_TITLES["fr"]).get(section_type, section_type.title())


def identify_section(title: str) -> str | None:
    if not _ALIAS_INDEX:
        for section_type, aliases in SECTION_ALIASES.items():
            for alias in aliases:
                _ALIAS_INDEX.setdefault(normalize(alias), section_type)
    return _ALIAS_INDEX.get(normalize(title))


def format_date(value: object) -> str:
    text = compact_text(value)
    match = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", text)
    if not match:
        return text
    year, month = match.groups()
    return f"{month}/{year}" if month else year


def current_label(language: str) -> str:
    return "Present" if language == "en" else "Aujourd'hui"


def format_period(start: object, end: object, language: str) -> str:
    """« 09/2024 – 02/2025 », « 09/2024 – Aujourd'hui » ; vide si aucune date n'est connue."""
    start_text = format_date(start)
    end_text = format_date(end)
    if not start_text:
        return end_text
    if not end_text:
        end_text = current_label(language)
    if end_text == start_text:
        return start_text
    return f"{start_text} – {end_text}"


def project_setting_label(setting: object, language: str) -> str:
    key = compact_text(setting).casefold()
    if not key or key == "autre":
        return ""
    return _PROJECT_SETTINGS.get(language, _PROJECT_SETTINGS["fr"]).get(key, compact_text(setting))


def display_url(url: str) -> str:
    """Adresse affichée sans protocole ni « www. », plus lisible et toujours reconnue par les ATS."""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.IGNORECASE)
    text = re.sub(r"^www\.", "", text, flags=re.IGNORECASE)
    return text.rstrip("/")


@dataclass(frozen=True)
class Entry:
    """Élément d'une section prêt à être mis en page, quel que soit le modèle."""

    title: str
    subtitle: str = ""
    period: str = ""
    details: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    tech: list[str] = field(default_factory=list)  # technologies et lien d'un projet ou d'un poste


@dataclass(frozen=True)
class SectionView:
    type: str
    title: str
    entries: list[Entry] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)  # (libellé, texte) pour compétences et langues


def _texts(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [text for text in (compact_text(value) for value in values) if text]


def _bullets(item: dict) -> list[str]:
    bullets = item.get("bullets")
    if not isinstance(bullets, list):
        return []
    return [text for text in (compact_text(b.get("text")) for b in bullets if isinstance(b, dict)) if text]


def _entry(section_type: str, item: dict, language: str) -> Entry | None:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    if section_type in {"experience", "project"}:
        title = compact_text(fields.get("title"))
        structure = compact_text(fields.get("organization"))
        if section_type == "project" and not structure:
            structure = project_setting_label(fields.get("setting"), language)
        subtitle = " · ".join(part for part in (structure, compact_text(fields.get("location"))) if part)
        tech: list[str] = []
        technologies = _texts(fields.get("technologies"))
        if technologies:
            tech.append(f"Technologies{colon(language)}{', '.join(technologies)}")
        link = compact_text(fields.get("link"))
        if link:
            tech.append(f"{'Link' if language == 'en' else 'Lien'}{colon(language)}{display_url(link)}")
        period = format_period(fields.get("start"), fields.get("end"), language)
        return Entry(title, subtitle, period, [], _bullets(item), tech) if title or subtitle else None
    if section_type == "education":
        title = compact_text(fields.get("degree") or fields.get("title"))
        school = compact_text(fields.get("school") or fields.get("organization"))
        subtitle = " · ".join(part for part in (school, compact_text(fields.get("location"))) if part)
        details = [compact_text(fields.get("details"))] if compact_text(fields.get("details")) else []
        period = format_period(fields.get("start"), fields.get("end"), language)
        return Entry(title, subtitle, period, details, _bullets(item)) if title or subtitle else None
    if section_type == "certification":
        title = compact_text(fields.get("name") or fields.get("title"))
        issuer = compact_text(fields.get("issuer") or fields.get("organization"))
        date = format_date(fields.get("date") or fields.get("start"))
        return Entry(title, issuer, date, [], _bullets(item)) if title else None
    return None


def section_views(composition: dict) -> list[SectionView]:
    """Traduit les sections d'une composition en blocs neutres, dans l'ordre de la composition."""
    language = language_of(composition)
    views: list[SectionView] = []
    sections = composition.get("sections")
    for section in sections if isinstance(sections, list) else []:
        if not isinstance(section, dict):
            continue
        section_type = string_value(section.get("type"))
        if section_type not in CV_SECTION_TYPES:
            continue
        title = section_title(section_type, language)
        if section_type == "skills":
            lines: list[tuple[str, str]] = []
            groups = section.get("groups")
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                skills = group.get("skills")
                texts = [compact_text(s.get("text")) for s in skills if isinstance(s, dict)] if isinstance(skills, list) else []
                texts = [text for text in texts if text]
                if texts:
                    lines.append((compact_text(group.get("label")), ", ".join(texts)))
            if lines:
                views.append(SectionView(section_type, title, lines=lines))
            continue
        items = section.get("items")
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if section_type == "language":
            parts = []
            for item in items:
                fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                name = compact_text(fields.get("name") or fields.get("title"))
                level = compact_text(fields.get("level"))
                if name:
                    parts.append(f"{name}{colon(language)}{level}" if level else name)
            if parts:
                views.append(SectionView(section_type, title, lines=[("", " · ".join(parts))]))
            continue
        entries = [entry for entry in (_entry(section_type, item, language) for item in items) if entry is not None]
        if entries:
            views.append(SectionView(section_type, title, entries=entries))
    return views


def identity_lines(identity: dict, language: str) -> tuple[str, list[str], list[str]]:
    """Nom, coordonnées (ville, e-mail, téléphone) et liens affichables.

    Le téléphone est précédé de « Tél. » : règle ATS, jamais de pictogramme seul.
    """
    name = compact_text(identity.get("full_name"))
    contacts: list[str] = []
    city = compact_text(identity.get("city"))
    if city:
        contacts.append(city)
    email = compact_text(identity.get("email"))
    if email:
        contacts.append(email)
    phone = compact_text(identity.get("phone"))
    if phone:
        contacts.append(f"{'Phone' if language == 'en' else 'Tél.'} {phone}")
    links: list[str] = []
    for link in identity.get("links") if isinstance(identity.get("links"), list) else []:
        if not isinstance(link, dict) or not compact_text(link.get("url")):
            continue
        label = compact_text(link.get("label"))
        url = display_url(compact_text(link.get("url")))
        links.append(f"{label}{colon(language)}{url}" if label else url)
    return name, contacts, links


def identity_parts(identity: dict, language: str) -> tuple[str, list[str]]:
    name, contacts, links = identity_lines(identity, language)
    return name, contacts + links


def letter_date(language: str, city: str, today: date | None = None) -> str:
    """Lieu et date de la lettre, au jour du rendu : « Paris, le 23 octobre 2026 »."""
    today = today or date.today()
    if language == "en":
        return f"{_MONTHS_EN[today.month - 1]} {today.day}, {today.year}"
    day = "1er" if today.day == 1 else str(today.day)
    text = f"le {day} {_MONTHS_FR[today.month - 1]} {today.year}"
    return f"{city}, {text}" if city else text[0].upper() + text[1:]


def letter_recipient(letter: dict) -> list[str]:
    """Lignes du destinataire, facultatives (clé « recipient » : liste de lignes)."""
    return _texts(letter.get("recipient"))


def document_title(composition: dict, name: str) -> str:
    if composition.get("kind") == "lettre":
        subject = compact_text(composition.get("subject"))
        return subject or ("Cover letter" if language_of(composition) == "en" else "Lettre de motivation")
    headline = compact_text(composition.get("headline"))
    return " — ".join(part for part in (name, headline) if part) or "CV"


def safe_base_name(base_name: str) -> str:
    if not isinstance(base_name, str):
        raise RenderError("Le nom de fichier doit être une chaîne de caractères.")
    name = base_name.strip()
    if not name or name in {".", ".."} or _INVALID_FILENAME.search(name) or name.endswith((".", " ")):
        raise RenderError("Le nom de fichier n'est pas portable sous Windows.")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise RenderError("Le nom de fichier est réservé sous Windows.")
    if len(name) > 180:
        raise RenderError("Le nom de fichier est trop long.")
    return name


def ensure_output_dir(out_dir: Path) -> Path:
    try:
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise OSError
        return directory
    except OSError as exc:
        raise RenderError("Le dossier de sortie ne peut pas être créé.") from exc


def atomic_write_files(files: dict[Path, bytes]) -> None:
    """Écrit chaque fichier dans un temporaire du même dossier, puis les met en place ensemble.

    Si l'écriture d'un fichier échoue, aucun fichier final n'est remplacé.
    """
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in files.items():
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            temporary = Path(name)
            staged.append((temporary, destination))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, destination in staged:
            os.replace(temporary, destination)
    except OSError as exc:
        raise RenderError("Les fichiers du document ne peuvent pas être écrits.") from exc
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
