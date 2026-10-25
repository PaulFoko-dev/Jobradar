"""Pages carrières d'entreprises hébergées sur Greenhouse, Lever, SmartRecruiters ou Workday (API JSON publiques).

Les entreprises à surveiller se déclarent dans data/config.json → "companies" ; la source « grands_groupes »
ajoute le catalogue de groups.py. On récupère leurs offres (Workday : recherche par mot-clé côté serveur, faute
de pouvoir tout lister) puis on garde celles dont l'intitulé contient un mot-clé de la recherche.
"""
from datetime import datetime, timezone

import httpx

from .common import HEADERS, html_to_text, matches_any, polite_pause


def _greenhouse(client, c, _criteria):
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs", params={"content": "true"})
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield {
            "title": j["title"],
            "location": (j.get("location") or {}).get("name"),
            "date_posted": (j.get("updated_at") or "")[:10] or None,
            "description": html_to_text(html_to_text(j.get("content"))),  # contenu HTML échappé deux fois
            "url": j["absolute_url"],
        }


def _lever(client, c, _criteria):
    r = client.get(f"https://api.lever.co/v0/postings/{c['slug']}", params={"mode": "json"})
    r.raise_for_status()
    for j in r.json():
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        yield {
            "title": j["text"],
            "location": cat.get("location"),
            "contract": cat.get("commitment"),
            "date_posted": datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else None,
            "published_at": datetime.fromtimestamp(ts / 1000, timezone.utc) if ts else None,
            "description": j.get("descriptionPlain") or html_to_text(j.get("description")),
            "url": j["hostedUrl"],
        }


def _smartrecruiters(client, c, _criteria):
    api = f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings"
    for offset in range(0, 2000, 100):
        r = client.get(api, params={"limit": 100, "offset": offset, "country": "fr"})
        r.raise_for_status()
        page = r.json().get("content", [])
        for j in page:
            loc = j.get("location") or {}
            yield {
                "title": j["name"],
                "location": ", ".join(x for x in (loc.get("city"), "France") if x),
                "contract": (j.get("typeOfEmployment") or {}).get("label"),
                "date_posted": (j.get("releasedDate") or "")[:10] or None,
                "published_at": j.get("releasedDate"),
                "description": "",
                "url": f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                "_detail": f"{api}/{j['id']}",
            }
        if len(page) < 100:
            break


def _smartrecruiters_detail(client, url: str) -> str:
    r = client.get(url)
    if r.status_code != 200:
        return ""
    sections = ((r.json().get("jobAd") or {}).get("sections") or {}).values()
    return "\n\n".join(f"{s.get('title', '')}\n{html_to_text(s.get('text'))}" for s in sections if s.get("text"))


def _smartrecruiters_update(client, url: str) -> dict:
    return {"description": _smartrecruiters_detail(client, url)}


WORKDAY_PAGE = 20  # plafond imposé par l'API Workday
WORKDAY_PAGES = 2
WORKDAY_QUERIES = 10


def _workday_base(c) -> str:
    return f"https://{c['host']}/wday/cxs/{c['host'].split('.')[0]}/{c['site']}"


def _workday(client, c, criteria):
    """Une recherche par requête : un grand groupe publie des milliers d'offres, impossible de tout lister.

    Les requêtes de la recherche (« stage pentest ») ciblent mieux que la liste des mots-clés, dont les
    premiers sont souvent des variantes d'un même terme.
    """
    queries = criteria.get("search_queries") or criteria.get("include_keywords", []) + criteria.get("titles", [])
    seen = set()
    for keyword in list(dict.fromkeys(q for q in queries if q))[:WORKDAY_QUERIES]:
        for page in range(WORKDAY_PAGES):
            r = client.post(f"{_workday_base(c)}/jobs", json={
                "appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": page * WORKDAY_PAGE, "searchText": keyword})
            r.raise_for_status()
            postings = r.json().get("jobPostings") or []
            for j in postings:
                path = j.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                bullets = j.get("bulletFields") or []
                yield {
                    "title": j["title"],
                    "location": None,  # seulement la ville dans la liste : le pays vient du détail
                    "contract": next((b for b in bullets if matches_any(b, EARLY_CAREER_TITLE)), None),
                    "date_posted": None,
                    "description": "",
                    "url": f"https://{c['host']}/{c['site']}{path}",
                    "_detail": f"{_workday_base(c)}{path}",
                }
            if len(postings) < WORKDAY_PAGE:
                break
            polite_pause(0.2)


def _workday_update(client, url: str) -> dict:
    r = client.get(url)
    if r.status_code != 200:
        return {}
    info = r.json().get("jobPostingInfo") or {}
    place = info.get("jobRequisitionLocation") or {}
    country = ((place.get("country") or info.get("country") or {}).get("descriptor"))
    city = info.get("location") or place.get("descriptor")
    return {
        "location": ", ".join(x for x in (city, country) if x) or None,
        "date_posted": info.get("startDate"),
        "description": html_to_text(info.get("jobDescription")),
        "url": info.get("externalUrl") or None,
    }


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "smartrecruiters": _smartrecruiters, "workday": _workday}
# Détail chargé seulement pour les offres retenues et inconnues ; il peut compléter le lieu, d'où le second filtre.
DETAILS = {"smartrecruiters": _smartrecruiters_update, "workday": _workday_update}
EARLY_CAREER = {"stage", "alternance", "professionnalisation", "vie"}
EARLY_CAREER_TITLE = ["stage", "stagiaire", "intern", "trainee", "alternan", "apprenti", "v.i.e", "vie ", "graduate"]


def _in_zone(location: str | None, criteria: dict) -> bool:
    """Pré-filtre grossier : France ou une des zones demandées (l'IA affine ensuite)."""
    if not location:
        return True
    names = ["france", "remote"] + [l["label"].lower() for l in criteria.get("locations", [])]
    return any(n in location.lower() for n in names)


def collect(criteria: dict, cfg: dict, known_urls: set, log, companies: list[dict] | None = None) -> list[dict]:
    keywords = criteria.get("include_keywords", []) + criteria.get("titles", [])
    out = []
    with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        for c in cfg.get("companies", []) if companies is None else companies:
            fetch = FETCHERS.get(c.get("ats"))
            if not fetch:
                log(f"ats : type « {c.get('ats')} » inconnu pour {c.get('name')}")
                continue
            try:
                jobs = list(fetch(client, c, criteria))
            except Exception as e:
                log(f"ats {c['name']} : erreur {type(e).__name__}: {str(e)[:120]}")
                continue
            early = set(criteria.get("contract_types", [])) <= EARLY_CAREER
            kept = [
                j for j in jobs
                if matches_any(j["title"], keywords) and _in_zone(j.get("location"), criteria)
                and (not early or matches_any(j["title"] + " " + (j.get("contract") or ""), EARLY_CAREER_TITLE))
            ]
            retained = 0
            for j in kept:
                detail = j.pop("_detail", None)
                if detail and j["url"] not in known_urls:
                    try:
                        j.update({k: v for k, v in DETAILS[c["ats"]](client, detail).items() if v})
                    except Exception as e:
                        log(f"ats {c['name']} : détail illisible ({type(e).__name__})")
                    polite_pause(0.3)
                    if not _in_zone(j.get("location"), criteria):
                        continue
                retained += 1
                out.append({**j, "company": c["name"], "logo": c.get("logo"), "source": f"carrières {c['name']}"})
            log(f"ats {c['name']} : {len(jobs)} offres examinées, {retained} correspondent aux mots-clés")
            polite_pause(0.5)
    return out
