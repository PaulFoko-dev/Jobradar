import pytest

from jobradar import application_flow, db, inbox, notifications


@pytest.mark.parametrize(("subject", "body", "expected"), [
    ("Votre candidature chez Acme", "Merci pour votre candidature. Malheureusement, nous ne pouvons pas y donner suite.", "refus"),
    ("Application update", "Unfortunately we will not be moving forward with your application.", "refus"),
    ("Entretien - Stage SOC", "Nous souhaitons organiser un entretien avec vous.", "entretien"),
    ("Accusé de réception", "Nous avons bien reçu votre candidature et revenons vers vous.", "accuse"),
    ("Newsletter", "Découvrez nos offres de la semaine", None),
])
def test_messages_are_classified_locally(subject, body, expected) -> None:
    assert inbox.classify(subject, body) == expected


def test_messages_are_matched_to_a_single_sent_application() -> None:
    applications = [{"id": 1, "company": "Thales"}, {"id": 2, "company": "Groupe SII"}]
    assert inbox.match_application("Thales Careers <no-reply@thalesgroup.com>", "Votre candidature", "", applications)["id"] == 1
    assert inbox.match_application("noreply@workday.com", "Candidature chez SII", "", applications)["id"] == 2
    assert inbox.match_application("x@example.com", "Thales et SII", "", applications) is None  # ambigu


def test_scan_proposes_statuses_and_confirms_unconfirmed_submissions(client, monkeypatch) -> None:
    applications = []
    for company in ("Thales", "Wavestone"):
        created = client.post("/api/applications", json={"title": "Stage", "company": company,
                                                         "url": "https://example.com/" + company}).json()
        db.update_application(created["id"], {"status": "envoyee"})
        applications.append(created)
    confirmed = []
    monkeypatch.setattr(db, "list_unconfirmed_submissions",
                        lambda: [{"id": 99, "application_id": applications[1]["id"]}])
    monkeypatch.setattr(application_flow, "confirm_assisted_sent", lambda attempt_id: confirmed.append(attempt_id))
    messages = [
        {"id": "m1", "from": "Thales <rh@thalesgroup.com>", "subject": "Votre candidature",
         "body": "Malheureusement, nous ne donnerons pas suite.", "received": None},
        {"id": "m2", "from": "Wavestone <jobs@wavestone.com>", "subject": "Accusé de réception",
         "body": "Nous avons bien reçu votre candidature.", "received": None},
    ]
    result = inbox.scan(messages)
    assert result == {"enabled": True, "messages": 2, "proposals": 1, "confirmed": 1} and confirmed == [99]
    assert db.get_application(applications[0]["id"])["status"] == "envoyee"  # jamais changé seul
    proposals = [n for n in notifications.list_notifications() if n["kind"] == "response_detected"] \
        if hasattr(notifications, "list_notifications") else client.get("/api/notifications").json()
    assert any(n["kind"] == "response_detected" and n["payload"]["proposed_status"] == "refuse" for n in proposals)
    assert inbox.scan(messages)["proposals"] == 0  # une seule notification par message
