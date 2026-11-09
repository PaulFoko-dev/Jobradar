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


def test_verification_code_and_link_are_extracted_only_from_the_site() -> None:
    domains = {"myworkdayjobs.com", "thalesgroup.com"}
    code = {"from": "Workday <noreply@myworkdayjobs.com>", "subject": "Votre code de vérification",
            "body": "Votre code de vérification est 482913. Il expire dans 10 minutes."}
    link = {"from": "Thales <careers@thalesgroup.com>", "subject": "Activez votre compte",
            "body": "Cliquez ici : https://thales.wd3.myworkdayjobs.com/activate?token=abc123 pour activer."}
    other = {"from": "Banque <info@banque.fr>", "subject": "Votre code", "body": "Code 123456"}
    assert inbox.extract_verification(code, domains) == {"code": "482913", "link": None}
    assert inbox.extract_verification(link, domains)["link"] == "https://thales.wd3.myworkdayjobs.com/activate?token=abc123"
    assert inbox.extract_verification(other, domains) is None


@pytest.mark.parametrize("message", [
    # lien hors du site qui cite seulement le domaine dans son chemin ou ses paramètres
    {"from": "Support <help@pirate.example>", "subject": "Vérifiez votre compte",
     "body": "https://pirate.example/verify?r=myworkdayjobs.com"},
    {"from": "Support <help@pirate.example>", "subject": "Activez votre compte",
     "body": "https://pirate.example/myworkdayjobs.com/activate"},
    # domaine du site en identifiant, vrai hôte après « @ »
    {"from": "Support <help@pirate.example>", "subject": "Activez votre compte",
     "body": "https://myworkdayjobs.com@pirate.example/activate"},
    # expéditeur dont le domaine se termine seulement par le même texte
    {"from": "Workday <noreply@fauxmyworkdayjobs.com>", "subject": "Votre code de vérification",
     "body": "Votre code de vérification est 482913."},
])
def test_verification_ignores_look_alike_senders_and_links(message) -> None:
    assert inbox.extract_verification(message, {"myworkdayjobs.com"}) is None


def test_verification_ignores_domains_too_broad_to_name_a_site() -> None:
    message = {"from": "Shop <noreply@shop.co.uk>", "subject": "Votre code de vérification", "body": "Code 482913"}
    assert inbox.extract_verification(message, {"co.uk", ""}) is None
    assert inbox.extract_verification(message, {"shop.co.uk"}) == {"code": "482913", "link": None}


def test_robot_types_the_verification_code_received_by_email(monkeypatch) -> None:
    from playwright.sync_api import sync_playwright

    from jobradar import application_browser

    monkeypatch.setattr(inbox, "find_verification", lambda domains, since: {"code": "482913", "link": None})

    class Journal:
        notes = []

        def note(self, text):
            self.notes.append(text)

    supervision = type("S", (), {"journal": Journal()})()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content('<form onsubmit="return false"><label for="c">Code de vérification</label><input id="c" name="verification_code"></form>')
        used = set()
        assert application_browser._apply_email_verification(page, {"example.com"}, None, used, supervision)
        assert page.input_value("#c") == "482913"
        assert not application_browser._apply_email_verification(page, {"example.com"}, None, used, supervision)
        browser.close()
