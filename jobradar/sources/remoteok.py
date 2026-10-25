"""Flux JSON public Remote OK, avec conservation du lien et de l'attribution obligatoires."""
from datetime import datetime, timezone

import httpx

from .common import html_to_text, matches_any

URL = "https://remoteok.com/api"
HEADERS = {"User-Agent": "JobRadar/1.0 (outil personnel de recherche d'emploi)"}


def _date(value: object) -> str | None:
    if isinstance(value, str) and len(value) >= 10:
        return value[:10]
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    return None


def _offer(item: dict) -> dict | None:
    url = item.get("apply_url") or item.get("url")
    title = item.get("position")
    if not url or not title:
        return None
    return {
        "title": title,
        "company": item.get("company"),
        "logo": item.get("company_logo"),
        "location": item.get("location") or "Télétravail",
        "contract": "Télétravail " + " ".join(item.get("tags") or []),
        "date_posted": _date(item.get("date") or item.get("epoch")),
        "published_at": item.get("date"),
        "description": html_to_text(item.get("description")),
        "url": url,
        "source": "remoteok",
    }


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    response = httpx.get(URL, headers=HEADERS, timeout=25, follow_redirects=True)
    response.raise_for_status()
    terms = [*(criteria.get("search_queries") or []), *(criteria.get("include_keywords") or [])]
    out, seen = [], set(known_urls)
    limit = max(1, int(cfg.get("results_per_query") or 25))
    for item in response.json():
        if not isinstance(item, dict) or "position" not in item:
            continue  # le premier élément du flux contient les mentions légales
        offer = _offer(item)
        if not offer or offer["url"] in seen:
            continue
        text = " ".join(str(offer.get(k) or "") for k in ("title", "description", "contract"))
        if terms and not matches_any(text, terms):
            continue
        seen.add(offer["url"])
        out.append(offer)
        if len(out) >= limit:
            break
    log(f"remoteok : {len(out)} offres à distance nouvelles")
    return out
