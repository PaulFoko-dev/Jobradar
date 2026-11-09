"""Mémoire des réponses types : validation datée, expiration par catégorie, historique des versions.

Une réponse type (``profile["standard_answers"][clé]``) porte ``validated_at`` et ``expires_at`` ; une réponse
expirée n'est plus utilisée et le formulaire la redemande. Une nouvelle réponse différente crée une version :
l'ancienne rejoint ``history`` sans être effacée. Les réponses sensibles ne sont jamais mémorisées.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Durées de validité (jours) ; absent = ne s'écoule pas (identité, diplôme, permis…).
EXPIRY_DAYS = {
    "availability": 30, "relocation": 90, "desired_salary": 90, "notice_period": 90,
    "internship_duration": 90, "work_authorization": 365, "sponsorship": 365, "internship_agreement": 365,
}
LEARNED_EXPIRY_DAYS = 180  # réponse apprise d'un formulaire, sans concept reconnu
HISTORY_LIMIT = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def expiry_days(key: str) -> int | None:
    if key in EXPIRY_DAYS:
        return EXPIRY_DAYS[key]
    return LEARNED_EXPIRY_DAYS if key.startswith("form_") else None


def stamp(key: str, item: dict, at: datetime | None = None) -> dict:
    """Date de validation et d'expiration d'une réponse qui vient d'être confirmée par l'utilisateur."""
    at = at or _now()
    item["validated_at"] = _iso(at)
    days = expiry_days(key)
    item["expires_at"] = _iso(at + timedelta(days=days)) if days else None
    item["status"] = "active"
    return item


def is_expired(item: dict, at: datetime | None = None) -> bool:
    expires = _parse(item.get("expires_at"))
    return bool(expires and expires <= (at or _now()))


def merge(previous: dict, incoming: dict, at: datetime | None = None) -> dict:
    """Réponses saisies par l'utilisateur : nouvelle version si la réponse change, revalidation sinon."""
    merged = {}
    for key, item in incoming.items():
        if not isinstance(item, dict):
            continue
        item = dict(item)
        old = previous.get(key) if isinstance(previous.get(key), dict) else None
        if item.get("sensitive"):
            continue  # jamais mémorisée
        if old and str(old.get("answer") or "").strip() == str(item.get("answer") or "").strip():
            # Inchangée : on garde ses dates (une réponse expirée le reste tant qu'elle n'est pas confirmée).
            item = old | {k: v for k, v in item.items() if k in {"question", "answer"}}
            merged[key] = item if item.get("validated_at") else stamp(key, item, at)
            continue
        history = list((old or {}).get("history") or [])
        if old and str(old.get("answer") or "").strip() != str(item.get("answer") or "").strip():
            history.insert(0, {"answer": old.get("answer"), "validated_at": old.get("validated_at"),
                               "status": "remplacé"})
        item["history"] = history[:HISTORY_LIMIT]
        merged[key] = stamp(key, item, at)
    return merged


def confirm(standard: dict, key: str, at: datetime | None = None) -> dict:
    """L'utilisateur confirme qu'une réponse est toujours valable : elle repart pour une période complète."""
    if key not in standard or not isinstance(standard[key], dict):
        raise KeyError(key)
    standard[key] = stamp(key, dict(standard[key]), at)
    return standard


def view(standard: dict, at: datetime | None = None) -> list[dict]:
    """Réponses avec leur état, pour l'interface (expirée, à revalider bientôt, active)."""
    rows = []
    for key, item in sorted(standard.items()):
        if not isinstance(item, dict):
            continue
        expires = _parse(item.get("expires_at"))
        soon = bool(expires and not is_expired(item, at) and expires - (at or _now()) <= timedelta(days=7))
        rows.append({"key": key, "question": item.get("question"), "validated_at": item.get("validated_at"),
                     "expires_at": item.get("expires_at"),
                     "state": "expirée" if is_expired(item, at) else "bientôt" if soon else "active"})
    return rows
