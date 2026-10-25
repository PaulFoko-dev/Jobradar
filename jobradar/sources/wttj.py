"""Welcome to the Jungle : index de recherche Algolia utilisé par leur propre site.

L'identifiant et la clé ci-dessous sont les clés publiques de recherche (lecture seule) que le site
welcometothejungle.com envoie à tous les navigateurs. S'ils changent, la source renverra une erreur
et les autres sources continueront de fonctionner.
"""
import time
from functools import lru_cache

import httpx

from .common import HEADERS, html_to_text, polite_pause, strip_contract_words

APP_ID = "CSEKHVMS53"
API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wttj_jobs_production_fr"
URL = f"https://{APP_ID}-dsn.algolia.net/1/indexes/{INDEX}/query"
CONTRACT = {
    "stage": "internship", "alternance": "apprenticeship", "professionnalisation": "apprenticeship",
    "cdi": "full_time", "cdd": "temporary", "interim": "temporary", "freelance": "freelance", "vie": "vie",
}
CONTRACT_LABEL = {
    "internship": "Stage", "apprenticeship": "Alternance", "full_time": "CDI", "temporary": "CDD",
    "freelance": "Freelance", "vie": "VIE", "part_time": "Temps partiel",
}


@lru_cache(maxsize=64)
def geocode(place: str) -> tuple[float, float] | None:
    """Coordonnées via la Base Adresse Nationale (service public, sans clé)."""
    try:
        r = httpx.get("https://api-adresse.data.gouv.fr/search/", params={"q": place, "limit": 1}, timeout=15)
        lng, lat = r.json()["features"][0]["geometry"]["coordinates"]
        return lat, lng
    except Exception:
        return None


def _offer(h: dict) -> dict:
    org = h.get("organization") or {}
    office = (h.get("offices") or [{}])[0]
    missions = "\n".join(f"- {m}" for m in h.get("key_missions") or [])
    desc = "\n\n".join(x for x in (
        h.get("summary"),
        f"Missions clés :\n{missions}" if missions else "",
        f"Profil recherché :\n{html_to_text(h.get('profile'))}" if h.get("profile") else "",
    ) if x)
    return {
        "title": h["name"],
        "company": org.get("name"),
        "logo": (org.get("logo") or {}).get("url"),
        "location": ", ".join(x for x in (office.get("city"), office.get("local_state")) if x) or None,
        "contract": CONTRACT_LABEL.get(h.get("contract_type"), h.get("contract_type")),
        "date_posted": (h.get("published_at") or "")[:10] or None,
        "published_at": h.get("published_at"),
        "description": desc,
        "url": f"https://www.welcometothejungle.com/fr/companies/{org.get('slug')}/jobs/{h['slug']}",
        "source": "welcometothejungle",
    }


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    contracts = sorted({CONTRACT[c] for c in criteria.get("contract_types", []) if c in CONTRACT})
    filters = " OR ".join(f"contract_type:{c}" for c in contracts)
    since = int(time.time()) - int(criteria.get("posted_within_days") or 30) * 86400
    queries = list(dict.fromkeys(strip_contract_words(q) for q in criteria.get("search_queries", [])))
    headers = {
        **HEADERS, "X-Algolia-Application-Id": APP_ID, "X-Algolia-API-Key": API_KEY,
        "Referer": "https://www.welcometothejungle.com/", "Origin": "https://www.welcometothejungle.com",
    }
    out = []
    with httpx.Client(headers=headers, timeout=25) as client:
        for loc in criteria.get("locations", []):
            coords = (loc.get("lat"), loc.get("lng")) if loc.get("lat") else geocode(loc["search_term"])
            if not coords:
                log(f"welcometothejungle : position inconnue pour {loc['label']}, zone ignorée")
                continue
            radius_km = max(int(loc.get("radius_km") or 0), 60 if loc.get("kind") in ("region", "country") else 25)
            if loc.get("kind") == "country":
                radius_km = 1000
            for q in queries:
                body = {
                    "query": q,
                    "hitsPerPage": cfg["results_per_query"],
                    "aroundLatLng": f"{coords[0]}, {coords[1]}",
                    "aroundRadius": radius_km * 1000,
                    "numericFilters": [f"published_at_timestamp>{since}"],
                }
                if filters:
                    body["filters"] = filters
                try:
                    r = client.post(URL, json=body)
                    r.raise_for_status()
                    hits = r.json().get("hits", [])
                except httpx.HTTPError as e:
                    log(f"welcometothejungle « {q} » : erreur {e}")
                    break
                out += [_offer(h) for h in hits if h.get("name") and h.get("slug")]
                log(f"welcometothejungle « {q} » @ {loc['label']} : {len(hits)} offres")
                polite_pause(0.6)
    return out
