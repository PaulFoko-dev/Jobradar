"""Chemins et configuration utilisateur (data/config.json).

Le dossier de données peut être déplacé avec la variable d'environnement
``JOBRADAR_DATA_DIR`` (chemin absolu) : les tests et les essais manuels
travaillent ainsi sur une copie sans jamais toucher à la base réelle. Le
dossier des archives de candidatures suit la même règle avec
``JOBRADAR_ARCHIVE_DIR``.
"""
import json
import os
import plistlib
import shutil
import stat
import subprocess
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
    "browser": {"engine": "system", "executable": "", "cdp_port": 9333, "keep_open": True,
                "ai_supervision": "always", "human_wait_minutes": 10},
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
    """Profil durable du navigateur de candidature : sensible, jamais sauvegardé ni partagé."""
    return DATA_DIR / "navigateur"


def capture_profile_dir() -> Path:
    """Profil séparé des captures : elles ne peuvent pas affamer une candidature."""
    return DATA_DIR / "navigateur-captures"


def system_profile_root() -> Path:
    return DATA_DIR / "navigateur-systeme"


def system_profile_dir(key: str) -> Path:
    """Un profil par navigateur : Chrome et Brave ne partagent jamais le même dossier."""
    return system_profile_root() / key


def browser_settings(cfg: dict | None = None) -> dict:
    raw = (load_config() if cfg is None else cfg).get("browser", {})
    raw = raw if isinstance(raw, dict) else {}
    engine = "system" if raw.get("engine") == "brave" else raw.get("engine")
    engine = engine if engine in {"chromium", "system"} else "system"
    executable = str(raw.get("executable") or "").strip()
    port = raw.get("cdp_port", 9333)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        port = 9333
    keep_open = raw.get("keep_open", True)
    # always : l'IA supervise chaque étape ; fallback : seulement si les heuristiques échouent ; off : jamais.
    supervision = raw.get("ai_supervision") if raw.get("ai_supervision") in {"always", "fallback", "off"} else "always"
    # Attente d'une étape humaine (connexion, création de compte…) avant de passer à la candidature suivante.
    wait = raw.get("human_wait_minutes", 10)
    wait = wait if isinstance(wait, int) and not isinstance(wait, bool) and 1 <= wait <= 60 else 10
    return {"engine": engine, "executable": executable, "cdp_port": port,
            "keep_open": keep_open if isinstance(keep_open, bool) else True, "ai_supervision": supervision,
            "human_wait_minutes": wait}


# Navigateurs pilotables (Chromium) : nom affiché, mots qui les reconnaissent comme navigateur par défaut,
# commandes du PATH puis emplacements usuels Windows/macOS/Linux.
_LOCAL = os.environ.get("LOCALAPPDATA", "")
CHROMIUM_BROWSERS = {
    "chrome": ("Google Chrome", ("chrome",), ("google-chrome", "google-chrome-stable"), (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        _LOCAL + r"\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
    "edge": ("Microsoft Edge", ("msedge", "edge"), ("microsoft-edge", "microsoft-edge-stable"), (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
    "brave": ("Brave", ("brave",), ("brave", "brave-browser"), (
        "/snap/bin/brave",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        _LOCAL + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")),
    "chromium": ("Chromium", ("chromium",), ("chromium", "chromium-browser"), (
        "/snap/bin/chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium")),
    "vivaldi": ("Vivaldi", ("vivaldi",), ("vivaldi", "vivaldi-stable"), (
        _LOCAL + r"\Vivaldi\Application\vivaldi.exe", "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi")),
    "opera": ("Opera", ("opera",), ("opera",), (
        _LOCAL + r"\Programs\Opera\opera.exe", "/Applications/Opera.app/Contents/MacOS/Opera")),
}
OTHER_BROWSERS = {"firefox": "Firefox", "safari": "Safari"}


def _default_browser_id() -> str:
    """Identifiant brut du navigateur par défaut (fichier .desktop, ProgId ou bundle id) ; vide si inconnu."""
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                return str(winreg.QueryValueEx(handle, "ProgId")[0])
        if sys.platform == "darwin":
            plist = (Path.home() / "Library/Preferences/com.apple.LaunchServices"
                     / "com.apple.launchservices.secure.plist")
            handlers = plistlib.loads(plist.read_bytes()).get("LSHandlers", [])
            return next((str(h.get("LSHandlerRoleAll", "")) for h in handlers
                         if h.get("LSHandlerURLScheme") in {"https", "http"}), "com.apple.safari")
        result = subprocess.run(["xdg-settings", "get", "default-web-browser"],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return ""


def default_browser() -> str | None:
    """Clé du navigateur par défaut (``brave``, ``chrome``, ``firefox``…), ou None s'il est inconnu."""
    raw = _default_browser_id().casefold()
    if not raw:
        return None
    # Edge avant Chrome : son bundle macOS ne contient pas « chrome », mais certains ProgId Chromium oui.
    for key in ("edge", "brave", "vivaldi", "opera", "chromium", "chrome"):
        if any(word in raw for word in CHROMIUM_BROWSERS[key][1]):
            return key
    return next((key for key in OTHER_BROWSERS if key in raw), None)


def _installed(key: str) -> str | None:
    _name, _words, commands, paths = CHROMIUM_BROWSERS[key]
    candidates = [shutil.which(command) or "" for command in commands] + list(paths)
    return next((str(Path(item)) for item in candidates if item and Path(item).is_file()), None)


def find_system_browser(explicit: str = "") -> dict | None:
    """Navigateur de candidature : l'exécutable imposé, sinon celui par défaut s'il est pilotable,
    sinon le premier navigateur Chromium installé."""
    default = default_browser()
    if explicit:
        if not Path(explicit).is_file():
            return None
        lowered = Path(explicit).name.casefold()
        key = next((k for k, v in CHROMIUM_BROWSERS.items() if any(w in lowered for w in v[1])), "personnalise")
        name = CHROMIUM_BROWSERS[key][0] if key in CHROMIUM_BROWSERS else Path(explicit).stem
        return {"key": key, "name": name, "path": str(Path(explicit)), "is_default": key == default}
    order = ([default] if default in CHROMIUM_BROWSERS else []) + list(CHROMIUM_BROWSERS)
    for key in order:
        path = _installed(key)
        if path:
            return {"key": key, "name": CHROMIUM_BROWSERS[key][0], "path": path, "is_default": key == default}
    return None


def default_browser_name() -> str | None:
    key = default_browser()
    return CHROMIUM_BROWSERS[key][0] if key in CHROMIUM_BROWSERS else OTHER_BROWSERS.get(key or "")


def save_browser_settings(value: dict) -> dict:
    unknown = set(value) - {"engine", "executable", "cdp_port", "keep_open", "ai_supervision", "human_wait_minutes"}
    if unknown:
        raise ValueError("Réglage navigateur inconnu.")
    merged = browser_settings() | value
    normalized = browser_settings({"browser": merged})
    if value.get("engine") not in {None, "chromium", "system"}:
        raise ValueError("Moteur navigateur invalide.")
    save_config_section("browser", normalized)
    return normalized


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
