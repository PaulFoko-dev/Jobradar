"""Tests des offres : date limite, publication, règles par entreprise et priorité."""
from datetime import datetime, timezone

import pytest

from jobradar import ai, db, pipeline


def _analysis(**fields: object) -> dict:
    return {"score": 70, "in_zone": True, "verdict": "Bon profil", "match_reasons": [],
            "application_deadline": "", "deadline_evidence": "", "deadline_confidence": 0} | fields


def _columns(offer_id: int) -> dict:
    with db.connect() as c:
        return dict(c.execute(
            """SELECT application_deadline_date, deadline_raw, deadline_source, deadline_extracted_at_utc,
                      deadline_confidence FROM offers WHERE id=?""", (offer_id,)).fetchone())


def _offer(search_id: int, offer_id: int) -> dict:
    return next(o for o in db.list_offers(search_id) if o["id"] == offer_id)


# ---------------------------------------------------------------- date limite

def test_save_analysis_enregistre_une_date_limite_valide(make_offer, search_id) -> None:
    oid = make_offer(date_posted="2026-10-15")
    db.save_analysis(oid, _analysis(application_deadline="2026-10-24",
                                    deadline_evidence="Candidatures   jusqu'au\n24 octobre",
                                    deadline_confidence=0.9))

    assert _columns(oid) == {
        "application_deadline_date": "2026-10-24",
        "deadline_raw": "Candidatures jusqu'au 24 octobre",
        "deadline_source": "ai",
        "deadline_extracted_at_utc": "2026-10-20T08:00:00Z",
        "deadline_confidence": 0.9,
    }
    assert _offer(search_id, oid)["deadline"] == {
        "date": "2026-10-24", "state": "a_venir", "urgent": True, "days_left": 4,
        "evidence": "Candidatures jusqu'au 24 octobre",
    }


@pytest.mark.parametrize(
    "deadline",
    ["", "24/10/2026", "2026-02-30", "20261024", "2026-10-24T12:00", "2027-10-21", "2026-10-13", None, 20261024],
)
def test_save_analysis_ignore_une_date_invalide_ou_implausible(make_offer, search_id, deadline) -> None:
    oid = make_offer(date_posted="2026-10-15")
    db.save_analysis(oid, _analysis(application_deadline=deadline, deadline_evidence="extrait",
                                    deadline_confidence=0.8))

    assert set(_columns(oid).values()) == {None}
    assert _offer(search_id, oid)["deadline"] == {
        "date": None, "state": "absente", "urgent": False, "days_left": None, "evidence": None,
    }


@pytest.mark.parametrize("deadline", ["2026-10-14", "2027-10-20"])
def test_save_analysis_accepte_les_bornes(make_offer, deadline) -> None:
    oid = make_offer(date_posted="2026-10-15")
    db.save_analysis(oid, _analysis(application_deadline=deadline))
    assert _columns(oid)["application_deadline_date"] == deadline


def test_borne_basse_utilise_linstant_de_publication(make_offer) -> None:
    oid = make_offer(published_at="2026-10-16T23:30:00Z")  # 17 octobre à Paris
    db.save_analysis(oid, _analysis(application_deadline="2026-10-15"))
    assert _columns(oid)["application_deadline_date"] is None
    db.save_analysis(oid, _analysis(application_deadline="2026-10-16"))
    assert _columns(oid)["application_deadline_date"] == "2026-10-16"


def test_confiance_bornee_et_extrait_tronque(make_offer) -> None:
    oid = make_offer()
    db.save_analysis(oid, _analysis(application_deadline="2026-11-30", deadline_evidence="x" * 300,
                                    deadline_confidence=1.7))
    cols = _columns(oid)
    assert cols["deadline_confidence"] == 1.0
    assert cols["deadline_raw"] == "x" * 120

    db.save_analysis(oid, _analysis(application_deadline="2026-11-30", deadline_confidence="sûr"))
    assert _columns(oid)["deadline_confidence"] is None
    assert _columns(oid)["deadline_raw"] is None


def test_etats_de_date_limite_dans_la_liste(make_offer, search_id) -> None:
    today, expired, far = make_offer(), make_offer(), make_offer()
    db.save_analysis(today, _analysis(application_deadline="2026-10-20"))
    db.save_analysis(expired, _analysis(application_deadline="2026-10-10"))
    db.save_analysis(far, _analysis(application_deadline="2026-12-01"))

    assert _offer(search_id, today)["deadline"] | {"evidence": None} == {
        "date": "2026-10-20", "state": "aujourdhui", "urgent": True, "days_left": 0, "evidence": None}
    assert _offer(search_id, expired)["deadline"]["state"] == "expiree"
    assert _offer(search_id, expired)["deadline"]["days_left"] == -10
    assert _offer(search_id, far)["deadline"]["urgent"] is False


def test_seuil_durgence_configurable(make_offer, search_id, write_config) -> None:
    oid = make_offer()
    db.save_analysis(oid, _analysis(application_deadline="2026-10-30"))
    assert _offer(search_id, oid)["deadline"]["urgent"] is False
    write_config(deadline_urgent_days=10)
    assert _offer(search_id, oid)["deadline"]["urgent"] is True


# ---------------------------------------------------------------- publication

@pytest.mark.parametrize(
    ("published_at", "expected"),
    [
        ("2026-10-19T10:00:00+02:00", "2026-10-19T08:00:00Z"),
        ("2026-10-19T08:00:00.000Z", "2026-10-19T08:00:00Z"),
        (datetime(2026, 10, 19, 8, tzinfo=timezone.utc), "2026-10-19T08:00:00Z"),
        ("2026-10-19T10:00:00", None),
        ("2026-10-19", None),
        ("hier", None),
        (None, None),
    ],
)
def test_publication_retenue_seulement_avec_fuseau(make_offer, search_id, published_at, expected) -> None:
    oid = make_offer(published_at=published_at, date_posted="2026-10-19")
    assert _offer(search_id, oid)["published_at_utc"] == expected


def test_un_doublon_complete_linstant_de_publication(make_offer, search_id) -> None:
    oid = make_offer(dedup_key="meme")
    make_offer(dedup_key="meme", published_at="2026-10-18T09:00:00+00:00")
    make_offer(dedup_key="meme", published_at="2026-10-19T09:00:00+00:00")
    assert _offer(search_id, oid)["published_at_utc"] == "2026-10-18T09:00:00Z"


# ---------------------------------------------------------------- règles par entreprise

def test_regles_entreprise_par_api(client) -> None:
    created = client.put("/api/company-rules", json={"company": "  Intrinsec  SAS ", "disposition": "favorite"})
    assert created.status_code == 200
    rule = created.json()
    assert rule["company_key"] == "intrinsec sas"
    assert rule["display_name"] == "Intrinsec SAS"
    assert rule["disposition"] == "favorite"

    updated = client.put("/api/company-rules", json={"company": "INTRINSEC SAS", "disposition": "excluded"}).json()
    assert updated["created_at"] == rule["created_at"]
    assert [(r["display_name"], r["disposition"]) for r in client.get("/api/company-rules").json()] == [
        ("INTRINSEC SAS", "excluded")]

    neutral = client.put("/api/company-rules", json={"company": "intrinsec sas", "disposition": "neutral"})
    assert neutral.json()["disposition"] == "neutral"
    assert client.get("/api/company-rules").json() == []


@pytest.mark.parametrize(
    "body",
    [{"company": "Acme", "disposition": "bof"}, {"company": "   ", "disposition": "favorite"},
     {"company": "", "disposition": "favorite"}, {"disposition": "favorite"}],
)
def test_regle_entreprise_invalide(client, body) -> None:
    response = client.put("/api/company-rules", json=body)
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


def test_list_offers_indique_la_regle_et_la_priorite(make_offer, search_id) -> None:
    neutre = make_offer(company="Neutre")
    favorite = make_offer(company="Favorite")
    exclue = make_offer(company="Exclue")
    urgente = make_offer(company="Autre")
    expiree = make_offer(company="Autre2")
    sans_score = make_offer(company="Autre3")
    db.save_analysis(neutre, _analysis(score=90))
    db.save_analysis(favorite, _analysis(score=60))
    db.save_analysis(exclue, _analysis(score=99))
    db.save_analysis(urgente, _analysis(score=40, application_deadline="2026-10-22"))
    db.save_analysis(expiree, _analysis(score=99, application_deadline="2026-10-10"))
    db.set_company_rule("favorite", "favorite")
    db.set_company_rule("EXCLUE", "excluded")

    offers = {o["id"]: o for o in db.list_offers(search_id)}
    assert offers[favorite]["company_rule"] == "favorite"
    assert offers[exclue]["company_rule"] == "excluded"
    assert offers[neutre]["company_rule"] == "neutral"
    ordre = sorted(offers, key=lambda i: offers[i]["priority_order"])
    assert ordre == [urgente, favorite, neutre, sans_score, expiree, exclue]
    assert sorted(o["priority_order"] for o in offers.values()) == list(range(6))


def test_deja_postule_chez_la_meme_entreprise(make_offer, search_id, clock) -> None:
    liee = make_offer(company="Acme")
    autre = make_offer(company="ACME ")
    ailleurs = make_offer(company="Globex")
    app_liee = db.create_application({"offer_id": liee})
    db.update_application(app_liee["id"], {"status": "envoyee"})
    clock(datetime(2026, 10, 21, 8, tzinfo=timezone.utc))

    offers = {o["id"]: o for o in db.list_offers(search_id)}
    assert offers[autre]["already_applied"] == {
        "application_id": app_liee["id"], "title": "Stage analyste 1", "status": "envoyee",
        "applied_at": "2026-10-20T08:00:00Z",
    }
    # L'offre liée à la candidature ne se signale pas elle-même ; une autre entreprise non plus.
    assert offers[liee]["already_applied"] is None
    assert offers[ailleurs]["already_applied"] is None


def test_deja_postule_ignore_les_candidatures_non_envoyees_ou_anciennes(make_offer, search_id) -> None:
    oid = make_offer(company="Acme")
    db.create_application({"title": "Brouillon", "company": "Acme"})
    ancienne = db.create_application({"title": "Ancienne", "company": "Acme", "status": "envoyee"})
    with db.connect() as c:
        c.execute("UPDATE applications SET applied_at_utc='2026-04-22T08:00:00Z' WHERE id=?", (ancienne["id"],))
    assert _offer(search_id, oid)["already_applied"] is None

    with db.connect() as c:
        c.execute("UPDATE applications SET applied_at_utc='2026-04-23T08:00:00Z' WHERE id=?", (ancienne["id"],))
    assert _offer(search_id, oid)["already_applied"]["application_id"] == ancienne["id"]


def test_api_offres_expose_les_nouveaux_champs(client, make_offer, search_id) -> None:
    make_offer()
    offer = client.get(f"/api/searches/{search_id}/offers").json()[0]
    assert {"deadline", "priority_order", "company_rule", "already_applied"} <= offer.keys()


# ---------------------------------------------------------------- pipeline

def test_pipeline_nenvoie_pas_les_entreprises_exclues_a_lia(make_offer, search_id, monkeypatch) -> None:
    gardee = make_offer(company="Acme", title="Stage pentest")
    exclue = make_offer(company="Exclue SA", title="Stage SOC")
    db.set_company_rule("exclue sa", "excluded")
    envoyees = []

    def faux_scoring(_crit: dict, batch: list[dict]) -> list[dict]:
        envoyees.extend(o["id"] for o in batch)
        return [{"id": o["id"], **_analysis(score=60)} for o in batch]

    monkeypatch.setattr(ai, "score_offers", faux_scoring)
    stats = pipeline.run_search(search_id, score_only=True)

    assert envoyees == [gardee]
    assert stats["scored"] == 1
    assert stats["pending"] == 0
    restante = _offer(search_id, exclue)
    assert restante["analysis"] is None
    assert restante["company_rule"] == "excluded"


def test_messages_de_validation_en_francais(client) -> None:
    detail = client.put("/api/company-rules", json={"company": "Acme", "disposition": "bof"}).json()["detail"]
    assert detail == ("Requête invalide — disposition : valeur non autorisée "
                      "(attendu : 'favorite', 'excluded' ou 'neutral')")
    detail = client.put("/api/company-rules", json={"disposition": "favorite"}).json()["detail"]
    assert detail == "Requête invalide — company : champ obligatoire"
