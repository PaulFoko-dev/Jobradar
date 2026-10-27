"""Indicateurs locaux de progression et compétences demandées.

Les calculs sont effectués à la lecture : aucune donnée dérivée n'est stockée et
aucun contenu personnel n'est transmis à un service externe.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

from . import db

RESPONSE_STATUSES = {"entretien_rh", "entretien_tech", "retenu", "refuse"}
SENT_STATUSES = {"envoyee", "relance", *RESPONSE_STATUSES}
SOURCE_DOMAINS = {
    "indeed.": "Indeed", "linkedin.": "LinkedIn", "hellowork.": "HelloWork",
    "welcometothejungle.": "Welcome to the Jungle", "francetravail.": "France Travail",
    "arbeitnow.": "Arbeitnow", "remoteok.": "Remote OK",
    "apprentissage.beta.gouv.fr": "La Bonne Alternance",
}
TEMPLATE_LABELS = {"sobre-ats": "Sobre ATS", "moderne-ats": "Moderne ATS",
                   "technique-projets": "Technique/Projets", "original": "Modèle d'origine"}


def _text_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return re.sub(r"\s+", " ", "".join(c for c in normalized if not unicodedata.combining(c))).strip()


def _source(url: str | None, snapshot: dict | None = None) -> str:
    links = (snapshot or {}).get("links") or []
    candidates = [link.get("source") for link in links if isinstance(link, dict)]
    candidates.append(url or (snapshot or {}).get("url"))
    for candidate in candidates:
        if not candidate:
            continue
        plain = str(candidate).strip()
        host = (urlparse(plain).hostname or plain).casefold().removeprefix("www.")
        for domain, label in SOURCE_DOMAINS.items():
            if domain in host:
                return label
        if "." not in host and len(plain) <= 60:
            return plain
    return "Ajout manuel"


def _json(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def demanded_skills(limit: int = 30) -> dict:
    """Compétences techniques les plus citées dans les offres analysées."""
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    offer_count = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT analysis_json FROM offers WHERE analysis_json IS NOT NULL").fetchall()
    for row in rows:
        analysis = _json(row["analysis_json"], {})
        raw = analysis.get("cv_keywords") or analysis.get("skills") or []
        per_offer: set[str] = set()
        for item in raw:
            label = str(item).strip() if not isinstance(item, dict) else str(item.get("name") or "").strip()
            key = _text_key(label)
            if not key or key in per_offer:
                continue
            per_offer.add(key)
            labels.setdefault(key, label)
        if per_offer:
            offer_count += 1
            counts.update(per_offer)
    items = [{"name": labels[key], "offers": count,
              "share": round(100 * count / offer_count) if offer_count else 0}
             for key, count in counts.most_common(max(1, min(limit, 100)))]
    return {"analyzed_offers": len(rows), "offers_with_skills": offer_count, "items": items}


def _first_response_days(row) -> float | None:
    if not row["applied_at_utc"]:
        return None
    start = datetime.fromisoformat(row["applied_at_utc"].replace("Z", "+00:00"))
    for event in _json(row["history_json"], []):
        if event.get("status") not in RESPONSE_STATUSES or not event.get("at"):
            continue
        end = datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() / 86400)
    return None


def _group(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in rows:
        label = row.get(key) or "Non renseigné"
        item = groups.setdefault(label, {"label": label, "applications": 0, "sent": 0,
                                         "responses": 0, "interviews": 0, "offers": 0})
        item["applications"] += 1
        item["sent"] += int(row["sent"])
        item["responses"] += int(row["response"])
        item["interviews"] += int(row["interview"])
        item["offers"] += int(row["offer"])
    for item in groups.values():
        item["response_rate"] = round(100 * item["responses"] / item["sent"]) if item["sent"] else None
        item["interview_rate"] = round(100 * item["interviews"] / item["sent"]) if item["sent"] else None
    return sorted(groups.values(), key=lambda item: (-item["sent"], item["label"].casefold()))


def application_statistics() -> dict:
    """Entonnoir et comparaisons ; un taux utilise les candidatures réellement envoyées."""
    with db.connect() as conn:
        applications = [dict(row) for row in conn.execute("SELECT * FROM applications ORDER BY id")]
        attempts = [dict(row) for row in conn.execute(
            "SELECT * FROM application_attempts ORDER BY created_at_utc DESC, id DESC")]
        documents = [dict(row) for row in conn.execute(
            "SELECT * FROM documents WHERE kind='cv' ORDER BY version DESC, id DESC")]
    attempts_by_app, docs_by_app = {}, {}
    for attempt in attempts:
        attempts_by_app.setdefault(attempt["application_id"], attempt)
    for document in documents:
        docs_by_app.setdefault(document["application_id"], document)
    rows, delays = [], []
    for application in applications:
        attempt = attempts_by_app.get(application["id"], {})
        document = docs_by_app.get(application["id"], {})
        composition = _json(document.get("composition_json"), {})
        template = _json(document.get("template_json"), {})
        snapshot = _json(application.get("offer_snapshot_json"), {})
        status = application["status"]
        delay = _first_response_days(application)
        if delay is not None:
            delays.append(delay)
        template_id = template.get("id") or composition.get("template") or ""
        rows.append({"source": _source(application.get("url"), snapshot),
                     "base_cv": composition.get("base_source_id") or "Non renseigné",
                     "template": TEMPLATE_LABELS.get(template_id, template_id or "Non renseigné"),
                     "mode": {"manual": "Manuel", "assisted": "Assisté par IA"}.get(attempt.get("mode"), "Non renseigné"),
                     "sent": bool(application.get("applied_at_utc") or status in SENT_STATUSES),
                     "response": status in RESPONSE_STATUSES,
                     "interview": status in {"entretien_rh", "entretien_tech", "retenu"},
                     "offer": status == "retenu"})
    sent, responses = sum(r["sent"] for r in rows), sum(r["response"] for r in rows)
    return {"summary": {"applications": len(rows), "sent": sent, "responses": responses,
                        "interviews": sum(r["interview"] for r in rows), "offers": sum(r["offer"] for r in rows),
                        "response_rate": round(100 * responses / sent) if sent else None,
                        "average_response_days": round(sum(delays) / len(delays), 1) if delays else None},
            "by_source": _group(rows, "source"), "by_base_cv": _group(rows, "base_cv"),
            "by_template": _group(rows, "template"), "by_mode": _group(rows, "mode")}
