"""Chemins et configuration utilisateur (data/config.json)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobradar.db"
CONFIG_PATH = DATA_DIR / "config.json"
# Dossier vide dans lequel Claude Code est lancé : il ne charge ainsi aucun CLAUDE.md du projet.
CLAUDE_CWD = DATA_DIR / "claude_cwd"

DEFAULT_CONFIG = {
    # Assistant IA en ligne de commande utilisé : "claude" (Claude Code) ou "codex" (Codex CLI).
    "ai_provider": "claude",
    # Modèle par assistant et par tâche ; chaîne vide = modèle par défaut de l'assistant.
    "models": {
        "claude": {"criteria": "sonnet", "scoring": "sonnet"},
        "codex": {"criteria": "", "scoring": ""},
    },
    "claude_bin": "claude",
    "codex_bin": "codex",
    "ai_timeout_s": 600,
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
        "ats": True,
    },
    # Identifiants API France Travail (gratuits sur https://francetravail.io).
    "francetravail": {"client_id": "", "client_secret": ""},
    # Pages carrières d'entreprises hébergées sur un ATS avec API publique.
    # ats : "greenhouse" | "lever" | "smartrecruiters" ; slug : identifiant de l'entreprise sur l'ATS.
    "companies": [
        {"name": "Devoteam", "ats": "smartrecruiters", "slug": "Devoteam"},
        {"name": "Sopra Steria", "ats": "smartrecruiters", "slug": "SopraSteria1"},
        {"name": "Datadog", "ats": "greenhouse", "slug": "datadog"},
        {"name": "Doctolib", "ats": "greenhouse", "slug": "doctolib"},
        {"name": "Qonto", "ats": "lever", "slug": "qonto"},
    ],
}


def load_config() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    CLAUDE_CWD.mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base
