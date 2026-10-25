"""Flux public Arbeitnow : offres européennes provenant principalement d'ATS employeurs."""
from datetime import datetime, timezone

import httpx

from .common import html_to_text, matches_any

URL = "https://www.arbeitnow.com/api/job-board-api"


def _date(value: object) -> str | None:
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    return None


def _offer(item: dict) -> dict | None:
    url, title = item.get("url"), item.get("title")
    if not url or not title:
        return None
    tags = item.get("tags") or []
    return {
        "title": title,
        "company": item.get("company_name"),
        "location": item.get("location") or ("Télétravail" if item.get("remote") else None),
        "contract": " ".join(str(tag) for tag in tags),
        "date_posted": _date(item.get("created_at")),
        "published_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
        "description": html_to_text(item.get("description")),
        "url": url,
        "source": "arbeitnow",
    }


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    terms = [*(criteria.get("search_queries") or []), *(criteria.get("include_keywords") or [])]
    locations = [loc.get("search_term") or loc.get("label") for loc in criteria.get("locations") or []]
    limit = max(1, int(cfg.get("results_per_query") or 25))
    out, seen, page = [], set(known_urls), 1
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        while len(out) < limit and page <= 5:
            response = client.get(URL, params={"page": page})
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data") or []
            for item in items:
                offer = _offer(item)
                if not offer or offer["url"] in seen:
                    continue
                haystack = " ".join(str(offer.get(k) or "") for k in ("title", "description", "contract"))
                if terms and not matches_any(haystack, terms):
                    continue
                if locations and not item.get("remote") and not matches_any(offer.get("location") or "", locations):
                    continue
                seen.add(offer["url"])
                out.append(offer)
                if len(out) >= limit:
                    break
            if not payload.get("links", {}).get("next") or not items:
                break
            page += 1
    log(f"arbeitnow : {len(out)} offres européennes nouvelles")
    return out
