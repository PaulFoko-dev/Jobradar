"""Chemins et configuration utilisateur (data/config.json).

Le dossier de données peut être déplacé avec la variable d'environnement
``JOBRADAR_DATA_DIR`` (chemin absolu) : les tests et les essais manuels
travaillent ainsi sur une copie sans jamais toucher à la base réelle. Le
dossier des archives de candidatures suit la même règle avec
``JOBRADAR_ARCHIVE_DIR``.
"""
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .workflow import DEFAULT_TIMEZONE, validate_timezone

ROOT = Path(__file__).resolve().parent.parent


def resolve_data_dir(environ: dict | None = None) -> Path:
    """Dossier de données : ``JOBRADAR_DATA_DIR`` s'il est défini, sinon ``data/`` du projet."""
    raw = (os.environ if environ is None else environ).get("JOBRADAR_DATA_DIR", "").strip()
    if not raw:
        return ROOT / "data"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"JOBRADAR_DATA_DIR doit être un chemin absolu (reçu : {raw!r}).")
    return path


DATA_DIR = resolve_data_dir()
DB_PATH = DATA_DIR / "jobradar.db"
CONFIG_PATH = DATA_DIR / "config.json"
# Dossier vide dans lequel Claude Code est lancé : il ne charge ainsi aucun CLAUDE.md du projet.
CLAUDE_CWD = DATA_DIR / "claude_cwd"

DEFAULT_CONFIG = {
    # Dossier des archives de candidatures ; chaîne vide = « candidatures/ » à la racine du projet.
    # Un chemin relatif est pris depuis la racine du projet.
    "archive_dir": "",
    # Assistant IA en ligne de commande utilisé : "claude" (Claude Code) ou "codex" (Codex CLI).
    "ai_provider": "claude",
    # Modèle par assistant et par tâche ; chaîne vide = modèle par défaut de l'assistant.
    "models": {
        "claude": {
            "criteria": "sonnet", "scoring": "sonnet", "follow_up": "sonnet",
            "profile": "sonnet", "tailoring": "sonnet", "project": "sonnet",
            "interview": "sonnet",
        },
        "codex": {
            "criteria": "", "scoring": "", "follow_up": "",
            "profile": "", "tailoring": "", "project": "",
            "interview": "",
        },
    },
    "claude_bin": "claude",
    "codex_bin": "codex",
    "ai_timeout_s": 600,
    # Fuseau métier des dates limites, relances et entretiens (identifiant IANA).
    "timezone": DEFAULT_TIMEZONE,
    # Jours sans réponse avant de proposer une relance.
    "follow_up_delay_days": 10,
    # Une date limite à moins de ce nombre de jours fait passer l'offre en tête.
    "deadline_urgent_days": 7,
    # Alerte pour une offre nouvellement analysée avec un score au moins égal à min_score.
    "alerts": {"min_score": 85, "desktop": True},
    # Récapitulatif des alertes par e-mail (désactivé par défaut). Le mot de passe SMTP n'est jamais
    # stocké ici : trousseau du système, ou variable d'environnement JOBRADAR_SMTP_PASSWORD.
    "email": {
        "enabled": False,
        "to": "",
        "from": "",  # vide = même adresse que "username"
        "smtp_host": "",
        "smtp_port": 587,
        "security": "starttls",  # "starttls" (587) ou "ssl" (465) ; jamais d'envoi en clair
        "username": "",
        "kinds": ["high_score_offer", "follow_up_due", "interview_reminder"],
    },
    # Nombre d'offres envoyées à Claude par appel, et appels en parallèle.
    "scoring_batch_size": 4,
    "scoring_parallel": 3,
    # Plafond d'offres nouvelles analysées par exécution (préserve le quota de l'abonnement).
    "max_offers_scored_per_run": 150,
    # Résultats demandés par requête et par source.
    "results_per_query": 25,
    "sources": {
        "indeed": True,
        "linkedin": True,
        "hellowork": True,
        "welcometothejungle": True,
        "francetravail": False,
        "labonnealternance": False,
        "arbeitnow": True,
        "remoteok": True,
        "ats": True,
        # Catalogue de grands groupes (Thales, Airbus, Accenture, Alten…) : voir sources/groups.py.
        "grands_groupes": True,
    },
    # Identifiants API France Travail (gratuits sur https://francetravail.io).
    "francetravail": {"client_id": "", "client_secret": ""},
    # Clé de lecture gratuite créée sur https://api.apprentissage.beta.gouv.fr/.
    "labonnealternance": {"api_key": ""},
    # Pages carrières d'entreprises hébergées sur un ATS avec API publique.
    # ats : "greenhouse" | "lever" | "smartrecruiters" ; slug : identifiant de l'entreprise sur l'ATS.
    # ats "workday" : host (ex. "thales.wd3.myworkdayjobs.com") et site (ex. "Careers") à la place du slug.
    "companies": [
        {"name": "Devoteam", "ats": "smartrecruiters", "slug": "Devoteam"},
        {"name": "Sopra Steria", "ats": "smartrecruiters", "slug": "SopraSteria1"},
        {"name": "Datadog", "ats": "greenhouse", "slug": "datadog"},
        {"name": "Doctolib", "ats": "greenhouse", "slug": "doctolib"},
        {"name": "Qonto", "ats": "lever", "slug": "qonto"},
    ],
}


def archive_dir(cfg: dict | None = None, environ: dict | None = None) -> Path:
    """Racine des archives : ``JOBRADAR_ARCHIVE_DIR``, sinon ``archive_dir`` de la configuration.

    Relue à chaque appel : les tests et les essais manuels la redirigent par la variable.
    """
    raw = (os.environ if environ is None else environ).get("JOBRADAR_ARCHIVE_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise RuntimeError(f"JOBRADAR_ARCHIVE_DIR doit être un chemin absolu (reçu : {raw!r}).")
        return path
    value = (load_config() if cfg is None else cfg).get("archive_dir")
    if not isinstance(value, str) or not value.strip():
        return ROOT / "candidatures"
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else ROOT / path


def browser_profile_dir() -> Path:
    """Profil du navigateur dédié aux captures (sessions) : sensible, jamais sauvegardé ni partagé."""
    return DATA_DIR / "navigateur"


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_CWD.mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg)


def save_config_section(key: str, value: object) -> None:
    """Remplace la section ``key`` de config.json en conservant toutes les autres clés.

    L'écriture passe par un fichier temporaire (remplacement atomique) qui
    reprend les droits du fichier d'origine.
    """
    load_config()  # crée le fichier par défaut au besoin
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.json doit contenir un objet JSON.")
    raw[key] = value
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IMODE(CONFIG_PATH.stat().st_mode))
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


@dataclass(frozen=True)
class Settings:
    """Réglages métier validés ; une valeur invalide retombe sur la valeur par défaut."""

    timezone: str
    follow_up_delay_days: int
    deadline_urgent_days: int
    alert_min_score: int
    alert_desktop: bool


def _int_setting(cfg: dict, key: str, default: int, low: int, high: int) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        print(f"config.json : « {key} » invalide ({value!r}), valeur par défaut {default} utilisée.", file=sys.stderr)
        return default
    return value


def settings(cfg: dict | None = None) -> Settings:
    cfg = load_config() if cfg is None else cfg
    zone = cfg.get("timezone", DEFAULT_TIMEZONE)
    try:
        validate_timezone(zone)
    except ValueError:
        print(f"config.json : fuseau {zone!r} inconnu, {DEFAULT_TIMEZONE} utilisé.", file=sys.stderr)
        zone = DEFAULT_TIMEZONE
    alerts = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    return Settings(
        timezone=zone,
        follow_up_delay_days=_int_setting(cfg, "follow_up_delay_days", 10, 1, 365),
        deadline_urgent_days=_int_setting(cfg, "deadline_urgent_days", 7, 0, 365),
        alert_min_score=_int_setting(alerts, "min_score", 85, 0, 100),
        alert_desktop=bool(alerts.get("desktop", True)),
    )
