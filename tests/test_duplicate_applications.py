"""Candidatures en double (même offre, annonces différentes) : jamais deux envois, fusion automatique réversible."""
import pytest

from jobradar import application_flow, db, pipeline


def _app(client, title: str, company: str, url: str, *, status: str = "consultee", score: int = 80,
         location: str | None = None) -> dict:
    created = client.post("/api/applications", json={"title": title, "company": company, "url": url,
                                                     "score": score, "location": location}).json()
    if status != "consultee":
        db.update_application(created["id"], {"status": status})
    return db.get_application(created["id"])


def test_sending_a_twin_of_a_sent_application_is_blocked_even_on_another_page(client) -> None:
    sent = _app(client, "Stage Ingénieur - cybersécurité applicative (H/F)", "HGH Systèmes Infrarouges",
                "https://www.hellowork.com/fr-fr/emplois/1.html", status="envoyee", location="Igny")
    twin = _app(client, "Stage Ingénieur - cybersécurité applicative (H/F)", "HGH Infrared Systems",
                "https://www.linkedin.com/jobs/view/2", location="Igny")
    assert application_flow.already_applied_elsewhere(twin["id"])["id"] == sent["id"]
    with pytest.raises(db.Conflict, match="déjà postulé"):
        application_flow.start(twin["id"], mode="manual", cv_document_id=None, letter_document_id=None)


def test_duplicate_applications_are_merged_keeping_the_sent_or_the_company_one(client) -> None:
    hgh_sent = _app(client, "Stage Ingénieur - cybersécurité applicative (H/F)", "HGH Systèmes Infrarouges",
                    "https://ex.com/hgh-1", status="envoyee", location="Igny")
    hgh_twin = _app(client, "Stage Ingénieur - cybersécurité applicative (H/F)", "HGH Infrared Systems",
                    "https://ex.com/hgh-2", location="Igny")
    wavestone = _app(client, "Stage - Audits techniques et tests d'intrusion", "Wavestone", "https://ex.com/w", score=91)
    relay = _app(client, "Stage - Audits techniques et tests d'intrusion (H/F)", "Groupe Talents Handicap",
                 "https://ex.com/th", score=92)
    other = _app(client, "Stage analyste SOC", "Thales", "https://ex.com/soc")

    merged = application_flow.merge_duplicate_applications()

    assert sorted(merged) == sorted([(hgh_twin["id"], hgh_sent["id"]), (relay["id"], wavestone["id"])])
    assert db.get_application(hgh_twin["id"])["status"] == "abandon"
    assert "Doublon de la candidature" in db.get_application(relay["id"])["notes"]
    assert db.get_application(wavestone["id"])["status"] == "consultee"  # l'annonce de l'entreprise est gardée
    assert db.get_application(hgh_sent["id"])["status"] == "envoyee"
    assert db.get_application(other["id"])["status"] == "consultee"
    assert application_flow.merge_duplicate_applications() == []  # plus aucun doublon


def test_two_sent_twins_or_an_open_session_are_never_touched(client) -> None:
    _app(client, "Stage pentest", "Patrowl", "https://ex.com/p", status="envoyee")
    _app(client, "Stage pentest", "Patrowl", "https://ex.com/p?utm=x", status="envoyee")
    open_one = _app(client, "Stage SOC junior", "Acme", "https://ex.com/a1", status="envoyee")
    busy = _app(client, "Stage SOC junior", "Acme", "https://ex.com/a2")
    document = db.create_document(busy["id"], "cv", 1, {"kind": "cv"}, {}, {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    db.create_application_attempt(busy["id"], mode="manual", status="manual_open", cv_document_id=document["id"],
                                  letter_document_id=None, target_url=busy["url"], provider="generic")
    assert application_flow.merge_duplicate_applications() == []
    assert db.get_application(busy["id"])["status"] == "consultee" and open_one["status"] == "envoyee"


def test_offer_cleanup_merges_well_scored_duplicates_unless_followed(client, search_id, make_offer) -> None:
    keep = make_offer(title="STAGE 2027 - Design Graphique (h/f)", company="Airbus", location="Paris")
    twin = make_offer(title="Stage 2027 - Design Graphique H/F", company="Airbus Defense and Space", location="Paris")
    with db.connect() as conn:  # deux offres bien notées : avant, un score ≥ 50 empêchait toute fusion
        conn.execute("UPDATE offers SET score = CASE id WHEN ? THEN 60 ELSE 55 END WHERE id IN (?, ?)", (keep, keep, twin))
    pipeline.cleanup(search_id, db.get_search(search_id)["criteria"])
    remaining = {offer["id"] for offer in db.list_offers(search_id)}
    assert keep in remaining and twin not in remaining


def test_a_stuck_robot_attempt_on_a_duplicate_is_cancelled_with_it(client) -> None:
    sent = _app(client, "Stagiaire Pentester / Security Researcher", "Patrowl", "https://ex.com/patrowl", status="envoyee")
    twin = _app(client, "Stagiaire Pentester / Security Researcher", "Patrowl", "https://ex.com/patrowl?utm=indeed")
    document = db.create_document(twin["id"], "cv", 1, {"kind": "cv"}, {}, {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    attempt = db.create_application_attempt(twin["id"], mode="assisted", status="needs_user",
                                            cv_document_id=document["id"], letter_document_id=None,
                                            target_url=twin["url"], provider="generic")
    assert application_flow.merge_duplicate_applications() == [(twin["id"], sent["id"])]
    assert db.get_application_attempt(attempt["id"])["status"] == "cancelled"
    assert db.get_application(twin["id"])["status"] == "abandon"
