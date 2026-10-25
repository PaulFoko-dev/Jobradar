"""HelloWork : page de recherche HTML, puis données structurées JSON-LD (JobPosting) de chaque offre."""
import json
import re

import httpx

from .common import HEADERS, html_to_text, polite_pause, strip_contract_words

BASE = "https://www.hellowork.com"
CONTRACT_PARAM = {
    "stage": "Stage", "alternance": "Alternance", "professionnalisation": "Alternance",
    "cdi": "CDI", "cdd": "CDD", "freelance": "Freelance", "interim": "Travail_temp",
}


def _parse_offer(html: str, url: str) -> dict | None:
    for m in re.finditer(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "JobPosting":
            continue
        loc = data.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address") or {}
        city = ", ".join(x for x in (addr.get("addressLocality"), addr.get("postalCode")) if x)
        et = data.get("employmentType")
        return {
            "title": html_to_text(data.get("title")),
            "company": (data.get("hiringOrganization") or {}).get("name"),
            "logo": (data.get("hiringOrganization") or {}).get("logo"),
            "location": city or None,
            "contract": ", ".join(et) if isinstance(et, list) else et,
            "date_posted": (data.get("datePosted") or "")[:10] or None,
            "published_at": data.get("datePosted"),
            "description": html_to_text(data.get("description")),
            "url": url,
            "source": "hellowork",
        }
    return None


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    contracts = sorted({CONTRACT_PARAM[c] for c in criteria.get("contract_types", []) if c in CONTRACT_PARAM})
    queries = list(dict.fromkeys(strip_contract_words(q) for q in criteria.get("search_queries", [])))
    out, seen = [], set()
    with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for loc in criteria.get("locations", []):
            for q in queries:
                for contract in contracts or [None]:
                    params = {"k": q, "l": loc["search_term"]}
                    if contract:
                        params["c"] = contract
                    ids = []
                    for page in (1, 2):
                        params["p"] = page
                        try:
                            r = client.get(f"{BASE}/fr-fr/emploi/recherche.html", params=params)
                        except httpx.HTTPError as e:
                            log(f"hellowork « {q} » : erreur {e}")
                            break
                        page_ids = list(dict.fromkeys(re.findall(r'href="/fr-fr/emplois/(\d+)\.html"', r.text)))
                        ids += page_ids
                        polite_pause(1)
                        if len(page_ids) < 20 or len(ids) >= cfg["results_per_query"]:
                            break
                    ids = ids[: cfg["results_per_query"]]
                    n = 0
                    for jid in ids:
                        url = f"{BASE}/fr-fr/emplois/{jid}.html"
                        if url in seen or url in known_urls:
                            continue
                        seen.add(url)
                        try:
                            offer = _parse_offer(client.get(url).text, url)
                        except httpx.HTTPError:
                            offer = None
                        polite_pause(0.8)
                        if offer and offer["title"]:
                            out.append(offer)
                            n += 1
                    log(f"hellowork « {q} » @ {loc['label']}{' (' + contract + ')' if contract else ''} : "
                        f"{len(ids)} résultats, {n} nouvelles offres lues")
    return out
