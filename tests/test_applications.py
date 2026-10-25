"""Tests du suivi : date d'envoi, journal d'événements et relances guidées."""
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FIXED_NOW

from jobradar import ai, db


def _events(app_id: int) -> list[tuple[str, dict]]:
    return [(e["type"], e["payload"]) for e in db.list_events(app_id)]


def _sent_app(clock, days_ago: int, **fields: object) -> dict:
    """Candidature envoyée ``days_ago`` jours avant FIXED_NOW ; l'horloge revient ensuite à FIXED_NOW."""
    clock(FIXED_NOW - timedelta(days=days_ago))
    app = db.create_application({"title": "Stage SOC", "company": "Acme", "status": "envoyee"} | fields)
    clock(FIXED_NOW)
    return app


# ---------------------------------------------------------------- date d'envoi et événements

def test_creation_avec_statut_envoye_fixe_la_date_denvoi(data_dir) -> None:
    app = db.create_application({"title": "Stage SOC", "status": "envoyee"})
    assert app["applied_at"] == "2026-10-20T08:00:00Z"
    assert _events(app["id"]) == [("status_changed", {"from": None, "to": "envoyee"})]


def test_premier_statut_envoye_fixe_la_date_puis_ne_la_change_plus(clock) -> None:
    app = db.create_application({"title": "Stage SOC"})
    assert app["applied_at"] is None

    clock(datetime(2026, 10, 21, 9, tzinfo=timezone.utc))
    assert db.update_application(app["id"], {"status": "envoyee"})["applied_at"] == "2026-10-21T09:00:00Z"
    clock(datetime(2026, 10, 25, 9, tzinfo=timezone.utc))
    updated = db.update_application(app["id"], {"status": "entretien_rh"})
    assert updated["applied_at"] == "2026-10-21T09:00:00Z"
    assert db.update_application(app["id"], {"status": "entretien_rh"}) is not None  # sans changement

    assert _events(app["id"]) == [
        ("status_changed", {"from": None, "to": "consultee"}),
        ("status_changed", {"from": "consultee", "to": "envoyee"}),
        ("status_changed", {"from": "envoyee", "to": "entretien_rh"}),
    ]
    assert [h["status"] for h in updated["history"]] == ["consultee", "envoyee", "entretien_rh"]


def test_abandon_ne_fixe_pas_de_date_denvoi(data_dir) -> None:
    app = db.create_application({"title": "Stage SOC"})
    assert db.update_application(app["id"], {"status": "abandon"})["applied_at"] is None


def test_api_candidature_expose_les_nouveaux_champs(client) -> None:
    app = client.post("/api/applications", json={"title": "Stage SOC", "status": "envoyee"}).json()
    assert app["applied_at"] == "2026-10-20T08:00:00Z"
    assert app["interviews_count"] == 0
    # 10 h à Paris conservées malgré le passage à l'heure d'hiver du 25 octobre.
    assert app["follow_up"] == {"due_at": "2026-10-30T09:00:00Z", "is_due": False,
                                "reason": "La relance sera possible le 2026-10-30."}
    listed = client.get("/api/applications").json()[0]
    assert listed["follow_up"] == app["follow_up"]


def test_api_statut_inconnu_refuse(client) -> None:
    app = client.post("/api/applications", json={"title": "Stage SOC"}).json()
    response = client.patch(f"/api/applications/{app['id']}", json={"status": "gagne"})
    assert response.status_code == 400
    assert "status" in response.json()["detail"]
    assert client.patch("/api/applications/999", json={"notes": "x"}).status_code == 404


def test_api_offre_inconnue(client) -> None:
    response = client.post("/api/applications", json={"offer_id": 999})
    assert response.status_code == 404
    assert response.json()["detail"] == "Offre introuvable."


# ---------------------------------------------------------------- relances dues

def test_relances_dues_et_a_venir(client, clock) -> None:
    due = _sent_app(clock, 11)
    bientot = _sent_app(clock, 8)
    recente = _sent_app(clock, 2)
    relancee = _sent_app(clock, 20)
    db.update_application(relancee["id"], {"status": "relance"})
    desactivee = _sent_app(clock, 15)
    db.set_follow_up_disabled(desactivee["id"], True)

    body = client.get("/api/follow-ups").json()
    assert [a["id"] for a in body["due"]] == [due["id"]]
    assert [a["id"] for a in body["upcoming"]] == [bientot["id"]]
    assert body["due"][0]["follow_up"] == {"due_at": "2026-10-19T08:00:00Z", "is_due": True,
                                           "reason": "La relance est due."}
    ids = {a["id"] for group in body.values() for a in group}
    assert recente["id"] not in ids


def test_delai_de_relance_configurable(clock, write_config) -> None:
    app = _sent_app(clock, 5)
    assert not db.get_application(app["id"])["follow_up"]["is_due"]
    write_config(follow_up_delay_days=5)
    assert db.get_application(app["id"])["follow_up"]["is_due"]


def test_report_de_relance(client, clock) -> None:
    app = _sent_app(clock, 11)
    snoozed = client.post(f"/api/applications/{app['id']}/follow-up/snooze", json={"days": 3}).json()
    assert snoozed["follow_up"] == {"due_at": "2026-10-23T08:00:00Z", "is_due": False,
                                    "reason": "Relance reportée au 2026-10-23."}
    assert [a["id"] for a in client.get("/api/follow-ups").json()["upcoming"]] == [app["id"]]

    clock(FIXED_NOW + timedelta(days=3))
    assert db.get_application(app["id"])["follow_up"]["is_due"]


@pytest.mark.parametrize("days", [0, 31, "trois"])
def test_report_invalide(client, clock, days) -> None:
    app = _sent_app(clock, 11)
    response = client.post(f"/api/applications/{app['id']}/follow-up/snooze", json={"days": days})
    assert response.status_code == 400


def test_desactivation_et_reactivation(client, clock) -> None:
    app = _sent_app(clock, 11)
    disabled = client.post(f"/api/applications/{app['id']}/follow-up/disable").json()
    assert disabled["follow_up_disabled"] is True
    assert disabled["follow_up"] == {"due_at": None, "is_due": False, "reason": "Rappel de relance désactivé."}
    assert client.get("/api/follow-ups").json() == {"due": [], "upcoming": []}
    assert client.post(f"/api/applications/{app['id']}/follow-up/enable").json()["follow_up"]["is_due"]


@pytest.mark.parametrize("action", ["sent", "disable", "enable", "draft"])
def test_relance_candidature_inconnue(client, action) -> None:
    response = client.post(f"/api/applications/999/follow-up/{action}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Candidature introuvable."


# ---------------------------------------------------------------- brouillon et envoi

def test_brouillon_de_relance_avec_ia_simulee(client, clock, make_offer, monkeypatch) -> None:
    oid = make_offer(company="Acme")
    db.save_analysis(oid, {"score": 80, "in_zone": True, "summary": "Stage au SOC.", "missions": ["Analyse"]})
    clock(FIXED_NOW - timedelta(days=11))
    app = db.create_application({"offer_id": oid, "status": "envoyee"})
    db.update_application(app["id"], {"notes": "Envoyée via le site carrière"})
    clock(FIXED_NOW)
    contextes = []

    def faux_brouillon(context: dict) -> dict:
        contextes.append(context)
        return {"subject": "Relance candidature", "body": "Madame, Monsieur,\n…\n[Prénom Nom]"}

    monkeypatch.setattr(ai, "draft_follow_up", faux_brouillon)
    response = client.post(f"/api/applications/{app['id']}/follow-up/draft")

    assert response.status_code == 200
    assert response.json() == {"subject": "Relance candidature", "body": "Madame, Monsieur,\n…\n[Prénom Nom]"}
    assert contextes == [{
        "intitule": "Stage analyste 1", "entreprise": "Acme", "date_envoi": "2026-10-09",
        "notes": "Envoyée via le site carrière", "resume_offre": "Stage au SOC.", "missions": ["Analyse"],
    }]
    assert _events(app["id"])[-1] == ("follow_up_draft_created", response.json())


@pytest.mark.parametrize("error", [ai.QuotaError("Quota IA atteint : limite"), ai.AIError("Codex a échoué")])
def test_brouillon_erreur_ia_en_502(client, monkeypatch, error) -> None:
    app = db.create_application({"title": "Stage SOC", "status": "envoyee"})

    def echec(_context: dict) -> dict:
        raise error

    monkeypatch.setattr(ai, "draft_follow_up", echec)
    response = client.post(f"/api/applications/{app['id']}/follow-up/draft")
    assert response.status_code == 502
    assert response.json()["detail"] == str(error)
    assert all(kind != "follow_up_draft_created" for kind, _ in _events(app["id"]))


def test_marquer_relancee(client, clock) -> None:
    app = _sent_app(clock, 11)
    sent = client.post(f"/api/applications/{app['id']}/follow-up/sent").json()
    assert sent["status"] == "relance"
    assert sent["follow_up"]["is_due"] is False
    assert _events(app["id"])[-2:] == [
        ("status_changed", {"from": "envoyee", "to": "relance"}),
        ("follow_up_sent", {}),
    ]


def test_marquer_relancee_refusee_apres_une_reponse(client) -> None:
    app = db.create_application({"title": "Stage SOC", "status": "refuse"})
    response = client.post(f"/api/applications/{app['id']}/follow-up/sent")
    assert response.status_code == 409
    assert "refuse" in response.json()["detail"]
    assert db.get_application(app["id"])["status"] == "refuse"


# ---------------------------------------------------------------- adaptateur IA

def test_draft_follow_up_prompt_et_validation(monkeypatch) -> None:
    appel = {}

    def faux_ask(prompt: str, schema: dict, task: str) -> dict:
        appel.update(prompt=prompt, schema=schema, task=task)
        return {"subject": "  Relance \n candidature ", "body": " Bonjour\n[Prénom Nom] "}

    monkeypatch.setattr(ai, "ask_ai", faux_ask)
    draft = ai.draft_follow_up({"intitule": "Stage SOC", "entreprise": "Acme"})

    assert draft == {"subject": "Relance candidature", "body": "Bonjour\n[Prénom Nom]"}
    assert appel["task"] == "follow_up"
    assert appel["schema"] is ai.FOLLOW_UP_SCHEMA
    assert "[Prénom Nom]" in appel["prompt"]
    assert "120 mots" in appel["prompt"]
    assert "Stage SOC" in appel["prompt"]


def test_draft_follow_up_refuse_une_reponse_vide(monkeypatch) -> None:
    monkeypatch.setattr(ai, "ask_ai", lambda *_a: {"subject": "", "body": "x"})
    with pytest.raises(ai.AIError, match="incomplet"):
        ai.draft_follow_up({})


def test_schema_offre_contient_la_date_limite_et_reste_strict() -> None:
    for field in ("application_deadline", "deadline_evidence", "deadline_confidence"):
        assert field in ai.OFFER_SCHEMA["required"]
    strict = ai.strict_schema(ai.SCORING_SCHEMA)
    confidence = strict["properties"]["results"]["items"]["properties"]["deadline_confidence"]
    assert confidence == {"type": "number"}
