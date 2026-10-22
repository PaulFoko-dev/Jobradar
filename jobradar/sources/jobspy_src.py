"""Indeed et LinkedIn via la bibliothèque python-jobspy (pages publiques, sans connexion)."""
import math

import httpx
from jobspy import scrape_jobs

from .common import HEADERS, html_to_text, polite_pause


def _str(v) -> str | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


def collect(site: str, criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    days = int(criteria.get("posted_within_days") or 30)
    out = []
    for loc in criteria.get("locations", []):
        for q in criteria.get("search_queries", []):
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=q,
                    location=loc["search_term"],
                    distance=max(10, round(int(loc.get("radius_km") or 0) * 0.62)) if loc.get("kind") == "city" else 50,
                    results_wanted=cfg["results_per_query"],
                    country_indeed="France",
                    hours_old=days * 24,
                    description_format="markdown",
                    verbose=0,
                )
            except Exception as e:  # le site peut bloquer ou changer de format
                log(f"{site} « {q} » @ {loc['label']} : erreur {type(e).__name__}: {str(e)[:120]}")
                polite_pause()
                continue
            n = 0
            for _, r in df.iterrows():
                url = _str(r.get("job_url"))
                title = _str(r.get("title"))
                if not url or not title:
                    continue
                n += 1
                out.append({
                    "title": title,
                    "company": _str(r.get("company")),
                    "location": _str(r.get("location")),
                    "contract": _str(r.get("job_type")),
                    "date_posted": _str(r.get("date_posted")),
                    "description": _str(r.get("description")) or "",
                    "url": url,
                    "apply_url": _str(r.get("job_url_direct")),
                    "logo": _str(r.get("company_logo")),
                    "source": site,
                })
            log(f"{site} « {q} » @ {loc['label']} : {n} offres")
            polite_pause()
    return out


def fetch_linkedin_details(url: str) -> tuple[str, str | None]:
    """LinkedIn ne renvoie ni description ni logo dans la recherche : on les lit sur l'API invité."""
    job_id = url.rstrip("/").split("/")[-1].split("?")[0]
    r = httpx.get(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
        headers=HEADERS, timeout=20, follow_redirects=True,
    )
    if r.status_code != 200:
        return "", None
    from bs4 import BeautifulSoup

    s = BeautifulSoup(r.text, "html.parser")
    node = s.select_one(".show-more-less-html__markup") or s.select_one(".description__text")
    criteria = " | ".join(li.get_text(" ", strip=True) for li in s.select(".description__job-criteria-item"))
    text = html_to_text(str(node)) if node else ""
    img = s.select_one("img.artdeco-entity-image, .top-card-layout__entity-image-container img")
    logo = img and (img.get("data-delayed-url") or img.get("src"))
    return (text + ("\n\n" + criteria if criteria else "")).strip(), (logo if logo and logo.startswith("http") else None)
