"""API officielle France Travail « Offres d'emploi v2 » (identifiants gratuits sur francetravail.io)."""
import httpx

from .common import strip_contract_words

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
# France Travail ne publie pas de type « stage » : on interroge sans filtre de contrat
# et l'analyse IA écarte ce qui ne correspond pas.
NATURE_ALTERNANCE = {"alternance", "professionnalisation"}


def _token(cfg: dict) -> str:
    r = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["francetravail"]["client_id"],
            "client_secret": cfg["francetravail"]["client_secret"],
            "scope": "api_offresdemploiv2 o2dsoffre",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    if not cfg["francetravail"].get("client_id"):
        log("francetravail : identifiants absents dans data/config.json, source ignorée")
        return []
    headers = {"Authorization": f"Bearer {_token(cfg)}", "Accept": "application/json"}
    days = int(criteria.get("posted_within_days") or 31)
    days = next(d for d in (1, 3, 7, 14, 31) if d >= min(days, 31))
    out = []
    for loc in criteria.get("locations", []):
        geo = {}
        if loc.get("ft_commune"):
            geo = {"commune": loc["ft_commune"], "distance": loc.get("radius_km") or 30}
        elif loc.get("ft_departement"):
            geo = {"departement": loc["ft_departement"]}
        elif loc.get("ft_region"):
            geo = {"region": loc["ft_region"]}
        else:
            log(f"francetravail : pas de code géographique pour {loc['label']}, zone ignorée")
            continue
        for q in dict.fromkeys(strip_contract_words(q) for q in criteria.get("search_queries", [])):
            params = {"motsCles": q, "publieeDepuis": days, "range": "0-49", **geo}
            if NATURE_ALTERNANCE & set(criteria.get("contract_types", [])):
                params["natureContrat"] = "E2,FS"
            r = httpx.get(SEARCH_URL, params=params, headers=headers, timeout=25)
            if r.status_code == 204:
                continue
            if r.status_code >= 400:
                log(f"francetravail « {q} » : HTTP {r.status_code}")
                continue
            for o in r.json().get("resultats", []):
                url = (o.get("origineOffre") or {}).get("urlOrigine") or \
                    f"https://candidat.francetravail.fr/offres/recherche/detail/{o['id']}"
                out.append({
                    "title": o.get("intitule", ""),
                    "company": (o.get("entreprise") or {}).get("nom"),
                    "location": (o.get("lieuTravail") or {}).get("libelle"),
                    "contract": o.get("typeContratLibelle"),
                    "date_posted": (o.get("dateCreation") or "")[:10] or None,
                    "published_at": o.get("dateCreation"),
                    "description": o.get("description", ""),
                    "url": url,
                    "source": "francetravail",
                })
            log(f"francetravail « {q} » @ {loc['label']} : {len(r.json().get('resultats', []))} offres")
    return out
