"""Grands groupes : Workday interrogé par requête, pays lu dans le détail, catalogue sans doublon."""
import httpx

from jobradar import config
from jobradar.sources import SOURCES, ats, groups

THALES = {"name": "Thales", "ats": "workday", "host": "thales.wd3.myworkdayjobs.com", "site": "Careers"}
CRITERIA = {"search_queries": ["stage pentest", "stage analyste SOC"], "include_keywords": ["pentest", "SOC"],
            "titles": [], "contract_types": ["stage"], "locations": [{"label": "Île-de-France"}]}


def _response(method, url, data):
    return httpx.Response(200, json=data, request=httpx.Request(method, url))


class FakeWorkday:
    """Liste : une offre en France, une en Inde, une hors sujet ; le détail donne ville et pays."""
    def __init__(self):
        self.searches, self.details = [], []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, json):
        self.searches.append(json["searchText"])
        postings = [] if json["searchText"] != "stage pentest" else [
            {"title": "STAGE - Développement d'outils de pentest F/H", "externalPath": "/job/Paris/Pentest_R1",
             "bulletFields": ["R1", "Intern/Trainee (Fixed Term)"]},
            {"title": "Pentest Intern", "externalPath": "/job/Bangalore/Pentest_R2", "bulletFields": ["R2"]},
            {"title": "STAGE - Contrôleur de gestion", "externalPath": "/job/Paris/Gestion_R3", "bulletFields": []},
        ]
        return _response("POST", url, {"total": len(postings), "jobPostings": postings})

    def get(self, url):
        self.details.append(url)
        country = "India" if "Bangalore" in url else "France"
        return _response("GET", url, {"jobPostingInfo": {
            "location": "Bangalore" if country == "India" else "Élancourt", "country": {"descriptor": country},
            "startDate": "2026-10-21", "jobDescription": "<p>Outils de test d'intrusion</p>",
            "externalUrl": f"https://thales.wd3.myworkdayjobs.com/Careers{url.split('/Careers')[1]}"}})


def test_workday_cherche_par_requete_et_garde_les_offres_en_france(monkeypatch) -> None:
    client = FakeWorkday()
    monkeypatch.setattr(ats.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(ats, "polite_pause", lambda *_args: None)

    offers = ats.collect(CRITERIA, {}, set(), lambda _message: None, companies=[THALES])

    assert client.searches == ["stage pentest", "stage analyste SOC"]
    assert [(o["title"], o["location"], o["date_posted"]) for o in offers] == [
        ("STAGE - Développement d'outils de pentest F/H", "Élancourt, France", "2026-10-21")]
    assert offers[0]["url"] == "https://thales.wd3.myworkdayjobs.com/Careers/job/Paris/Pentest_R1"
    assert offers[0]["description"] == "Outils de test d'intrusion"
    assert offers[0]["source"] == "carrières Thales"
    assert len(client.details) == 2  # l'offre hors sujet n'est jamais ouverte


def test_workday_ne_rouvre_pas_une_offre_deja_connue(monkeypatch) -> None:
    client = FakeWorkday()
    monkeypatch.setattr(ats.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(ats, "polite_pause", lambda *_args: None)
    known = {"https://thales.wd3.myworkdayjobs.com/Careers/job/Paris/Pentest_R1"}

    offers = ats.collect(CRITERIA, {}, known, lambda _message: None, companies=[THALES])

    assert all("Paris/Pentest_R1" not in url for url in client.details)
    assert [o["url"] for o in offers] == list(known)


def test_catalogue_n_interroge_pas_deux_fois_une_entreprise_declaree(monkeypatch) -> None:
    called = {}
    monkeypatch.setattr(ats, "collect", lambda *_args, companies: called.setdefault("companies", companies) and [])
    cfg = {"sources": {"ats": True}, "companies": [{"name": "Alten SA", "ats": "smartrecruiters", "slug": "alten"}]}

    groups.collect(CRITERIA, cfg, set(), lambda _message: None)

    names = {c["name"] for c in called["companies"]}
    assert "Alten" not in names and {"Thales", "Airbus", "Accenture"} <= names


def test_catalogue_actif_par_defaut_et_entrees_completes() -> None:
    assert config.DEFAULT_CONFIG["sources"]["grands_groupes"] is True
    assert SOURCES["grands_groupes"] is groups.collect
    for company in groups.GROUPS:
        assert company["ats"] in ats.FETCHERS
        assert ("host" in company and "site" in company) if company["ats"] == "workday" else company.get("slug")
