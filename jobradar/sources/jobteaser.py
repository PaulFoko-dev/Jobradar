"""JobTeaser : offres réservées aux étudiants, lues dans le navigateur dédié de JobRadar.

JobTeaser répond aux requêtes automatisées par une page anti-robot et réserve ses offres aux comptes étudiants
(connexion par l'école). JobRadar ne contourne ni l'une ni l'autre : la collecte passe par le navigateur visible de
JobRadar, avec ton profil, où tu te connectes une fois par ton école (session conservée d'une fois sur l'autre).

Connexion en trois niveaux :
1. session déjà ouverte dans le profil du navigateur : la collecte se fait seule ;
2. page de connexion de l'école avec un identifiant enregistré pour son domaine (Réglages → identifiants) : remplie
   et validée une fois ;
3. sinon (double authentification, captcha, aucun identifiant) : JobTeaser est sauté pour ce scan, sans bloquer les
   autres sources, et une seule notification par jour propose de te reconnecter.

Usage personnel, faible volume (pauses entre les pages) ; les conditions d'utilisation de JobTeaser s'appliquent.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .common import html_to_text, polite_pause, strip_contract_words

BASE = "https://www.jobteaser.com"
SEARCH = f"{BASE}/fr/job-offers"
LOGIN = f"{BASE}/fr/users/sign_in"
OFFER_PATH = re.compile(r"/(?:[a-z]{2}/)?job-offers/[0-9a-f-]{6,}[^/?#]*", re.I)
CHALLENGE_TITLES = ("un instant", "just a moment", "security checkup", "attention required", "vérification")
LOGIN_WORDS = ("connecte-toi", "se connecter", "connexion", "sign in", "log in", "mon ecole", "mon école")
MAX_QUERIES = 6
CONTRACTS = {"INTERN": "Stage", "INTERNSHIP": "Stage", "FULL_TIME": "CDI", "PART_TIME": "Temps partiel",
             "TEMPORARY": "CDD", "CONTRACTOR": "Freelance", "APPRENTICESHIP": "Alternance", "OTHER": None}


class NeedsUser(RuntimeError):
    """Connexion ou vérification à faire par l'utilisateur dans le navigateur de JobRadar."""


def search_url(query: str) -> str:
    return f"{SEARCH}?q={quote(query)}"


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.jobteaser.com", parts.path, "", ""))


def parse_listing(html: str) -> list[str]:
    """Liens des offres d'une page de résultats, dans l'ordre, sans doublon ni paramètre de suivi."""
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        match = OFFER_PATH.search(href)
        if match:
            url = clean_url(href if href.startswith("http") else BASE + match.group(0))
            if url not in links:
                links.append(url)
    return links


def _job_posting(html: str) -> dict | None:
    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                return item
    return None


def parse_offer(html: str, url: str) -> dict | None:
    """Offre normalisée depuis les données structurées (JobPosting), sinon depuis le titre et le texte de la page."""
    posting = _job_posting(html)
    if posting:
        location = posting.get("jobLocation") or {}
        location = location[0] if isinstance(location, list) and location else location
        address = location.get("address") or {} if isinstance(location, dict) else {}
        types = posting.get("employmentType")
        types = types if isinstance(types, list) else [types] if types else []
        contract = next((CONTRACTS.get(str(kind).upper()) for kind in types if CONTRACTS.get(str(kind).upper())), None)
        organization = posting.get("hiringOrganization") or {}
        title = str(posting.get("title") or "").strip()
        if not title:
            return None
        return {
            "title": title,
            "company": organization.get("name") if isinstance(organization, dict) else None,
            "location": ", ".join(str(value) for value in (address.get("addressLocality"), address.get("addressRegion"))
                                  if value) or None,
            "contract": contract,
            "date_posted": str(posting.get("datePosted") or "")[:10] or None,
            "published_at": posting.get("datePosted"),
            "description": html_to_text(posting.get("description")),
            "url": url, "source": "jobteaser",
        }
    soup = BeautifulSoup(html or "", "html.parser")
    heading = soup.find("h1")
    if heading is None or not heading.get_text(strip=True):
        return None
    main = soup.find("main") or soup.body or soup
    return {"title": heading.get_text(" ", strip=True), "company": None, "location": None, "contract": None,
            "date_posted": None, "description": main.get_text("\n", strip=True)[:20_000], "url": url,
            "source": "jobteaser"}


def page_state(title: str, url: str, text: str, has_password: bool, offer_links: int) -> str:
    """« challenge » (vérification anti-robot), « login » (connexion requise) ou « ready »."""
    folded = (title or "").casefold()
    if any(word in folded for word in CHALLENGE_TITLES):
        return "challenge"
    path = urlsplit(url).path.casefold()
    if has_password or "sign_in" in path or "/login" in path or "jobteaser.com" not in urlsplit(url).netloc:
        return "login"
    if not offer_links and any(word in (text or "").casefold()[:4000] for word in LOGIN_WORDS):
        return "login"
    return "ready"


def _wait_ready(page, timeout_s: int = 30) -> str:
    """Laisse passer la vérification du site (le navigateur visible la résout seul) ; renvoie l'état final."""
    state = "challenge"
    for _ in range(timeout_s):
        try:
            state = page_state(page.title(), page.url, page.locator("body").inner_text(timeout=3_000),
                               bool(page.locator('input[type="password"]:visible').count()),
                               page.locator("a[href*='job-offers/']").count())
        except Exception:  # noqa: BLE001 - page en cours de navigation : on relit un peu plus tard
            state = "challenge"
        if state != "challenge":
            return state
        page.wait_for_timeout(1_000)
    return state


def collect_with_page(page, criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    """Collecte dans une page déjà ouverte (navigateur de JobRadar, ou navigateur de test)."""
    from .. import application_browser  # import local : module lourd, inutile tant que la source est désactivée

    queries = list(dict.fromkeys(strip_contract_words(query) for query in criteria.get("search_queries", [])))
    limit = int(cfg.get("results_per_query") or 25)
    offers, seen = [], set(known_urls)
    for number, query in enumerate(queries[:MAX_QUERIES]):
        page.goto(search_url(query), wait_until="domcontentloaded", timeout=45_000)
        state = _wait_ready(page)
        if state == "login" and number == 0:
            # Niveau 2 : identifiant enregistré pour la page de connexion (JobTeaser ou SSO de l'école).
            if application_browser._try_login(page):
                page.goto(search_url(query), wait_until="domcontentloaded", timeout=45_000)
                state = _wait_ready(page)
        if state != "ready":
            raise NeedsUser("vérification anti-robot à passer" if state == "challenge" else "connexion requise")
        links = [url for url in parse_listing(page.content()) if url not in seen][:limit]
        log(f"jobteaser « {query} » : {len(links)} offres nouvelles")
        for url in links:
            seen.add(url)
            polite_pause(1.0)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                if _wait_ready(page, 15) != "ready":
                    continue
                offer = parse_offer(page.content(), url)
            except Exception as exc:  # noqa: BLE001 - une offre illisible n'arrête pas la collecte
                log(f"jobteaser : offre ignorée ({type(exc).__name__})")
                continue
            if offer:
                offers.append(offer)
        polite_pause(1.5)
    return offers


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    """Source « jobteaser » : navigateur de JobRadar, jamais en même temps que le robot de candidature."""
    from playwright.sync_api import sync_playwright

    from .. import application_browser, capture, config, notifications

    if not application_browser._headed_available():
        log("jobteaser : aucune session graphique (mise à jour programmée) ; collecte au prochain lancement manuel")
        return []
    lock = capture._profile_lock(config.system_profile_root())
    if not lock.acquire(timeout=120):
        log("jobteaser : navigateur occupé par une candidature ; collecte reportée au prochain scan")
        return []
    try:
        with sync_playwright() as playwright:
            port = application_browser.open_system_browser(playwright)["port"]
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                return collect_with_page(page, criteria, cfg, known_urls, log)
            except NeedsUser as exc:
                log(f"jobteaser : {exc} ; source ignorée pour ce scan")
                notifications.source_attention("jobteaser", "JobTeaser : reconnecte-toi dans le navigateur de JobRadar "
                                               f"({exc}). Les autres sources ont fonctionné normalement.")
                return []
            finally:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
    except application_browser.BrowserUnavailable as exc:
        log(f"jobteaser : navigateur indisponible ({exc})")
        return []
    finally:
        lock.release()
