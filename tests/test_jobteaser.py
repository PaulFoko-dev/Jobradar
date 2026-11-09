"""Source JobTeaser : lecture des pages, états (vérification, connexion), parcours complet sur un faux site."""
import json

import pytest

from jobradar import config, notifications
from jobradar.sources import SOURCES, jobteaser

UUID_A = "3f2a9c1e-1b2c-4d5e-8f90-0a1b2c3d4e5f"
UUID_B = "7d6e5f40-aaaa-4bbb-8ccc-111122223333"
UUID_C = "0c0ffee0-1234-4567-89ab-cdef01234567"


def _listing(*uuids: str) -> str:
    cards = "".join(f'<a href="/fr/job-offers/{uuid}-stage-cybersecurite?utm_source=search">Stage {uuid[:4]}</a>'
                    for uuid in uuids)
    return f"""<html><head><title>Offres - JobTeaser</title></head><body><main>
      <a href="/fr/job-offers?q=cyber">Rechercher</a><a href="/fr/job-offers/internships">Stages</a>{cards}
      <a href="/fr/job-offers/{uuids[0]}-stage-cybersecurite">doublon</a></main></body></html>"""


def _offer(title: str, company: str) -> str:
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "datePosted": "2026-11-05",
               "employmentType": ["INTERN"], "hiringOrganization": {"@type": "Organization", "name": company},
               "jobLocation": {"@type": "Place", "address": {"addressLocality": "Paris", "addressRegion": "Île-de-France"}},
               "description": "<p>Missions : <b>SOC</b>, détection, réponse à incident.</p>"}
    return (f'<html><head><title>{title}</title><script type="application/ld+json">{json.dumps(posting)}</script></head>'
            f"<body><h1>{title}</h1></body></html>")


def test_listing_keeps_only_offer_links_without_tracking() -> None:
    links = jobteaser.parse_listing(_listing(UUID_A, UUID_B))
    assert links == [f"https://www.jobteaser.com/fr/job-offers/{UUID_A}-stage-cybersecurite",
                     f"https://www.jobteaser.com/fr/job-offers/{UUID_B}-stage-cybersecurite"]


def test_offer_is_read_from_structured_data_or_from_the_page() -> None:
    offer = jobteaser.parse_offer(_offer("Stage analyste SOC", "Thales"), "https://www.jobteaser.com/fr/job-offers/x")
    assert {key: offer[key] for key in ("title", "company", "location", "contract", "date_posted", "source")} == {
        "title": "Stage analyste SOC", "company": "Thales", "location": "Paris, Île-de-France", "contract": "Stage",
        "date_posted": "2026-11-05", "source": "jobteaser"}
    assert offer["company"] == "Thales" and offer["contract"] == "Stage" and "SOC" in offer["description"]
    fallback = jobteaser.parse_offer("<html><body><h1>Stage pentest</h1><main>Audit et tests.</main></body></html>", "u")
    assert fallback["title"] == "Stage pentest" and "Audit" in fallback["description"]
    assert jobteaser.parse_offer("<html><body><p>rien</p></body></html>", "u") is None


@pytest.mark.parametrize(("title", "url", "text", "password", "links", "expected"), [
    ("Un instant…", "https://www.jobteaser.com/fr/job-offers", "", False, 0, "challenge"),
    ("Connexion", "https://www.jobteaser.com/fr/users/sign_in", "", True, 0, "login"),
    ("CAS", "https://cas.mon-ecole.fr/login", "Identifiant", True, 0, "login"),
    ("Offres", "https://www.jobteaser.com/fr/job-offers", "Connecte-toi pour voir les offres", False, 0, "login"),
    ("Offres", "https://www.jobteaser.com/fr/job-offers", "120 offres", False, 12, "ready"),
])
def test_page_states(title, url, text, password, links, expected) -> None:
    assert jobteaser.page_state(title, url, text, password, links) == expected


def _fake_site(page, *, logged_in: bool = True, challenge: bool = False) -> None:
    listing = _listing(UUID_A, UUID_B, UUID_C)
    if challenge:  # page de vérification qui se résout seule après un court délai, comme dans un vrai navigateur
        listing = ("<html><head><title>Un instant…</title></head><body>Vérification…<script>setTimeout(() => {"
                   f"document.open(); document.write({json.dumps(listing)}); document.close(); }}, 1200);</script></body></html>")
    login = """<html><head><title>Connexion</title></head><body><form><input type="email" name="user">
      <input type="password" name="pw"><button type="submit">Se connecter</button></form></body></html>"""

    def handle(route):
        url = route.request.url
        if "/job-offers/" in url:
            name = {UUID_A: ("Stage analyste SOC", "Thales"), UUID_B: ("Stage pentest", "Wavestone"),
                    UUID_C: ("Stage DevSecOps", "Airbus")}
            uuid = next(key for key in name if key in url)
            route.fulfill(body=_offer(*name[uuid]), content_type="text/html; charset=utf-8")
        else:
            route.fulfill(body=listing if logged_in else login, content_type="text/html; charset=utf-8")

    page.context.route("https://www.jobteaser.com/**", handle)


def _run(monkeypatch, **site):
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(jobteaser, "polite_pause", lambda *_a: None)
    from jobradar import application_browser
    monkeypatch.setattr(application_browser.login_credentials, "for_host", lambda _host: None)
    logs = []
    criteria = {"search_queries": ["stage cybersécurité"]}
    known = {f"https://www.jobteaser.com/fr/job-offers/{UUID_B}-stage-cybersecurite"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _fake_site(page, **site)
        try:
            return jobteaser.collect_with_page(page, criteria, {"results_per_query": 25}, known, logs.append), logs
        finally:
            browser.close()


def test_collect_reads_new_offers_and_skips_known_ones(monkeypatch) -> None:
    offers, logs = _run(monkeypatch)
    assert [(offer["title"], offer["company"]) for offer in offers] == [("Stage analyste SOC", "Thales"),
                                                                         ("Stage DevSecOps", "Airbus")]
    assert all(offer["source"] == "jobteaser" and offer["contract"] == "Stage" for offer in offers)
    assert "2 offres nouvelles" in logs[0]


def test_collect_waits_for_the_site_check_to_clear(monkeypatch) -> None:
    offers, _logs = _run(monkeypatch, challenge=True)
    assert len(offers) == 2


def test_collect_stops_cleanly_on_a_login_wall(monkeypatch) -> None:
    with pytest.raises(jobteaser.NeedsUser, match="connexion"):
        _run(monkeypatch, logged_in=False)


def test_source_is_registered_disabled_by_default_and_skips_without_screen(monkeypatch) -> None:
    from jobradar import application_browser

    assert "jobteaser" in SOURCES and config.DEFAULT_CONFIG["sources"]["jobteaser"] is False
    monkeypatch.setattr(application_browser, "_headed_available", lambda: False)
    logs = []
    assert jobteaser.collect({"search_queries": ["x"]}, {}, set(), logs.append) == []
    assert "session graphique" in logs[0]


def test_reconnection_notice_is_sent_once_a_day(data_dir) -> None:
    assert notifications.source_attention("jobteaser", "JobTeaser : reconnecte-toi.")
    assert notifications.source_attention("jobteaser", "JobTeaser : reconnecte-toi.") is None


def test_settings_toggle_the_source(client) -> None:
    assert client.get("/api/sources/jobteaser").json() == {"enabled": False}
    assert client.put("/api/sources/jobteaser", json={"enabled": True}).json() == {"enabled": True}
    assert config.load_config()["sources"]["jobteaser"] is True
