"""Notifications : boîte locale durable (table ``notifications``), bureau et récapitulatif par e-mail.

Chaque notification porte une clé de déduplication unique : recréer la même
alerte est sans effet, ce qui rend ``refresh_notifications`` idempotente.
Les textes de bureau ne contiennent aucune donnée personnelle du candidat.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta

from . import db, mailer, workflow
from .config import settings

INTERVIEW_REMINDER_HOURS = 36
EMAIL_MAX_ATTEMPTS = 3
REMINDER_KINDS = ("follow_up_due", "interview_reminder")
LIST_LIMIT = 50


def application_intervention(attempt: dict, intervention: dict) -> dict | None:
    """Signale une candidature assistée en attente, avec déduplication par contenu de l'intervention."""
    application = db.get_application(attempt["application_id"])
    fingerprint = hashlib.sha256(json.dumps(intervention, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    payload = {"application_id": application["id"], "attempt_id": attempt["id"],
               "title": application.get("title"), "company": application.get("company"),
               "intervention_kind": intervention.get("kind"), "message": intervention.get("message") or ""}
    with db.connect() as c:
        created = _insert(c, "application_intervention", f"attempt:{attempt['id']}:{fingerprint}", payload)
    if created:
        deliver_desktop([created], "Une candidature assistée nécessite ton intervention.")
    return created


def application_submitted(attempt: dict) -> dict | None:
    """Confirme localement un envoi assisté détecté, une seule fois par tentative."""
    application = db.get_application(attempt["application_id"])
    payload = {"application_id": application["id"], "attempt_id": attempt["id"],
               "title": application.get("title"), "company": application.get("company")}
    with db.connect() as c:
        created = _insert(c, "application_submitted", f"attempt:{attempt['id']}:submitted", payload)
    if created:
        deliver_desktop([created], "Candidature envoyée et preuve archivée.")
    return created


def notify(message: str, title: str = "JobRadar") -> bool:
    """Notification de bureau (Linux : notify-send ; macOS : osascript ; ailleurs : rien).

    Le processus est lancé sans être attendu : une notification ne bloque
    jamais le pipeline. Le texte est passé en argument, jamais interpolé dans
    un script, ce qui évite toute injection AppleScript.
    """
    cmds = {
        "linux": ["notify-send", "--app-name=JobRadar", title, message],
        "darwin": ["osascript", "-e", "on run argv", "-e",
                   "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                   title, message],
    }
    cmd = cmds.get(sys.platform)
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def _row(r) -> dict:
    return {
        "id": r["id"],
        "kind": r["kind"],
        "status": r["status"],
        "offer_id": r["offer_id"],
        "payload": json.loads(r["payload_json"]),
        "created_at": r["created_at_utc"],
        "delivered_at": r["delivered_at_utc"],
    }


def _insert(c, kind: str, dedupe_key: str, payload: dict, offer_id: int | None = None) -> dict | None:
    """Crée la notification si sa clé est nouvelle ; renvoie None si elle existait déjà."""
    cur = c.execute(
        """INSERT OR IGNORE INTO notifications(kind, dedupe_key, offer_id, payload_json, created_at_utc)
           VALUES (?,?,?,?,?)""",
        (kind, dedupe_key, offer_id, json.dumps(payload, ensure_ascii=False), db.utc_iso(db.utcnow(), "created_at")),
    )
    if cur.rowcount != 1:
        return None
    return _row(c.execute("SELECT * FROM notifications WHERE id=?", (cur.lastrowid,)).fetchone())


# ---------------------------------------------------------------- alertes offres

def alert_high_score_offers(analyzed: list[dict]) -> list[dict]:
    """Alerte pour chaque offre nouvellement analysée assez bien notée, dans la zone et non exclue.

    ``analyzed`` contient l'offre (``id``, ``search_id``, ``title``, ``company``)
    fusionnée avec la réponse de l'IA (``score``, ``verdict``, ``match_reasons``, ``in_zone``).
    """
    min_score = settings().alert_min_score
    excluded = db.excluded_company_keys()
    created = []
    with db.connect() as c:
        for o in analyzed:
            if o.get("score") is None or o["score"] < min_score or o.get("in_zone") is False:
                continue
            if workflow.normalize_company(o.get("company") or "") in excluded:
                continue
            payload = {
                "title": o["title"],
                "company": o.get("company"),
                "score": o["score"],
                "verdict": o.get("verdict") or "",
                "reasons": list(o.get("match_reasons") or [])[:3],
                "search_id": o["search_id"],
                "offer_id": o["id"],
            }
            note = _insert(c, "high_score_offer", f"offer:{o['id']}", payload, offer_id=o["id"])
            if note:
                created.append(note)
    return created


def offers_desktop_text(created: list[dict]) -> str:
    """« 3 offres très pertinentes : Intrinsec (93), … » ; les entreprises sont triées par score."""
    best = sorted((n["payload"] for n in created), key=lambda p: -p["score"])
    names = ", ".join(f"{p['company'] or p['title']} ({p['score']})" for p in best[:3])
    more = "…" if len(best) > 3 else ""
    label = "offre très pertinente" if len(best) == 1 else "offres très pertinentes"
    return f"{len(best)} {label} : {names}{more}"


def deliver_desktop(created: list[dict], message: str) -> bool:
    """Envoie une seule notification de bureau pour ``created`` si l'utilisateur l'autorise."""
    if not created or not settings().alert_desktop:
        return False
    sent = notify(message)
    if sent:
        with db.connect() as c:
            c.executemany(
                "UPDATE notifications SET delivered_at_utc=? WHERE id=?",
                [(db.utc_iso(db.utcnow(), "delivered_at"), n["id"]) for n in created],
            )
    return sent


# ---------------------------------------------------------------- rappels

def _follow_up_due(at: datetime, zone) -> dict[str, dict]:
    out = {}
    for a in db.follow_ups(at)["due"]:
        due_day = db.parse_utc(a["follow_up"]["due_at"]).astimezone(zone).date().isoformat()
        out[f"followup:{a['id']}:{due_day}"] = {
            "application_id": a["id"], "title": a["title"], "company": a["company"],
            "applied_at": a["applied_at"], "due_at": a["follow_up"]["due_at"],
        }
    return out


def _interview_reminders(at: datetime) -> dict[str, dict]:
    with db.connect() as c:
        rows = c.execute(
            """SELECT i.id, i.application_id, i.title, i.starts_at_utc, i.location, a.company
               FROM interviews i JOIN applications a ON a.id = i.application_id
               WHERE i.status = 'scheduled'"""
        ).fetchall()
    out = {}
    for r in rows:
        start = db.parse_utc(r["starts_at_utc"])
        if start and at < start <= at + timedelta(hours=INTERVIEW_REMINDER_HOURS):
            out[f"interview:{r['id']}:{r['starts_at_utc']}"] = {
                "interview_id": r["id"], "application_id": r["application_id"], "title": r["title"],
                "company": r["company"], "starts_at": r["starts_at_utc"], "location": r["location"],
            }
    return out


def refresh_notifications(now: datetime | None = None) -> list[dict]:
    """Crée les rappels de relance et d'entretien manquants ; renvoie ceux créés à cet appel.

    Un rappel encore non lu devenu sans objet (relance faite, reportée ou
    désactivée ; entretien annulé, déplacé ou passé) est retiré de la boîte.
    """
    at = now or db.utcnow()
    zone = workflow.validate_timezone(settings().timezone)
    wanted = {
        "follow_up_due": _follow_up_due(at, zone),
        "interview_reminder": _interview_reminders(at),
    }
    created = []
    with db.connect() as c:
        for kind, items in wanted.items():
            for key, payload in items.items():
                note = _insert(c, kind, key, payload)
                if note:
                    created.append(note)
            stale = [
                r["id"] for r in c.execute(
                    "SELECT id, dedupe_key FROM notifications WHERE kind=? AND status='pending'", (kind,)
                ) if r["dedupe_key"] not in items
            ]
            c.executemany("UPDATE notifications SET status='dismissed' WHERE id=?", [(i,) for i in stale])
    return created


def deliver_pending_reminders() -> bool:
    """Notification de bureau agrégée pour les rappels non lus jamais montrés sur le bureau."""
    with db.connect() as c:
        rows = c.execute(
            f"""SELECT * FROM notifications WHERE status='pending' AND delivered_at_utc IS NULL
                AND kind IN ({','.join('?' * len(REMINDER_KINDS))})""",
            REMINDER_KINDS,
        ).fetchall()
    pending = [_row(r) for r in rows]
    follow, interviews = (sum(n["kind"] == k for n in pending) for k in REMINDER_KINDS)
    parts = []
    if follow:
        parts.append(f"{follow} relance{'s' if follow > 1 else ''} à faire")
    if interviews:
        parts.append(f"{interviews} entretien{'s' if interviews > 1 else ''} dans les {INTERVIEW_REMINDER_HOURS} h")
    return deliver_desktop(pending, "Rappels : " + ", ".join(parts)) if parts else False


# ---------------------------------------------------------------- récapitulatif par e-mail

def _email_item(r) -> dict:
    payload = json.loads(r["payload_json"])
    if r["kind"] == "high_score_offer":
        payload["url"] = r["offer_url"]
    return payload


def deliver_email_digest() -> dict:
    """Envoie un seul e-mail pour les alertes non lues pas encore envoyées ; ne lève jamais d'exception.

    Succès : ``emailed_at_utc`` est renseigné. Échec d'envoi : ``email_attempts``
    augmente et l'alerte est reprise à l'exécution suivante, jusqu'à
    EMAIL_MAX_ATTEMPTS tentatives. Un réglage incomplet (mot de passe introuvable
    par exemple) ne consomme pas de tentative. Renvoie ``{"sent": n, "error": message ou None}``.
    """
    at = db.utcnow()
    ids: list[int] = []
    try:
        conf = mailer.email_config()
        if not conf["enabled"] or not conf["kinds"]:
            return {"sent": 0, "error": None}
        with db.connect() as c:
            rows = c.execute(
                f"""SELECT n.*, o.url AS offer_url FROM notifications n LEFT JOIN offers o ON o.id = n.offer_id
                    WHERE n.status = 'pending' AND n.emailed_at_utc IS NULL AND n.email_attempts < ?
                    AND n.kind IN ({','.join('?' * len(conf['kinds']))})
                    ORDER BY n.created_at_utc, n.id""",
                (EMAIL_MAX_ATTEMPTS, *conf["kinds"]),
            ).fetchall()
        if not rows:
            return {"sent": 0, "error": None}
        ids = [r["id"] for r in rows]
        sections: dict[str, list[dict]] = {}
        for r in rows:
            sections.setdefault(r["kind"], []).append(_email_item(r))
        mailer.send_digest(conf, sections, at)
    except mailer.SendError as e:
        with db.connect() as c:
            c.executemany("UPDATE notifications SET email_attempts = email_attempts + 1, email_error=? WHERE id=?",
                          [(str(e), i) for i in ids])
        return _email_failed(str(e), at)
    except Exception as e:  # réglage incomplet, base indisponible… : jamais bloquant
        return _email_failed(str(e) if isinstance(e, ValueError) else f"Erreur interne : {type(e).__name__}", at)
    with db.connect() as c:
        c.executemany("UPDATE notifications SET emailed_at_utc=?, email_error=NULL WHERE id=?",
                      [(db.utc_iso(at, "emailed_at"), i) for i in ids])
    mailer.record_status(None, at)
    return {"sent": len(ids), "error": None}


def _email_failed(error: str, at: datetime) -> dict:
    try:
        mailer.record_status(error, at)
    except OSError:
        pass
    return {"sent": 0, "error": error}


# ---------------------------------------------------------------- boîte de notifications

def list_notifications(limit: int = LIST_LIMIT) -> list[dict]:
    """Notifications non lues d'abord, puis les plus récentes."""
    with db.connect() as c:
        rows = c.execute(
            """SELECT * FROM notifications
               ORDER BY status = 'pending' DESC, created_at_utc DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


def dismiss(notification_id: int) -> dict:
    with db.connect() as c:
        if c.execute("UPDATE notifications SET status='dismissed' WHERE id=?", (notification_id,)).rowcount != 1:
            raise db.NotFound("Notification introuvable.")
        return _row(c.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone())


def dismiss_all() -> int:
    with db.connect() as c:
        return c.execute("UPDATE notifications SET status='dismissed' WHERE status='pending'").rowcount
