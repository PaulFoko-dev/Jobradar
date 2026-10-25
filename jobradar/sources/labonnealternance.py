"""API officielle La Bonne Alternance.

La recherche officielle ne propose pas de texte libre : elle est interrogée une fois par zone,
puis le filtre métier habituel de JobRadar écarte les offres hors sujet avant tout appel à l'IA.
"""
from datetime import date, timedelta

import httpx

from .common import html_to_text

SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
ALTERNANCE = {"alternance", "apprentissage", "professionnalisation"}


def _location(workplace: dict) -> str | None:
    location = workplace.get("location") or {}
    return location.get("address") or workplace.get("name")


def _offer(item: dict) -> dict | None:
    offer = item.get("offer") or {}
    workplace = item.get("workplace") or {}
    apply = item.get("apply") or {}
    publication = offer.get("publication") or {}
    identifier = item.get("identifier") or {}
    title = offer.get("title")
    url = apply.get("url")
    if not title or not url:
        return None
    skills = [*(offer.get("desired_skills") or []), *(offer.get("to_be_acquired_skills") or [])]
    description = html_to_text(offer.get("description"))
    if skills:
        description += "\n\nCompétences : " + "; ".join(str(skill) for skill in skills)
    return {
        "title": title,
        "company": workplace.get("name") or workplace.get("brand") or workplace.get("legal_name"),
        "location": _location(workplace),
        "contract": ", ".join((item.get("contract") or {}).get("type") or []) or "Alternance",
        "date_posted": (publication.get("creation") or "")[:10] or None,
        "published_at": publication.get("creation"),
        "description": description,
        "url": url,
        "source": "labonnealternance",
        "external_id": identifier.get("partner_job_id") or identifier.get("id"),
    }


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    if not ALTERNANCE.intersection(criteria.get("contract_types") or []):
        return []
    api_key = (cfg.get("labonnealternance") or {}).get("api_key", "").strip()
    if not api_key:
        log("labonnealternance : clé absente dans data/config.json, source ignorée")
        return []
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    cutoff = date.today() - timedelta(days=int(criteria.get("posted_within_days") or 30))
    out, seen = [], set(known_urls)
    with httpx.Client(headers=headers, timeout=25) as client:
        for loc in criteria.get("locations") or [{}]:
            params = {}
            if loc.get("lat") is not None and loc.get("lng") is not None:
                params.update(latitude=loc["lat"], longitude=loc["lng"],
                              radius=min(200, max(1, int(loc.get("radius_km") or 30))))
            elif loc.get("ft_departement"):
                params["departements"] = loc["ft_departement"]
            # Les mêmes partenaires sont déjà lus par les connecteurs dédiés.
            params["partners_to_exclude"] = ["Hellowork"]
            response = client.get(SEARCH_URL, params=params)
            if response.status_code == 429:
                log("labonnealternance : quota atteint, collecte reportée")
                break
            response.raise_for_status()
            items = response.json().get("jobs") or []
            kept = 0
            for item in items:
                normalized = _offer(item)
                if not normalized or normalized["url"] in seen:
                    continue
                if normalized["date_posted"] and normalized["date_posted"] < cutoff.isoformat():
                    continue
                seen.add(normalized["url"])
                out.append(normalized)
                kept += 1
            log(f"labonnealternance @ {loc.get('label', 'France')} : {len(items)} résultats, {kept} nouveaux")
    return out
