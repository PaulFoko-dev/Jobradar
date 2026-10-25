"""Contrats des nouvelles API : normalisation, secrets et filtrage sans accès réseau."""
from datetime import date

import httpx

from jobradar.sources import arbeitnow, labonnealternance, remoteok


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def response(data, status=200, url="https://example.test/api"):
    return httpx.Response(status, json=data, request=httpx.Request("GET", url))


def test_labonnealternance_ignore_stage_et_absence_de_cle() -> None:
    logs = []
    assert labonnealternance.collect({"contract_types": ["stage"]}, {}, set(), logs.append) == []
    assert labonnealternance.collect({"contract_types": ["alternance"]}, {}, set(), logs.append) == []
    assert "clé absente" in logs[-1]


def test_labonnealternance_normalise_et_filtre_date(monkeypatch) -> None:
    today = date.today().isoformat()
    jobs = [{
        "identifier": {"partner_job_id": "abc"},
        "workplace": {"name": "Acme", "location": {"address": "Paris 75001"}},
        "apply": {"url": "https://acme.test/jobs/abc"},
        "contract": {"type": ["Apprentissage"]},
        "offer": {"title": "Alternant cybersécurité", "description": "<p>Défense du SI</p>",
                  "desired_skills": ["Python"], "publication": {"creation": today + "T08:00:00Z"}},
    }]
    client = FakeClient([response({"jobs": jobs})])
    monkeypatch.setattr(labonnealternance.httpx, "Client", lambda **_kwargs: client)

    offers = labonnealternance.collect(
        {"contract_types": ["alternance"], "locations": [{"label": "Paris", "lat": 48.85,
                                                             "lng": 2.35, "radius_km": 30}]},
        {"labonnealternance": {"api_key": "secret"}}, set(), lambda _message: None)

    assert {key: offers[0][key] for key in ("title", "company", "contract", "url", "source")} == {
        "title": "Alternant cybersécurité", "company": "Acme", "contract": "Apprentissage",
        "url": "https://acme.test/jobs/abc", "source": "labonnealternance",
    }
    assert "Python" in offers[0]["description"]
    assert client.calls[0][1]["params"]["radius"] == 30


def test_arbeitnow_filtre_metier_zone_et_doublons(monkeypatch) -> None:
    payload = {"data": [
        {"title": "Cyber Security Intern", "company_name": "Acme", "location": "Paris, France",
         "remote": False, "tags": ["Internship"], "created_at": 1792886400,
         "description": "<p>SOC et pentest</p>", "url": "https://arbeitnow.com/jobs/1"},
        {"title": "Sales", "company_name": "Acme", "location": "Paris", "remote": False,
         "description": "Vente", "url": "https://arbeitnow.com/jobs/2"},
    ], "links": {"next": None}}
    monkeypatch.setattr(arbeitnow.httpx, "Client", lambda **_kwargs: FakeClient([response(payload)]))
    offers = arbeitnow.collect(
        {"search_queries": ["cyber security"], "include_keywords": ["pentest"],
         "locations": [{"label": "Paris", "search_term": "Paris"}]},
        {"results_per_query": 25}, set(), lambda _message: None)
    assert [offer["url"] for offer in offers] == ["https://arbeitnow.com/jobs/1"]
    assert offers[0]["description"] == "SOC et pentest"


def test_remoteok_ignore_les_mentions_et_conserve_attribution(monkeypatch) -> None:
    payload = [
        {"legal": "Please link back to Remote OK"},
        {"position": "Security Engineer", "company": "Acme", "location": "Worldwide",
         "tags": ["security", "python"], "description": "<p>Cloud security</p>",
         "url": "https://remoteok.com/remote-jobs/1", "date": "2026-10-24T08:00:00Z"},
    ]
    monkeypatch.setattr(remoteok.httpx, "get", lambda *_args, **_kwargs: response(payload))
    offers = remoteok.collect({"include_keywords": ["security"]}, {"results_per_query": 10}, set(),
                              lambda _message: None)
    assert offers == [{
        "title": "Security Engineer", "company": "Acme", "logo": None, "location": "Worldwide",
        "contract": "Télétravail security python", "date_posted": "2026-10-24",
        "published_at": "2026-10-24T08:00:00Z", "description": "Cloud security",
        "url": "https://remoteok.com/remote-jobs/1", "source": "remoteok",
    }]
