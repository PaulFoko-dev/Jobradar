"""Stockage SQLite : recherches, offres, exécutions."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    form_json TEXT NOT NULL DEFAULT '{}',
    prompt TEXT NOT NULL DEFAULT '',
    criteria_json TEXT NOT NULL DEFAULT '{}',
    frequency TEXT NOT NULL DEFAULT 'daily',
    created_at TEXT NOT NULL,
    last_run TEXT
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    dedup_key TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    contract TEXT,
    date_posted TEXT,
    description TEXT,
    url TEXT NOT NULL,
    links_json TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    analysis_json TEXT,
    score INTEGER,
    in_zone INTEGER,
    status TEXT NOT NULL DEFAULT 'new',
    viewed INTEGER NOT NULL DEFAULT 0,
    logo TEXT,
    UNIQUE(search_id, dedup_key)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER UNIQUE,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    url TEXT,
    score INTEGER,
    logo TEXT,
    status TEXT NOT NULL DEFAULT 'consultee',
    notes TEXT NOT NULL DEFAULT '',
    history_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    search_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        # Migrations des bases créées avant l'ajout d'une colonne
        for table in ("offers", "applications"):
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if "logo" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN logo TEXT")


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
    with connect() as c:
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
                "UPDATE offers SET links_json=?, last_seen=?, description=?, logo=? WHERE id=?",
                (json.dumps(links, ensure_ascii=False), now(), desc, r["logo"] or o.get("logo"), r["id"]),
            )
            return False
        c.execute(
            """INSERT INTO offers(search_id, dedup_key, title, company, location, contract, date_posted,
               description, url, links_json, first_seen, last_seen, logo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                search_id, o["dedup_key"], o["title"], o.get("company"), o.get("location"),
                o.get("contract"), o.get("date_posted"), o.get("description"), o["url"],
                json.dumps(o["links"], ensure_ascii=False), now(), now(), o.get("logo"),
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


def save_analysis(offer_id: int, analysis: dict):
    with connect() as c:
        c.execute(
            "UPDATE offers SET analysis_json=?, score=?, in_zone=? WHERE id=?",
            (
                json.dumps(analysis, ensure_ascii=False),
                int(analysis.get("score", 0)),
                1 if analysis.get("in_zone") else 0,
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


def list_offers(search_id: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """SELECT o.*, a.id AS app_id, a.status AS app_status FROM offers o
               LEFT JOIN applications a ON a.offer_id = o.id
               WHERE o.search_id=? ORDER BY o.score DESC NULLS LAST, o.first_seen DESC""",
            (search_id,),
        ).fetchall()
        logos = company_logos(c)
        out = [_offer_row(r) for r in rows]
        for o in out:
            o["logo"] = o["logo"] or logos.get((o["company"] or "").strip().lower())
        return out


def company_logos(c) -> dict:
    """Logo connu par entreprise : une offre sans logo reprend celui d'une autre offre de la même entreprise."""
    rows = c.execute("SELECT company, logo FROM offers WHERE logo IS NOT NULL AND company IS NOT NULL").fetchall()
    return {r["company"].strip().lower(): r["logo"] for r in rows}


# ---------------------------------------------------------------- suivi des candidatures

APP_STATUSES = ["consultee", "envoyee", "relance", "entretien_rh", "entretien_tech", "retenu", "refuse", "abandon"]


def _app_row(r) -> dict:
    d = dict(r)
    d["history"] = json.loads(d.pop("history_json"))
    return d


def list_applications() -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
        logos = company_logos(c)
        out = [_app_row(r) for r in rows]
        for a in out:
            a["logo"] = a["logo"] or logos.get((a["company"] or "").strip().lower())
        return out


def create_application(data: dict) -> dict:
    """Ajoute une candidature au suivi. Pour une offre déjà suivie, renvoie l'existante sans la modifier."""
    with connect() as c:
        if data.get("offer_id"):
            r = c.execute("SELECT * FROM applications WHERE offer_id=?", (data["offer_id"],)).fetchone()
            if r:
                return _app_row(r)
            o = c.execute("SELECT * FROM offers WHERE id=?", (data["offer_id"],)).fetchone()
            if not o:
                raise KeyError("offre introuvable")
            links = json.loads(o["links_json"])
            apply = next((l["url"] for l in links if l.get("apply")), o["url"])
            data = {**data, "title": o["title"], "company": o["company"], "location": o["location"],
                    "url": apply, "score": o["score"], "logo": o["logo"]}
        status = data.get("status") if data.get("status") in APP_STATUSES else "consultee"
        ts = now()
        cur = c.execute(
            """INSERT INTO applications(offer_id, title, company, location, url, score, logo, status, notes,
               history_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.get("offer_id"), data["title"], data.get("company"), data.get("location"), data.get("url"),
             data.get("score"), data.get("logo"), status, data.get("notes", ""),
             json.dumps([{"status": status, "at": ts}]), ts, ts),
        )
        return _app_row(c.execute("SELECT * FROM applications WHERE id=?", (cur.lastrowid,)).fetchone())


def update_application(app_id: int, fields: dict) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if not r:
            return None
        history = json.loads(r["history_json"])
        status, notes = r["status"], r["notes"]
        if fields.get("status") in APP_STATUSES and fields["status"] != status:
            status = fields["status"]
            history.append({"status": status, "at": now()})
        if fields.get("notes") is not None:
            notes = fields["notes"]
        c.execute(
            "UPDATE applications SET status=?, notes=?, history_json=?, updated_at=? WHERE id=?",
            (status, notes, json.dumps(history), now(), app_id),
        )
        return _app_row(c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def delete_application(app_id: int):
    with connect() as c:
        c.execute("DELETE FROM applications WHERE id=?", (app_id,))


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
