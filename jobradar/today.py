"""Écran « Aujourd'hui » : tout ce qui demande ton attention, regroupé en une seule liste d'actions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db, inbox, vault

INTERVENTION_GROUPS = {
    "confirmation_missing": "Vérifier si la candidature est partie",
    "missing_fields": "Répondre à des questions du formulaire",
    "verification_failed": "Corriger un champ relu différemment",
    "page_validation_failed": "Corriger une erreur signalée par le site",
    "waiting_user": "Terminer une connexion ou une création de compte",
    "review_required": "Relire avant l'envoi",
    "not_application_form": "Formulaire introuvable : envoi manuel",
    "unsupported_form": "Formulaire non pris en charge : envoi manuel",
}


def summary() -> dict:
    with db.connect() as conn:
        attempts = [dict(row) for row in conn.execute(
            """SELECT a.id, a.application_id, a.intervention_json, p.title, p.company
               FROM application_attempts a JOIN applications p ON p.id = a.application_id
               WHERE a.status = 'needs_user' ORDER BY a.updated_at_utc DESC""")]
        ready = [dict(row) for row in conn.execute(
            """SELECT p.id, p.title, p.company, p.score FROM applications p
               WHERE p.status = 'consultee'
                 AND EXISTS (SELECT 1 FROM documents d WHERE d.application_id = p.id AND d.status = 'pret')
                 AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.application_id = p.id
                                 AND a.status IN ('queued', 'running', 'needs_user', 'submitted'))
               ORDER BY p.score DESC LIMIT 20""")]
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        offers = [dict(row) for row in conn.execute(
            """SELECT o.id, o.search_id, o.title, o.company, o.score FROM offers o
               WHERE o.score >= 85 AND o.status NOT IN ('ignored') AND o.first_seen >= ?
                 AND NOT EXISTS (SELECT 1 FROM applications p WHERE p.offer_id = o.id)
               ORDER BY o.score DESC LIMIT 10""", (since,))]
        reminders = [dict(row) for row in conn.execute(
            """SELECT id, kind, payload_json FROM notifications
               WHERE status <> 'dismissed' AND kind IN ('follow_up_due', 'interview_reminder', 'response_detected')
               ORDER BY created_at_utc DESC LIMIT 20""")]
    groups: dict[str, dict] = {}
    for attempt in attempts:
        kind = (json.loads(attempt["intervention_json"] or "null") or {}).get("kind") or "autre"
        group = groups.setdefault(kind, {"kind": kind, "label": INTERVENTION_GROUPS.get(kind, "Action nécessaire"),
                                         "items": []})
        group["items"].append({"application_id": attempt["application_id"], "attempt_id": attempt["id"],
                               "title": attempt["title"], "company": attempt["company"]})
    try:
        vault_state = vault.status()
    except vault.VaultError:
        vault_state = {"initialized": True, "unlocked": False}
    return {
        "interventions": sorted(groups.values(), key=lambda group: -len(group["items"])),
        "ready_to_send": ready,
        "new_offers": offers,
        "reminders": [{"id": row["id"], "kind": row["kind"], **(json.loads(row["payload_json"] or "{}") or {})}
                      for row in reminders],
        "vault": vault_state,
        "inbox_enabled": bool(inbox.settings().get("enabled")),
    }
