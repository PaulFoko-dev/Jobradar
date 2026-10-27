"""Stockage SQLite : recherches, offres, règles par entreprise, candidatures, entretiens."""
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import config, workflow
from .calendar_export import build_interview_ics, safe_ics_filename
from .config import Settings, settings
from .migrations import migrate


class NotFound(LookupError):
    """Élément demandé absent de la base."""


class Conflict(RuntimeError):
    """Action incompatible avec l'état actuel de l'élément."""


def utcnow() -> datetime:
    """Instant courant ; point unique remplaçable dans les tests."""
    return datetime.now(timezone.utc)


def now() -> str:
    return utcnow().isoformat(timespec="seconds")


def utc_iso(value: str | datetime, field: str) -> str:
    """Valide un instant explicite et le normalise en UTC, sans déduire de fuseau."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} doit être une date-heure ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} doit inclure un fuseau horaire")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    """Relit un instant stocké par ``_utc`` ou ``now`` ; None si absent ou illisible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _source_instant(value: object) -> str | None:
    """Instant de publication fourni par une source : retenu seulement s'il porte un fuseau."""
    if isinstance(value, datetime | str) and value:
        try:
            return utc_iso(value, "published_at")
        except ValueError:
            return None
    return None


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date(value: object) -> date | None:
    """Date stricte AAAA-MM-JJ (les 10 premiers caractères d'une valeur de source), sinon None."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value[:10]):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@contextmanager
def connect():
    # Chemins relus à chaque connexion : JOBRADAR_DATA_DIR et les tests les redirigent.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with connect() as c:
        migrate(c)


def _search_row(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "form": json.loads(r["form_json"]),
        "prompt": r["prompt"],
        "criteria": json.loads(r["criteria_json"]),
        "frequency": r["frequency"],
        "created_at": r["created_at"],
        "last_run": r["last_run"],
        "auto_apply": {"enabled": bool(r["auto_apply_enabled"]), "min_score": r["auto_apply_min_score"],
                       "daily_limit": r["auto_apply_daily_limit"]},
    }


def list_searches() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM searches ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            s = _search_row(r)
            s["offer_count"] = c.execute(
                "SELECT COUNT(*) FROM offers WHERE search_id=? AND status!='ignored'", (r["id"],)
            ).fetchone()[0]
            out.append(s)
        return out


def get_search(search_id: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
        return _search_row(r) if r else None


def save_search(data: dict, search_id: int | None = None) -> int:
    vals = (
        data["name"],
        json.dumps(data.get("form", {}), ensure_ascii=False),
        data.get("prompt", ""),
        json.dumps(data.get("criteria", {}), ensure_ascii=False),
        data.get("frequency", "daily"),
    )
    with connect() as c:
        if search_id:
            c.execute(
                "UPDATE searches SET name=?, form_json=?, prompt=?, criteria_json=?, frequency=? WHERE id=?",
                (*vals, search_id),
            )
            return search_id
        cur = c.execute(
            "INSERT INTO searches(name, form_json, prompt, criteria_json, frequency, created_at) VALUES (?,?,?,?,?,?)",
            (*vals, now()),
        )
        return cur.lastrowid


def delete_search(search_id: int):
    """Supprime une recherche sans supprimer les candidatures déjà suivies.

    Une candidature contient sa propre copie des informations importantes. Son
    lien vers l'offre est donc détaché avant la suppression en cascade des
    offres, ce qui évite aussi tout identifiant orphelin sur les anciennes bases.
    """
    with connect() as c:
        c.execute(
            """UPDATE applications SET offer_id = NULL
               WHERE offer_id IN (SELECT id FROM offers WHERE search_id = ?)""",
            (search_id,),
        )
        c.execute("DELETE FROM offers WHERE search_id=?", (search_id,))
        c.execute("DELETE FROM runs WHERE search_id=?", (search_id,))
        c.execute("DELETE FROM searches WHERE id=?", (search_id,))


def mark_search_run(search_id: int):
    with connect() as c:
        c.execute("UPDATE searches SET last_run=? WHERE id=?", (now(), search_id))


def upsert_offer(search_id: int, o: dict) -> bool:
    """Insère l'offre ou fusionne ses liens avec un doublon existant. Retourne True si nouvelle."""
    with connect() as c:
        r = c.execute(
            "SELECT id, links_json, description, logo FROM offers WHERE search_id=? AND dedup_key=?",
            (search_id, o["dedup_key"]),
        ).fetchone()
        if r:
            links = json.loads(r["links_json"])
            known = {l["url"] for l in links}
            for l in o["links"]:
                if l["url"] not in known:
                    links.append(l)
            desc = r["description"] or ""
            if len(o.get("description") or "") > len(desc):
                desc = o["description"]
            c.execute(
                """UPDATE offers SET links_json=?, last_seen=?, description=?, logo=?,
                   published_at_utc=COALESCE(published_at_utc, ?) WHERE id=?""",
                (json.dumps(links, ensure_ascii=False), now(), desc, r["logo"] or o.get("logo"),
                 _source_instant(o.get("published_at")), r["id"]),
            )
            return False
        c.execute(
            """INSERT INTO offers(search_id, dedup_key, title, company, location, contract, date_posted,
               published_at_utc, description, url, links_json, first_seen, last_seen, logo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                search_id, o["dedup_key"], o["title"], o.get("company"), o.get("location"),
                o.get("contract"), o.get("date_posted"), _source_instant(o.get("published_at")),
                o.get("description"), o["url"], json.dumps(o["links"], ensure_ascii=False), now(), now(),
                o.get("logo"),
            ),
        )
        return True


def delete_offer(offer_id: int):
    with connect() as c:
        c.execute("DELETE FROM offers WHERE id=?", (offer_id,))


def merge_offer_into(keep_id: int, dup: dict):
    """Fusionne un doublon (liens, description la plus complète, logo) dans l'offre conservée, puis le supprime."""
    with connect() as c:
        r = c.execute("SELECT links_json, description, logo FROM offers WHERE id=?", (keep_id,)).fetchone()
        links = json.loads(r["links_json"])
        known = {l["url"] for l in links}
        links += [l for l in dup["links"] if l["url"] not in known]
        desc = max(r["description"] or "", dup.get("description") or "", key=len)
        c.execute(
            "UPDATE offers SET links_json=?, description=?, logo=? WHERE id=?",
            (json.dumps(links, ensure_ascii=False), desc, r["logo"] or dup.get("logo"), keep_id),
        )
        c.execute("DELETE FROM offers WHERE id=?", (dup["id"],))


def set_dedup_key(offer_id: int, key: str):
    with connect() as c:
        c.execute("UPDATE offers SET dedup_key=? WHERE id=?", (key, offer_id))


def offers_to_score(search_id: int, limit: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM offers WHERE search_id=? AND analysis_json IS NULL ORDER BY first_seen DESC LIMIT ?",
            (search_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _published(row) -> date | datetime | None:
    """Instant de publication s'il est connu avec fuseau, sinon la date seule de la source."""
    instant = parse_utc(row["published_at_utc"])
    return instant if instant is not None else _date(row["date_posted"])


def _add_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 février
        return day + timedelta(days=365)


def _deadline_columns(analysis: dict, published: date | None, today: date) -> tuple:
    """Date limite extraite par l'IA, retenue seulement si elle est stricte et plausible.

    Une date à plus d'un an, ou antérieure à la publication (moins un jour de
    marge pour les décalages de fuseau), est presque toujours une confusion
    avec une autre date de l'offre : toutes les colonnes restent alors NULL.
    """
    empty = (None, None, None, None, None)
    raw = analysis.get("application_deadline")
    if not isinstance(raw, str) or not _ISO_DATE.fullmatch(raw.strip()):
        return empty
    deadline = _date(raw.strip())
    if deadline is None or deadline > _add_year(today):
        return empty
    if published is not None and deadline < published - timedelta(days=1):
        return empty
    evidence = analysis.get("deadline_evidence")
    evidence = " ".join(evidence.split())[:120] if isinstance(evidence, str) else ""
    confidence = analysis.get("deadline_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not math.isfinite(confidence):
        confidence = None
    else:
        confidence = min(1.0, max(0.0, float(confidence)))
    return deadline.isoformat(), evidence or None, "ai", utc_iso(utcnow(), "deadline_extracted_at"), confidence


def save_analysis(offer_id: int, analysis: dict):
    zone = workflow.validate_timezone(settings().timezone)
    today = utcnow().astimezone(zone).date()
    with connect() as c:
        row = c.execute("SELECT published_at_utc, date_posted FROM offers WHERE id=?", (offer_id,)).fetchone()
        if row is None:
            return
        published = _published(row)
        if isinstance(published, datetime):
            published = published.astimezone(zone).date()
        c.execute(
            """UPDATE offers SET analysis_json=?, score=?, in_zone=?, application_deadline_date=?,
               deadline_raw=?, deadline_source=?, deadline_extracted_at_utc=?, deadline_confidence=?
               WHERE id=?""",
            (
                json.dumps(analysis, ensure_ascii=False),
                int(analysis.get("score", 0)),
                1 if analysis.get("in_zone") else 0,
                *_deadline_columns(analysis, published, today),
                offer_id,
            ),
        )


def _offer_row(r) -> dict:
    d = dict(r)
    d["links"] = json.loads(d.pop("links_json"))
    a = d.pop("analysis_json")
    d["analysis"] = json.loads(a) if a else None
    d["in_zone"] = None if d["in_zone"] is None else bool(d["in_zone"])
    d["viewed"] = bool(d["viewed"])
    return d


ALREADY_APPLIED_DAYS = 180


def _sent_applications(c, since: datetime) -> dict[str, list[dict]]:
    """Candidatures envoyées depuis ``since``, groupées par clé d'entreprise, plus récentes d'abord."""
    rows = c.execute(
        "SELECT id, offer_id, title, company, status, applied_at_utc FROM applications "
        "WHERE applied_at_utc IS NOT NULL AND company IS NOT NULL"
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        applied = parse_utc(r["applied_at_utc"])
        key = workflow.normalize_company(r["company"])
        if applied is None or applied < since or not key:
            continue
        grouped.setdefault(key, []).append({**dict(r), "_applied": applied})
    for apps in grouped.values():
        apps.sort(key=lambda a: a["_applied"], reverse=True)
    return grouped


def _deadline_view(o: dict, decision: workflow.DeadlineDecision, at: datetime, st: Settings) -> dict:
    return {
        "date": o["application_deadline_date"],
        "state": decision.state.value,
        "urgent": workflow.is_urgent(decision, now=at, timezone_name=st.timezone,
                                     urgent_days=st.deadline_urgent_days),
        "days_left": workflow.days_left(decision, now=at, timezone_name=st.timezone),
        "evidence": o["deadline_raw"],
    }


def list_offers(search_id: int) -> list[dict]:
    st, at = settings(), utcnow()
    with connect() as c:
        rows = c.execute(
            """SELECT o.*, a.id AS app_id, a.status AS app_status FROM offers o
               LEFT JOIN applications a ON a.offer_id = o.id
               WHERE o.search_id=? ORDER BY o.score DESC NULLS LAST, o.first_seen DESC""",
            (search_id,),
        ).fetchall()
        logos = company_logos(c)
        rules = company_dispositions(c)
        sent = _sent_applications(c, at - timedelta(days=ALREADY_APPLIED_DAYS))
    out, keys = [], {}
    for r in rows:
        o = _offer_row(r)
        o["logo"] = o["logo"] or logos.get((o["company"] or "").strip().lower())
        company = workflow.normalize_company(o["company"] or "")
        o["company_rule"] = rules.get(company, "neutral")
        decision = workflow.deadline_status(_date(o["application_deadline_date"]), now=at, timezone_name=st.timezone)
        o["deadline"] = _deadline_view(o, decision, at, st)
        o["nationality_required"] = workflow.nationality_requirement(f"{o['title'] or ''}.\n{o['description'] or ''}")
        other = next((a for a in sent.get(company, []) if a["offer_id"] != o["id"]), None) if company else None
        o["already_applied"] = None if other is None else {
            "application_id": other["id"], "title": other["title"], "status": other["status"],
            "applied_at": other["applied_at_utc"],
        }
        score = o["score"]
        keys[o["id"]] = workflow.priority(
            item_id=o["id"],
            excluded=o["company_rule"] == "excluded",
            deadline=_date(o["application_deadline_date"]),
            favorite=o["company_rule"] == "favorite",
            score=None if score is None else min(100, max(0, score)),
            published_at=_published(r),
            now=at,
            timezone_name=st.timezone,
            urgent_days=st.deadline_urgent_days,
        ).key
        out.append(o)
    rank = {oid: i for i, oid in enumerate(sorted(keys, key=keys.__getitem__))}
    for o in out:
        o["priority_order"] = rank[o["id"]]
    return out


# ---------------------------------------------------------------- règles par entreprise

COMPANY_DISPOSITIONS = ("favorite", "excluded", "neutral")


def _rule_row(r) -> dict:
    return {
        "company_key": r["company_key"],
        "display_name": r["display_name"],
        "disposition": r["disposition"],
        "created_at": r["created_at_utc"],
        "updated_at": r["updated_at_utc"],
    }


def company_dispositions(c) -> dict[str, str]:
    return {r["company_key"]: r["disposition"] for r in c.execute("SELECT company_key, disposition FROM company_rules")}


def excluded_company_keys() -> set[str]:
    with connect() as c:
        return {k for k, v in company_dispositions(c).items() if v == "excluded"}


def list_company_rules() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM company_rules ORDER BY display_name COLLATE NOCASE").fetchall()
        return [_rule_row(r) for r in rows]


def set_company_rule(company: str, disposition: str) -> dict:
    """Enregistre la règle d'une entreprise ; ``neutral`` supprime simplement la règle existante."""
    if disposition not in COMPANY_DISPOSITIONS:
        raise ValueError(f"Disposition inconnue : {disposition!r} (favorite, excluded ou neutral).")
    if not isinstance(company, str):
        raise ValueError("Le nom de l'entreprise est obligatoire.")
    display = " ".join(company.split())
    key = workflow.normalize_company(display)
    if not key:
        raise ValueError("Le nom de l'entreprise est obligatoire.")
    if len(display) > 200:
        raise ValueError("Le nom de l'entreprise ne doit pas dépasser 200 caractères.")
    ts = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        if disposition == "neutral":
            c.execute("DELETE FROM company_rules WHERE company_key=?", (key,))
            return {"company_key": key, "display_name": display, "disposition": "neutral",
                    "created_at": None, "updated_at": None}
        c.execute(
            """INSERT INTO company_rules(company_key, display_name, disposition, created_at_utc, updated_at_utc)
               VALUES (?,?,?,?,?)
               ON CONFLICT(company_key) DO UPDATE SET display_name=excluded.display_name,
                   disposition=excluded.disposition, updated_at_utc=excluded.updated_at_utc""",
            (key, display, disposition, ts, ts),
        )
        return _rule_row(c.execute("SELECT * FROM company_rules WHERE company_key=?", (key,)).fetchone())


def company_logos(c) -> dict:
    """Logo connu par entreprise : une offre sans logo reprend celui d'une autre offre de la même entreprise."""
    rows = c.execute("SELECT company, logo FROM offers WHERE logo IS NOT NULL AND company IS NOT NULL").fetchall()
    return {r["company"].strip().lower(): r["logo"] for r in rows}


# ---------------------------------------------------------------- suivi des candidatures

APP_STATUSES = ["consultee", "envoyee", "relance", "entretien_rh", "entretien_tech", "retenu", "refuse", "abandon"]
# Une relance ne fait reculer qu'une candidature encore sans réponse.
FOLLOW_UP_SENDABLE = {"consultee", "envoyee", "relance"}
UPCOMING_FOLLOW_UP_DAYS = 3


def _app_row(r) -> dict:
    d = dict(r)
    d["history"] = json.loads(d.pop("history_json"))
    d["follow_up_disabled"] = bool(d["follow_up_disabled"])
    d["applied_at"] = d["applied_at_utc"]
    d.pop("offer_snapshot_json", None)  # volumineux : servi par une route dédiée
    d["offer_online"] = None if d.get("offer_online") is None else bool(d["offer_online"])
    return d


def follow_up_view(a: dict, at: datetime, st: Settings) -> dict:
    """État de relance d'une candidature, report et désactivation compris."""
    def view(due: datetime | None, is_due: bool, reason: str) -> dict:
        return {"due_at": None if due is None else utc_iso(due, "due_at"), "is_due": is_due, "reason": reason}

    if a["follow_up_disabled"]:
        return view(None, False, "Rappel de relance désactivé.")
    sent = parse_utc(a["applied_at_utc"])
    if sent is None:
        return view(None, False, "Date d'envoi inconnue.")
    try:
        # min() : une horloge légèrement décalée ne doit pas rendre la candidature invalide.
        decision = workflow.follow_up_status(sent_at=min(sent, at), status=a["status"], now=at,
                                             timezone_name=st.timezone, delay_days=st.follow_up_delay_days)
    except ValueError as exc:
        return view(None, False, str(exc))
    if decision.due_at is None:
        return view(None, False, decision.reason)
    snoozed = parse_utc(a["follow_up_snoozed_until_utc"])
    if snoozed is None or snoozed <= decision.due_at:
        return view(decision.due_at, decision.is_due, decision.reason)
    if at >= snoozed:
        return view(snoozed, True, "La relance reportée est due.")
    local = snoozed.astimezone(workflow.validate_timezone(st.timezone)).date().isoformat()
    return view(snoozed, False, f"Relance reportée au {local}.")


def _enrich(c, rows, at: datetime | None = None) -> list[dict]:
    st, at = settings(), at or utcnow()
    logos = company_logos(c)
    counts = dict(c.execute(
        "SELECT application_id, COUNT(*) FROM interviews WHERE status='scheduled' GROUP BY application_id"
    ).fetchall())
    out = []
    for r in rows:
        a = _app_row(r)
        a["logo"] = a["logo"] or logos.get((a["company"] or "").strip().lower())
        a["follow_up"] = follow_up_view(a, at, st)
        a["interviews_count"] = counts.get(a["id"], 0)
        out.append(a)
    return out


def _get_app(c, app_id: int):
    r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if r is None:
        raise NotFound("Candidature introuvable.")
    return r


def _one(c, app_id: int) -> dict:
    return _enrich(c, [_get_app(c, app_id)])[0]


def list_applications() -> list[dict]:
    with connect() as c:
        return _enrich(c, c.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall())


def get_application(app_id: int) -> dict:
    with connect() as c:
        return _one(c, app_id)


def add_event(c, app_id: int, event_type: str, payload: dict) -> None:
    ts = utc_iso(utcnow(), "occurred_at")
    c.execute(
        """INSERT INTO application_events(application_id, event_type, occurred_at_utc, payload_json, created_at_utc)
           VALUES (?,?,?,?,?)""",
        (app_id, event_type, ts, json.dumps(payload, ensure_ascii=False), ts),
    )


def list_events(app_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, app_id)
        rows = c.execute(
            "SELECT * FROM application_events WHERE application_id=? ORDER BY occurred_at_utc, id", (app_id,)
        ).fetchall()
        return [{"id": r["id"], "type": r["event_type"], "at": r["occurred_at_utc"],
                 "payload": json.loads(r["payload_json"])} for r in rows]


def _check_status(status: str) -> str:
    if status not in APP_STATUSES:
        raise ValueError(f"Statut de candidature inconnu : {status!r}.")
    return status


def create_application(data: dict) -> dict:
    """Ajoute une candidature au suivi. Pour une offre déjà suivie, renvoie l'existante sans la modifier."""
    with connect() as c:
        if data.get("offer_id"):
            r = c.execute("SELECT * FROM applications WHERE offer_id=?", (data["offer_id"],)).fetchone()
            if r:
                return _enrich(c, [r])[0]
            o = c.execute("SELECT * FROM offers WHERE id=?", (data["offer_id"],)).fetchone()
            if not o:
                raise NotFound("Offre introuvable.")
            links = json.loads(o["links_json"])
            apply = next((l["url"] for l in links if l.get("apply")), o["url"])
            data = {**data, "title": o["title"], "company": o["company"], "location": o["location"],
                    "url": apply, "score": o["score"], "logo": o["logo"]}
        status = _check_status(data.get("status") or "consultee")
        ts = now()
        applied = utc_iso(utcnow(), "applied_at") if status in workflow.SENT_STATUSES else None
        cur = c.execute(
            """INSERT INTO applications(offer_id, title, company, location, url, score, logo, status, notes,
               history_json, created_at, updated_at, applied_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.get("offer_id"), data["title"], data.get("company"), data.get("location"), data.get("url"),
             data.get("score"), data.get("logo"), status, data.get("notes", ""),
             json.dumps([{"status": status, "at": ts}]), ts, ts, applied),
        )
        add_event(c, cur.lastrowid, "status_changed", {"from": None, "to": status})
        return _one(c, cur.lastrowid)


def _set_status(c, r, status: str) -> None:
    """Change le statut : historique JSON, événement, et date d'envoi au premier statut « envoyé »."""
    if status == r["status"]:
        return
    ts = now()
    history = json.loads(r["history_json"]) + [{"status": status, "at": ts}]
    applied = r["applied_at_utc"]
    if applied is None and status in workflow.SENT_STATUSES:
        applied = utc_iso(utcnow(), "applied_at")
    c.execute(
        "UPDATE applications SET status=?, history_json=?, applied_at_utc=?, updated_at=? WHERE id=?",
        (status, json.dumps(history), applied, ts, r["id"]),
    )
    add_event(c, r["id"], "status_changed", {"from": r["status"], "to": status})


def update_application(app_id: int, fields: dict) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not r:
            return None
        if fields.get("status") is not None:
            _set_status(c, r, _check_status(fields["status"]))
        if fields.get("notes") is not None:
            c.execute("UPDATE applications SET notes=?, updated_at=? WHERE id=?", (fields["notes"], now(), app_id))
        return _one(c, app_id)


def delete_application(app_id: int):
    with connect() as c:
        c.execute("DELETE FROM applications WHERE id=?", (app_id,))


# ---------------------------------------------------------------- archives des candidatures

ARCHIVE_FIELDS = ("archive_id", "archive_dir", "capture_status", "capture_level", "capture_error",
                  "captured_at_utc", "offer_online", "offer_checked_at_utc", "offer_snapshot_json")


def set_archive_fields(app_id: int, fields: dict) -> None:
    """Met à jour les colonnes d'archive ; une candidature disparue entre-temps est ignorée."""
    unknown = set(fields) - set(ARCHIVE_FIELDS)
    if unknown:
        raise ValueError(f"Champs d'archive inconnus : {', '.join(sorted(unknown))}.")
    if fields:
        with connect() as c:
            c.execute(f"UPDATE applications SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                      (*fields.values(), app_id))


def archive_row(app_id: int) -> dict:
    """Ligne brute de la candidature (instantané compris) et son offre d'origine si elle existe encore."""
    with connect() as c:
        r = dict(_get_app(c, app_id))
        o = c.execute("SELECT * FROM offers WHERE id=?", (r["offer_id"],)).fetchone() if r["offer_id"] else None
        r["offer"] = dict(o) if o else None
        return r


def claim_archive(app_id: int, previous_id: str | None, new_id: str) -> bool:
    """Réserve la création du dossier : un seul processus gagne (serveur et ``cli refresh`` concurrents)."""
    with connect() as c:
        return c.execute(
            "UPDATE applications SET archive_id=? WHERE id=? AND archive_dir IS NULL AND archive_id IS ?",
            (new_id, app_id, previous_id),
        ).rowcount == 1


def request_capture(app_id: int) -> None:
    """Passe la capture en attente ; refusée si une capture est déjà en attente ou en cours."""
    with connect() as c:
        r = _get_app(c, app_id)
        if r["archive_dir"] is None:
            raise NotFound("Archive introuvable pour cette candidature.")
        done = c.execute(
            """UPDATE applications SET capture_status='pending', capture_error=NULL
               WHERE id=? AND (capture_status IS NULL OR capture_status NOT IN ('pending', 'running'))""",
            (app_id,),
        ).rowcount
        if not done:
            raise Conflict("Une capture de cette offre est déjà en attente ou en cours.")


def start_capture(app_id: int) -> bool:
    """Passe une capture de « pending » à « running » ; faux si un autre fil l'a déjà prise."""
    with connect() as c:
        return c.execute("UPDATE applications SET capture_status='running' WHERE id=? AND capture_status='pending'",
                         (app_id,)).rowcount == 1


def pending_captures(reset_running: bool = False) -> list[int]:
    """Captures à (re)lancer. ``reset_running`` : une capture interrompue par un arrêt repart en attente."""
    with connect() as c:
        if reset_running:
            c.execute("UPDATE applications SET capture_status='pending' WHERE capture_status='running'")
        return [r[0] for r in c.execute(
            "SELECT id FROM applications WHERE capture_status='pending' AND archive_dir IS NOT NULL ORDER BY id")]


def applications_without_archive() -> list[int]:
    with connect() as c:
        return [r[0] for r in c.execute("SELECT id FROM applications WHERE archive_dir IS NULL ORDER BY id")]


def captured_pages() -> list[dict]:
    """Captures terminées dont la copie vient de la page d'origine."""
    with connect() as c:
        return [dict(r) for r in c.execute(
            """SELECT id, archive_dir FROM applications WHERE capture_status='done' AND archive_dir IS NOT NULL
               AND capture_level IN ('page', 'page_navigateur_dedie') ORDER BY id""")]


def known_archive_ids() -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT archive_id FROM applications WHERE archive_id IS NOT NULL")}


def applications_to_check_online() -> list[dict]:
    """Candidatures encore ouvertes dont le lien d'origine peut être vérifié."""
    with connect() as c:
        rows = c.execute(
            """SELECT id, url, offer_online, offer_checked_at_utc FROM applications
               WHERE status NOT IN ('retenu', 'refuse', 'abandon') AND url LIKE 'http%' ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]


def restore_application(data: dict) -> int:
    """Recrée une candidature lue dans un ``candidature.json`` (réindexation), entretiens compris.

    Le lien vers l'offre n'est pas rétabli : l'identifiant d'offre d'une autre base n'a pas de sens ici.
    """
    status = data.get("status") if data.get("status") in APP_STATUSES else "consultee"
    ts = now()
    history = data.get("history") if isinstance(data.get("history"), list) else []
    values = {
        "title": str(data.get("title") or "Candidature sans intitulé"),
        "company": data.get("company"), "location": data.get("location"), "url": data.get("url"),
        "score": data.get("score") if isinstance(data.get("score"), int) else None,
        "status": status, "notes": str(data.get("notes") or ""),
        "history_json": json.dumps(history, ensure_ascii=False),
        "created_at": str(data.get("created_at") or ts), "updated_at": str(data.get("updated_at") or ts),
        "applied_at_utc": data.get("applied_at_utc"),
    } | {k: data.get(k) for k in ARCHIVE_FIELDS if k not in ("offer_online", "offer_checked_at_utc")}
    if values["capture_status"] in ("pending", "running"):
        values["capture_status"] = "failed"
        values["capture_error"] = "Capture interrompue avant la réindexation."
    with connect() as c:
        cur = c.execute(f"INSERT INTO applications({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values()))
        add_event(c, cur.lastrowid, "note", {"text": "Candidature reconstruite depuis son dossier d'archive."})
        for interview in data.get("interviews") or []:
            _restore_interview(c, cur.lastrowid, interview)
        return cur.lastrowid


def _restore_interview(c, app_id: int, i: dict) -> None:
    try:
        c.execute(
            """INSERT OR IGNORE INTO interviews(application_id, uid, title, starts_at_utc, ends_at_utc, timezone,
               location, meeting_url, notes, status, reminder_minutes, sequence, created_at_utc, updated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (app_id, i["uid"], i["title"], i["starts_at_utc"], i["ends_at_utc"], i["timezone"], i.get("location"),
             i.get("meeting_url"), i.get("notes") or "", i.get("status", "scheduled"),
             i.get("reminder_minutes", 1440), i.get("sequence", 0), i["created_at"], i["updated_at"]),
        )
    except (KeyError, TypeError, sqlite3.IntegrityError):
        pass  # entretien incomplet ou incohérent : la candidature reste restaurée


# ---------------------------------------------------------------- relances

def follow_ups(at: datetime | None = None) -> dict:
    """Relances dues et relances prévues dans les prochains jours, par échéance croissante."""
    at = at or utcnow()
    horizon = at + timedelta(days=UPCOMING_FOLLOW_UP_DAYS)
    due, upcoming = [], []
    with connect() as c:
        apps = _enrich(c, c.execute("SELECT * FROM applications").fetchall(), at)
    for a in apps:
        fu = a["follow_up"]
        if fu["due_at"] is None:
            continue
        if fu["is_due"]:
            due.append(a)
        elif parse_utc(fu["due_at"]) <= horizon:
            upcoming.append(a)
    for group in (due, upcoming):
        group.sort(key=lambda a: (a["follow_up"]["due_at"], a["id"]))
    return {"due": due, "upcoming": upcoming}


def follow_up_context(app_id: int) -> dict:
    """Informations transmises à l'IA pour rédiger une relance ; jamais le nom du candidat."""
    with connect() as c:
        r = _get_app(c, app_id)
        context = {
            "intitule": r["title"],
            "entreprise": r["company"] or "",
            "date_envoi": (r["applied_at_utc"] or "")[:10],
            "notes": r["notes"] or "",
        }
        o = c.execute("SELECT analysis_json FROM offers WHERE id=?", (r["offer_id"],)).fetchone() \
            if r["offer_id"] else None
        analysis = json.loads(o["analysis_json"]) if o and o["analysis_json"] else {}
        context["resume_offre"] = analysis.get("summary") or ""
        context["missions"] = analysis.get("missions") or []
        return context


def record_follow_up_draft(app_id: int, draft: dict) -> None:
    with connect() as c:
        _get_app(c, app_id)
        add_event(c, app_id, "follow_up_draft_created", draft)


def mark_follow_up_sent(app_id: int) -> dict:
    with connect() as c:
        r = _get_app(c, app_id)
        if r["status"] not in FOLLOW_UP_SENDABLE:
            raise Conflict(f"La candidature est au statut « {r['status']} » : une relance n'est plus attendue.")
        _set_status(c, r, "relance")
        add_event(c, app_id, "follow_up_sent", {})
        return _one(c, app_id)


def snooze_follow_up(app_id: int, days: int) -> dict:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("Le report doit être compris entre 1 et 30 jours.")
    with connect() as c:
        _get_app(c, app_id)
        until = utc_iso(utcnow() + timedelta(days=days), "follow_up_snoozed_until")
        c.execute("UPDATE applications SET follow_up_snoozed_until_utc=?, updated_at=? WHERE id=?",
                  (until, now(), app_id))
        return _one(c, app_id)


def set_follow_up_disabled(app_id: int, disabled: bool) -> dict:
    with connect() as c:
        _get_app(c, app_id)
        c.execute("UPDATE applications SET follow_up_disabled=?, updated_at=? WHERE id=?",
                  (int(disabled), now(), app_id))
        return _one(c, app_id)


# ---------------------------------------------------------------- entretiens

INTERVIEW_FIELDS = ("title", "starts_at", "duration_minutes", "timezone", "location", "meeting_url", "notes",
                    "reminder_minutes")


def _interview_row(r) -> dict:
    zone = workflow.validate_timezone(r["timezone"])
    start, end = parse_utc(r["starts_at_utc"]), parse_utc(r["ends_at_utc"])
    return {
        "id": r["id"],
        "application_id": r["application_id"],
        "uid": r["uid"],
        "title": r["title"],
        "starts_at": start.astimezone(zone).isoformat(),
        "ends_at": end.astimezone(zone).isoformat(),
        "starts_at_utc": r["starts_at_utc"],
        "ends_at_utc": r["ends_at_utc"],
        "duration_minutes": int((end - start).total_seconds() // 60),
        "timezone": r["timezone"],
        "location": r["location"],
        "meeting_url": r["meeting_url"],
        "notes": r["notes"],
        "status": r["status"],
        "reminder_minutes": r["reminder_minutes"],
        "sequence": r["sequence"],
        "created_at": r["created_at_utc"],
        "updated_at": r["updated_at_utc"],
    }


def _optional_text(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} doit être un texte.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} ne doit pas dépasser {limit} caractères.")
    return value or None


def _interview_values(merged: dict, app, default_timezone: str) -> dict:
    """Valide et normalise les champs d'un entretien (création ou modification complète)."""
    start = merged.get("starts_at")
    if isinstance(start, str):
        start = parse_utc(utc_iso(start, "starts_at"))
    if not isinstance(start, datetime) or start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("starts_at doit être une date-heure ISO 8601 avec décalage horaire.")
    duration = merged.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 15 <= duration <= 480:
        raise ValueError("La durée doit être comprise entre 15 et 480 minutes.")
    reminder = merged.get("reminder_minutes")
    if isinstance(reminder, bool) or not isinstance(reminder, int) or not 0 <= reminder <= 20160:
        raise ValueError("Le rappel doit être compris entre 0 et 20160 minutes (14 jours).")
    zone = merged.get("timezone") or default_timezone
    workflow.validate_timezone(zone)
    url = _optional_text(merged.get("meeting_url"), "Le lien de visioconférence", 2000)
    if url is not None:
        workflow.validate_http_url(url)
    title = _optional_text(merged.get("title"), "Le titre", 200)
    return {
        "title": title or f"Entretien — {app['company'] or app['title']}",
        "starts_at_utc": utc_iso(start, "starts_at"),
        "ends_at_utc": utc_iso(start + timedelta(minutes=duration), "ends_at"),
        "timezone": zone,
        "location": _optional_text(merged.get("location"), "Le lieu", 300),
        "meeting_url": url,
        "notes": _optional_text(merged.get("notes"), "Les notes", 5000) or "",
        "reminder_minutes": reminder,
    }


def list_interviews(app_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, app_id)
        rows = c.execute("SELECT * FROM interviews WHERE application_id=? ORDER BY starts_at_utc, id",
                         (app_id,)).fetchall()
        return [_interview_row(r) for r in rows]


def _get_interview(c, interview_id: int):
    r = c.execute("SELECT * FROM interviews WHERE id=?", (interview_id,)).fetchone()
    if r is None:
        raise NotFound("Entretien introuvable.")
    return r


def get_interview(interview_id: int) -> dict:
    with connect() as c:
        return _interview_row(_get_interview(c, interview_id))


def create_interview(app_id: int, data: dict) -> dict:
    merged = {"duration_minutes": 60, "reminder_minutes": 1440} | {k: v for k, v in data.items() if v is not None}
    with connect() as c:
        app = _get_app(c, app_id)
        values = _interview_values(merged, app, settings().timezone)
        ts = utc_iso(utcnow(), "created_at")
        cur = c.execute(
            """INSERT INTO interviews(application_id, uid, title, starts_at_utc, ends_at_utc, timezone, location,
               meeting_url, notes, reminder_minutes, created_at_utc, updated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (app_id, f"{uuid.uuid4()}@jobradar.local", values["title"], values["starts_at_utc"],
             values["ends_at_utc"], values["timezone"], values["location"], values["meeting_url"], values["notes"],
             values["reminder_minutes"], ts, ts),
        )
        add_event(c, app_id, "interview_scheduled",
                  {"interview_id": cur.lastrowid, "title": values["title"], "starts_at": values["starts_at_utc"]})
        return _interview_row(_get_interview(c, cur.lastrowid))


def update_interview(interview_id: int, fields: dict) -> dict:
    """Modifie les champs fournis ; ``None`` efface un champ facultatif (titre : titre par défaut)."""
    unknown = set(fields) - set(INTERVIEW_FIELDS)
    if unknown:
        raise ValueError(f"Champs inconnus : {', '.join(sorted(unknown))}.")
    with connect() as c:
        r = _get_interview(c, interview_id)
        if r["status"] == "cancelled":
            raise Conflict("Cet entretien est annulé : crée un nouvel entretien.")
        current = _interview_row(r)
        merged = {k: current[k] for k in INTERVIEW_FIELDS} | fields
        for key in ("duration_minutes", "reminder_minutes", "starts_at", "timezone"):
            if merged[key] is None:
                merged[key] = current[key]
        values = _interview_values(merged, _get_app(c, r["application_id"]), current["timezone"])
        sets = ", ".join(f"{k}=?" for k in values)
        c.execute(
            f"UPDATE interviews SET {sets}, sequence=sequence+1, updated_at_utc=? WHERE id=?",
            (*values.values(), utc_iso(utcnow(), "updated_at"), interview_id),
        )
        return _interview_row(_get_interview(c, interview_id))


def cancel_interview(interview_id: int) -> dict:
    """Annule sans supprimer : l'export suivant porte METHOD:CANCEL et met à jour l'agenda."""
    with connect() as c:
        r = _get_interview(c, interview_id)
        if r["status"] != "cancelled":
            c.execute(
                "UPDATE interviews SET status='cancelled', sequence=sequence+1, updated_at_utc=? WHERE id=?",
                (utc_iso(utcnow(), "updated_at"), interview_id),
            )
        return _interview_row(_get_interview(c, interview_id))


def interview_calendar(interview_id: int) -> tuple[str, bytes]:
    """Nom de fichier et contenu .ics d'un entretien ; aucun chemin local n'y figure."""
    with connect() as c:
        r = _get_interview(c, interview_id)
        app = _get_app(c, r["application_id"])
    interview = _interview_row(r)
    start = parse_utc(r["starts_at_utc"])
    offer_url = app["url"]
    try:
        workflow.validate_http_url(offer_url)
    except ValueError:
        offer_url = None
    lines = [f"Poste : {app['title']}"]
    lines += [f"Entreprise : {app['company']}"] if app["company"] else []
    lines += [f"Offre : {offer_url}"] if offer_url else []
    lines += [f"Visioconférence : {r['meeting_url']}"] if r["meeting_url"] else []
    lines += ["", r["notes"]] if r["notes"] else []
    content = build_interview_ics(
        uid=r["uid"],
        summary=r["title"],
        start=start,
        end=parse_utc(r["ends_at_utc"]),
        dtstamp=utcnow(),
        last_modified=parse_utc(r["updated_at_utc"]),
        sequence=r["sequence"],
        cancelled=r["status"] == "cancelled",
        description="\n".join(lines),
        location=r["location"],
        preparation_url=r["meeting_url"],
        reminder_before=timedelta(minutes=r["reminder_minutes"]) if r["reminder_minutes"] else None,
    )
    local_day = interview["starts_at"][:10]
    return safe_ics_filename(f"Entretien {app['company'] or app['title']} {local_day}"), content


def update_offer(offer_id: int, fields: dict):
    allowed = {k: v for k, v in fields.items() if k in ("status", "viewed")}
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    with connect() as c:
        c.execute(f"UPDATE offers SET {sets} WHERE id=?", (*allowed.values(), offer_id))


def start_run(search_id: int) -> int:
    with connect() as c:
        return c.execute(
            "INSERT INTO runs(search_id, started_at, status) VALUES (?,?,'running')", (search_id, now())
        ).lastrowid


def finish_run(run_id: int, status: str, stats: dict, log: str):
    with connect() as c:
        c.execute(
            "UPDATE runs SET finished_at=?, status=?, stats_json=?, log=? WHERE id=?",
            (now(), status, json.dumps(stats), log, run_id),
        )


# ---------------------------------------------------------------- documents de candidature

def _document_row(row) -> dict:
    data = dict(row)
    data["composition"] = json.loads(data.pop("composition_json"))
    data["template"] = json.loads(data.pop("template_json"))
    data["ats"] = json.loads(data.pop("ats_json"))
    data["files"] = json.loads(data.pop("files_json"))
    return data


def next_document_version(application_id: int) -> int:
    with connect() as c:
        _get_app(c, application_id)
        return c.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM documents WHERE application_id=?",
            (application_id,),
        ).fetchone()[0]


def create_document(application_id: int, kind: str, version: int, composition: dict,
                    template: dict, ats: dict, files: list[dict]) -> dict:
    if kind not in {"cv", "lettre"}:
        raise ValueError("Type de document inconnu.")
    timestamp = utc_iso(utcnow(), "created_at")
    with connect() as c:
        _get_app(c, application_id)
        cur = c.execute(
            """INSERT INTO documents(application_id, kind, version, composition_json, template_json,
               ats_json, status, files_json, created_at_utc, updated_at_utc)
               VALUES (?,?,?,?,?,?,'brouillon',?,?,?)""",
            (application_id, kind, version, json.dumps(composition, ensure_ascii=False),
             json.dumps(template, ensure_ascii=False), json.dumps(ats, ensure_ascii=False),
             json.dumps(files, ensure_ascii=False), timestamp, timestamp),
        )
        return _document_row(c.execute("SELECT * FROM documents WHERE id=?", (cur.lastrowid,)).fetchone())


def list_documents(application_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, application_id)
        rows = c.execute(
            "SELECT * FROM documents WHERE application_id=? ORDER BY version DESC, kind, id DESC",
            (application_id,),
        ).fetchall()
        return [_document_row(row) for row in rows]


def get_document(document_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise NotFound("Document de candidature introuvable.")
        return _document_row(row)


def update_document(document_id: int, *, composition: dict, template: dict,
                    ats: dict, files: list[dict]) -> dict:
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        if c.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone() is None:
            raise NotFound("Document de candidature introuvable.")
        c.execute(
            """UPDATE documents SET composition_json=?, template_json=?, ats_json=?, files_json=?,
               status='brouillon', updated_at_utc=? WHERE id=?""",
            (json.dumps(composition, ensure_ascii=False), json.dumps(template, ensure_ascii=False),
             json.dumps(ats, ensure_ascii=False), json.dumps(files, ensure_ascii=False), timestamp, document_id),
        )
        return _document_row(c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())


def set_document_status(document_id: int, status: str) -> dict:
    if status not in {"brouillon", "pret", "envoye"}:
        raise ValueError("Statut de document inconnu.")
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise NotFound("Document de candidature introuvable.")
        ats = json.loads(row["ats_json"])
        if status == "pret" and (not ats.get("ready") or float(ats.get("readability", 0)) < 95):
            raise Conflict("Lisibilité ATS inférieure à 95 % : corrige et contrôle de nouveau le document.")
        c.execute("UPDATE documents SET status=?, updated_at_utc=? WHERE id=?", (status, timestamp, document_id))
        return _document_row(c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone())


# ---------------------------------------------------------------- sessions de candidature

ATTEMPT_STATUSES = {"manual_open", "queued", "running", "needs_user", "submitted", "failed", "cancelled"}
SUBMIT_GUARD_MESSAGE = ("Le bouton d'envoi a déjà été cliqué pour cette session : la candidature est peut-être partie. "
                        "Pour éviter un doublon, JobRadar ne la renvoie pas. Vérifie sur le site de l'entreprise, "
                        "puis indique si elle est partie ou passe en mode manuel.")
UNCONFIRMED_SUBMISSION = {
    "kind": "confirmation_missing", "action": "confirm_sent",
    "message": ("Le bouton d'envoi a été cliqué, mais aucune page de confirmation n'a été reconnue. Vérifie sur le "
                "site de l'entreprise ou dans tes e-mails, puis indique si la candidature est partie. JobRadar ne la "
                "renverra jamais automatiquement."),
}


def _attempt_row(row) -> dict:
    data = dict(row)
    data["answers"] = json.loads(data.pop("answers_json"))
    raw_intervention = data.pop("intervention_json")
    data["intervention"] = json.loads(raw_intervention) if raw_intervention else None
    return data


def create_application_attempt(application_id: int, *, mode: str, status: str, cv_document_id: int | None,
                               letter_document_id: int | None, target_url: str, provider: str,
                               review_policy: str = "first_five") -> dict:
    """Crée une tentative après avoir lié chaque document à la bonne candidature."""
    if mode not in {"manual", "assisted"} or status not in ATTEMPT_STATUSES:
        raise ValueError("Mode ou statut de candidature invalide.")
    if review_policy not in {"first_five", "always", "never"}:
        raise ValueError("Politique de validation avant envoi invalide.")
    if not cv_document_id and not letter_document_id:
        raise ValueError("Choisis au moins un document à envoyer.")
    timestamp = utc_iso(utcnow(), "created_at")
    with connect() as c:
        _get_app(c, application_id)
        for document_id, kind in ((cv_document_id, "cv"), (letter_document_id, "lettre")):
            if document_id is None:
                continue
            row = c.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None or row["application_id"] != application_id or row["kind"] != kind:
                raise ValueError(f"Le document sélectionné ne correspond pas à cette candidature ({kind}).")
            if row["status"] != "pret":
                raise Conflict(f"Le {kind} sélectionné doit être marqué « prêt à envoyer ».")
        cur = c.execute(
            """INSERT INTO application_attempts(
                   application_id, mode, status, cv_document_id, letter_document_id, target_url, provider,
                   review_policy, created_at_utc, updated_at_utc
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (application_id, mode, status, cv_document_id, letter_document_id, target_url, provider,
             review_policy, timestamp, timestamp),
        )
        return _attempt_row(c.execute("SELECT * FROM application_attempts WHERE id=?", (cur.lastrowid,)).fetchone())


def list_application_attempts(application_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, application_id)
        rows = c.execute(
            "SELECT * FROM application_attempts WHERE application_id=? ORDER BY created_at_utc DESC, id DESC",
            (application_id,),
        ).fetchall()
        return [_attempt_row(row) for row in rows]


def get_application_attempt(attempt_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound("Session de candidature introuvable.")
        return _attempt_row(row)


def submit_application_attempt(attempt_id: int) -> dict:
    """Fige une session manuelle comme envoyée ; opération idempotente."""
    timestamp = utc_iso(utcnow(), "submitted_at")
    with connect() as c:
        row = c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound("Session de candidature introuvable.")
        if row["mode"] != "manual":
            raise Conflict("Seule une candidature manuelle peut être confirmée par cette action.")
        if row["status"] not in {"manual_open", "submitted"}:
            raise Conflict("Cette session ne peut plus être confirmée comme envoyée.")
        if row["status"] != "submitted":
            c.execute(
                """UPDATE application_attempts
                   SET status='submitted', submitted_at_utc=?, updated_at_utc=? WHERE id=?""",
                (timestamp, timestamp, attempt_id),
            )
        return _attempt_row(c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone())


def transition_application_attempt(attempt_id: int, *, expected: set[str], status: str,
                                   intervention: dict | None = None, error: str | None = None) -> dict:
    """Transition compare-and-set : un worker ou l'utilisateur ne peut pas écraser un état concurrent."""
    if status not in ATTEMPT_STATUSES or not expected:
        raise ValueError("Transition de session invalide.")
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        row = c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound("Session de candidature introuvable.")
        if row["status"] not in expected:
            raise Conflict(f"La session est déjà au statut « {row['status']} ».")
        if status == "queued" and row["submit_clicked_at_utc"]:
            raise Conflict(SUBMIT_GUARD_MESSAGE)
        submitted = timestamp if status == "submitted" else row["submitted_at_utc"]
        c.execute(
            """UPDATE application_attempts SET status=?, intervention_json=?, error=?,
               submitted_at_utc=?, updated_at_utc=? WHERE id=?""",
            (status, json.dumps(intervention, ensure_ascii=False) if intervention else None,
             error, submitted, timestamp, attempt_id),
        )
        return _attempt_row(c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone())


def answer_application_attempt(attempt_id: int, answers: list[dict]) -> dict:
    """Enregistre les réponses d'une intervention, puis remet atomiquement la session en file."""
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        row = c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound("Session de candidature introuvable.")
        if row["status"] != "needs_user":
            raise Conflict("Cette session n'attend pas de réponse.")
        if row["submit_clicked_at_utc"]:
            raise Conflict(SUBMIT_GUARD_MESSAGE)
        intervention = json.loads(row["intervention_json"] or "null") or {}
        if intervention.get("kind") != "missing_fields":
            raise Conflict("Cette intervention n'accepte pas de réponses de formulaire.")
        allowed = {field.get("selector"): field for field in intervention.get("fields") or []}
        if not allowed or {answer.get("selector") for answer in answers} != set(allowed):
            raise ValueError("Réponds à chacun des champs demandés, sans en ajouter.")
        previous = json.loads(row["answers_json"] or "[]")
        merged = {answer.get("selector"): answer for answer in previous if answer.get("selector")}
        merged.update({answer.get("selector"): answer for answer in answers if answer.get("selector")})
        c.execute(
            """UPDATE application_attempts SET status='queued', answers_json=?, intervention_json=?,
               error=NULL, updated_at_utc=? WHERE id=?""",
            (json.dumps(list(merged.values()), ensure_ascii=False),
             json.dumps({"kind": "answers_provided"}, ensure_ascii=False),
             timestamp, attempt_id),
        )
        return _attempt_row(c.execute("SELECT * FROM application_attempts WHERE id=?", (attempt_id,)).fetchone())


def recover_application_attempts() -> int:
    """Après arrêt brutal, remet les traitements interrompus dans la file.

    Une session interrompue après le clic sur « Envoyer » n'est jamais relancée : elle attend
    que l'utilisateur indique si la candidature est partie.
    """
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        unconfirmed = c.execute(
            """UPDATE application_attempts SET status='needs_user', intervention_json=?, updated_at_utc=?
               WHERE status='running' AND submit_clicked_at_utc IS NOT NULL""",
            (json.dumps(UNCONFIRMED_SUBMISSION, ensure_ascii=False), timestamp),
        ).rowcount
        return unconfirmed + c.execute(
            """UPDATE application_attempts SET status='queued', updated_at_utc=?,
               error='Traitement interrompu par un arrêt de JobRadar ; reprise planifiée.'
               WHERE status='running' AND submit_clicked_at_utc IS NULL""",
            (timestamp,),
        ).rowcount


def mark_submit_clicked(attempt_id: int) -> None:
    """Enregistré juste avant le clic d'envoi : au moindre doute ensuite, la candidature est supposée partie."""
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        done = c.execute(
            """UPDATE application_attempts SET submit_clicked_at_utc=?, updated_at_utc=?
               WHERE id=? AND status='running'""",
            (timestamp, timestamp, attempt_id),
        ).rowcount
        if not done:
            raise Conflict("La session n'est plus en cours : envoi annulé par sécurité.")


def claim_next_application_attempt() -> dict | None:
    """Réserve atomiquement la plus ancienne session ; un seul worker peut la gagner."""
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        row = c.execute(
            "SELECT id FROM application_attempts WHERE status='queued' ORDER BY created_at_utc, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        won = c.execute(
            "UPDATE application_attempts SET status='running', updated_at_utc=? WHERE id=? AND status='queued'",
            (timestamp, row["id"]),
        ).rowcount
        if not won:
            return None
        return _attempt_row(c.execute("SELECT * FROM application_attempts WHERE id=?", (row["id"],)).fetchone())


def set_auto_apply(search_id: int, *, enabled: bool, min_score: int, daily_limit: int) -> dict:
    """Réglages d'auto-candidature d'une recherche (bornes vérifiées ici aussi)."""
    if not 0 <= min_score <= 100 or not 1 <= daily_limit <= 50:
        raise ValueError("Score minimal entre 0 et 100, plafond quotidien entre 1 et 50.")
    with connect() as c:
        done = c.execute(
            """UPDATE searches SET auto_apply_enabled=?, auto_apply_min_score=?, auto_apply_daily_limit=?
               WHERE id=?""",
            (int(enabled), min_score, daily_limit, search_id),
        ).rowcount
        if not done:
            raise NotFound("Recherche introuvable.")
        return _search_row(c.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone())


def mark_offer_auto_applied(offer_id: int) -> bool:
    """Réserve une offre pour l'auto-candidature ; faux si elle l'a déjà été (une seule fois par offre)."""
    with connect() as c:
        return c.execute("UPDATE offers SET auto_apply_at_utc=? WHERE id=? AND auto_apply_at_utc IS NULL",
                         (utc_iso(utcnow(), "auto_apply_at"), offer_id)).rowcount == 1


def auto_applied_offer_count(search_id: int, since_utc: datetime) -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM offers WHERE search_id=? AND auto_apply_at_utc >= ?",
                         (search_id, utc_iso(since_utc, "since"))).fetchone()[0]


# ---------------------------------------------------------------- préparations CV/lettre

PREPARATION_ACTIVE = ("queued", "running")


def _document_job_row(row) -> dict:
    data = dict(row)
    data["params"] = json.loads(data.pop("params_json") or "{}")
    raw = data.pop("result_json")
    data["result"] = json.loads(raw) if raw else None
    return data


def create_preparation_job(application_id: int, *, origin: str, params: dict) -> dict:
    """File une préparation ; refusée si une autre est déjà en attente ou en cours pour cette candidature."""
    if origin not in {"manual", "auto"}:
        raise ValueError("Origine de préparation invalide.")
    with connect() as c:
        _get_app(c, application_id)
        if c.execute("SELECT 1 FROM preparation_jobs WHERE application_id=? AND status IN ('queued', 'running')",
                     (application_id,)).fetchone():
            raise Conflict("Une préparation de documents est déjà en cours pour cette candidature.")
        cur = c.execute(
            """INSERT INTO preparation_jobs(application_id, origin, params_json, status, created_at_utc)
               VALUES (?,?,?,'queued',?)""",
            (application_id, origin, json.dumps(params, ensure_ascii=False), utc_iso(utcnow(), "created_at")),
        )
        return _document_job_row(c.execute("SELECT * FROM preparation_jobs WHERE id=?", (cur.lastrowid,)).fetchone())


def get_preparation_job(job_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM preparation_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("Préparation introuvable.")
        return _document_job_row(row)


def latest_preparation_job(application_id: int) -> dict | None:
    with connect() as c:
        _get_app(c, application_id)
        row = c.execute("SELECT * FROM preparation_jobs WHERE application_id=? ORDER BY id DESC LIMIT 1",
                        (application_id,)).fetchone()
        return _document_job_row(row) if row else None


def claim_next_preparation_job() -> dict | None:
    """Réserve atomiquement la plus ancienne préparation en attente."""
    timestamp = utc_iso(utcnow(), "started_at")
    with connect() as c:
        row = c.execute(
            "SELECT id FROM preparation_jobs WHERE status='queued' ORDER BY created_at_utc, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        won = c.execute(
            "UPDATE preparation_jobs SET status='running', started_at_utc=? WHERE id=? AND status='queued'",
            (timestamp, row["id"]),
        ).rowcount
        if not won:
            return None
        return _document_job_row(c.execute("SELECT * FROM preparation_jobs WHERE id=?", (row["id"],)).fetchone())


def update_preparation_job(job_id: int, **fields) -> dict:
    """Avancement d'une préparation en cours ; ``status`` final (done | failed) date la fin."""
    allowed = {"status", "step", "session_id", "result", "attempt_id", "error"}
    if set(fields) - allowed:
        raise ValueError("Champ de préparation inconnu.")
    if "result" in fields:
        fields["result_json"] = json.dumps(fields.pop("result"), ensure_ascii=False)
    if fields.get("status") in {"done", "failed"}:
        fields["finished_at_utc"] = utc_iso(utcnow(), "finished_at")
    with connect() as c:
        assignments = ", ".join(f"{name}=?" for name in fields)
        c.execute(f"UPDATE preparation_jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
        row = c.execute("SELECT * FROM preparation_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("Préparation introuvable.")
        return _document_job_row(row)


def consume_preparation_jobs(application_id: int) -> None:
    """La page a repris le résultat (documents générés ou écran fermé) : il n'est plus proposé à la reprise."""
    with connect() as c:
        c.execute("""UPDATE preparation_jobs SET consumed_at_utc=? WHERE application_id=?
                     AND status IN ('done', 'failed') AND consumed_at_utc IS NULL""",
                  (utc_iso(utcnow(), "consumed_at"), application_id))


def recover_preparation_jobs() -> int:
    """Après un arrêt de JobRadar, les préparations interrompues repartent depuis le début."""
    with connect() as c:
        return c.execute("UPDATE preparation_jobs SET status='queued', step=NULL WHERE status='running'").rowcount


def recent_auto_preparations(search_id: int, limit: int = 1000) -> list[dict]:
    """Dernières préparations automatiques des offres d'une recherche, pour l'état affiché."""
    with connect() as c:
        rows = c.execute(
            """SELECT p.*, a.title, a.company FROM preparation_jobs p
               JOIN applications a ON a.id = p.application_id
               JOIN offers o ON o.id = a.offer_id
               WHERE p.origin='auto' AND o.search_id=? ORDER BY p.id DESC LIMIT ?""",
            (search_id, limit),
        ).fetchall()
        return [_document_job_row(row) for row in rows]


def assisted_submitted_count() -> int:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM application_attempts WHERE mode='assisted' AND status='submitted'"
        ).fetchone()[0]


# ---------------------------------------------------------------- préparation d'entretien

def _preparation_row(row) -> dict:
    data = dict(row)
    data["content"] = json.loads(data.pop("content_json"))
    return data


def create_interview_preparation(application_id: int, interview_id: int | None, content: dict,
                                 markdown_path: str | None) -> dict:
    timestamp = utc_iso(utcnow(), "created_at")
    with connect() as c:
        _get_app(c, application_id)
        if interview_id is not None:
            interview = c.execute("SELECT application_id FROM interviews WHERE id=?", (interview_id,)).fetchone()
            if interview is None or interview["application_id"] != application_id:
                raise ValueError("L'entretien ne correspond pas à cette candidature.")
        cur = c.execute(
            """INSERT INTO interview_preparations(
                   application_id, interview_id, content_json, markdown_path, created_at_utc, updated_at_utc
               ) VALUES (?,?,?,?,?,?)""",
            (application_id, interview_id, json.dumps(content, ensure_ascii=False), markdown_path,
             timestamp, timestamp),
        )
        return _preparation_row(c.execute(
            "SELECT * FROM interview_preparations WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def list_interview_preparations(application_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, application_id)
        rows = c.execute(
            """SELECT * FROM interview_preparations WHERE application_id=?
               ORDER BY created_at_utc DESC, id DESC""", (application_id,)
        ).fetchall()
        return [_preparation_row(row) for row in rows]


def get_interview_preparation(preparation_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM interview_preparations WHERE id=?", (preparation_id,)).fetchone()
        if row is None:
            raise NotFound("Fiche de préparation introuvable.")
        return _preparation_row(row)


def _simulation_row(row) -> dict:
    data = dict(row)
    data["questions"] = json.loads(data.pop("questions_json"))
    data["turns"] = json.loads(data.pop("turns_json"))
    return data


def create_interview_simulation(application_id: int, preparation_id: int, questions: list[dict]) -> dict:
    timestamp = utc_iso(utcnow(), "created_at")
    with connect() as c:
        _get_app(c, application_id)
        preparation = c.execute(
            "SELECT application_id FROM interview_preparations WHERE id=?", (preparation_id,)
        ).fetchone()
        if preparation is None or preparation["application_id"] != application_id:
            raise ValueError("La fiche de préparation ne correspond pas à cette candidature.")
        cur = c.execute(
            """INSERT INTO interview_simulations(
                   application_id, preparation_id, questions_json, created_at_utc, updated_at_utc
               ) VALUES (?,?,?,?,?)""",
            (application_id, preparation_id, json.dumps(questions, ensure_ascii=False), timestamp, timestamp),
        )
        return _simulation_row(c.execute(
            "SELECT * FROM interview_simulations WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def get_interview_simulation(simulation_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM interview_simulations WHERE id=?", (simulation_id,)).fetchone()
        if row is None:
            raise NotFound("Simulation d'entretien introuvable.")
        return _simulation_row(row)


def list_interview_simulations(application_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, application_id)
        return [_simulation_row(row) for row in c.execute(
            """SELECT * FROM interview_simulations WHERE application_id=?
               ORDER BY created_at_utc DESC, id DESC""", (application_id,)
        ).fetchall()]


def answer_interview_simulation(simulation_id: int, question_index: int, answer: str, feedback: dict) -> dict:
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        row = c.execute("SELECT * FROM interview_simulations WHERE id=?", (simulation_id,)).fetchone()
        if row is None:
            raise NotFound("Simulation d'entretien introuvable.")
        if row["status"] != "active" or row["current_question"] != question_index:
            raise Conflict("Cette question a déjà été évaluée ou la simulation est terminée.")
        questions, turns = json.loads(row["questions_json"]), json.loads(row["turns_json"])
        if question_index >= len(questions):
            raise Conflict("La simulation est déjà terminée.")
        turns.append({"question_index": question_index, "question": questions[question_index]["question"],
                      "answer": answer, "feedback": feedback, "answered_at_utc": timestamp})
        next_index = question_index + 1
        status = "completed" if next_index >= len(questions) else "active"
        updated = c.execute(
            """UPDATE interview_simulations SET turns_json=?, current_question=?, status=?, updated_at_utc=?
               WHERE id=? AND status='active' AND current_question=?""",
            (json.dumps(turns, ensure_ascii=False), next_index, status, timestamp, simulation_id, question_index),
        ).rowcount
        if updated != 1:
            raise Conflict("Cette réponse a été enregistrée par une autre action.")
        return _simulation_row(c.execute(
            "SELECT * FROM interview_simulations WHERE id=?", (simulation_id,)
        ).fetchone())


# ---------------------------------------------------------------- contacts de candidature

CONTACT_FIELDS = ("name", "role", "company", "email", "phone", "profile_url", "context", "notes")


def _contact_row(row) -> dict:
    return dict(row)


def create_application_contact(application_id: int, values: dict) -> dict:
    timestamp = utc_iso(utcnow(), "created_at")
    data = {field: values.get(field) for field in CONTACT_FIELDS}
    data["context"] = data["context"] or ""
    data["notes"] = data["notes"] or ""
    with connect() as c:
        _get_app(c, application_id)
        cur = c.execute(
            """INSERT INTO application_contacts(application_id, name, role, company, email, phone, profile_url,
                   context, notes, created_at_utc, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (application_id, *(data[field] for field in CONTACT_FIELDS), timestamp, timestamp),
        )
        return _contact_row(c.execute("SELECT * FROM application_contacts WHERE id=?", (cur.lastrowid,)).fetchone())


def list_application_contacts(application_id: int) -> list[dict]:
    with connect() as c:
        _get_app(c, application_id)
        return [_contact_row(row) for row in c.execute(
            "SELECT * FROM application_contacts WHERE application_id=? ORDER BY name COLLATE NOCASE, id",
            (application_id,),
        ).fetchall()]


def get_application_contact(contact_id: int) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM application_contacts WHERE id=?", (contact_id,)).fetchone()
        if row is None:
            raise NotFound("Contact introuvable.")
        return _contact_row(row)


def update_application_contact(contact_id: int, fields: dict) -> dict:
    allowed = {key: value for key, value in fields.items() if key in CONTACT_FIELDS}
    if not allowed:
        return get_application_contact(contact_id)
    timestamp = utc_iso(utcnow(), "updated_at")
    with connect() as c:
        if c.execute("SELECT 1 FROM application_contacts WHERE id=?", (contact_id,)).fetchone() is None:
            raise NotFound("Contact introuvable.")
        c.execute(f"UPDATE application_contacts SET {', '.join(f'{key}=?' for key in allowed)}, updated_at_utc=? "
                  "WHERE id=?", (*allowed.values(), timestamp, contact_id))
        return _contact_row(c.execute("SELECT * FROM application_contacts WHERE id=?", (contact_id,)).fetchone())


def delete_application_contact(contact_id: int) -> None:
    with connect() as c:
        if c.execute("DELETE FROM application_contacts WHERE id=?", (contact_id,)).rowcount != 1:
            raise NotFound("Contact introuvable.")
