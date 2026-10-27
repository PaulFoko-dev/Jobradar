"""Garde-fous du moteur navigateur, sans lancer de navigateur réel."""
import pytest

from jobradar import application_browser, db


def _attempt(data_dir, provider="unknown") -> dict:
    application = db.create_application({"title": "Stage", "url": "https://example.test/job"})
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                  {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider=provider,
    )
    return db.claim_next_application_attempt()


def test_unsupported_provider_falls_back_without_browser(data_dir) -> None:
    attempt = _attempt(data_dir)
    application_browser.process(attempt)
    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "needs_user"
    assert saved["intervention"]["kind"] == "unsupported_form"


def test_supported_provider_requires_graphical_session(data_dir, monkeypatch) -> None:
    attempt = _attempt(data_dir, "greenhouse")
    monkeypatch.setattr(application_browser, "_headed_available", lambda: False)
    with pytest.raises(application_browser.BrowserUnavailable, match="session graphique"):
        application_browser.process(attempt)
    assert db.get_application_attempt(attempt["id"])["status"] == "running"


def test_verification_detects_a_value_changed_by_the_page() -> None:
    class Locator:
        first = None

        def __init__(self, value):
            self.value = value
            self.first = self

        def input_value(self):
            return self.value

    class Page:
        def locator(self, selector):
            return Locator({"[name=ok]": "Alice", "[name=changed]": "Valeur du site"}[selector])

    issues = application_browser._verify_filled(Page(), {"values": [
        {"selector": "[name=ok]", "kind": "text", "value": "Alice", "label": "Nom"},
        {"selector": "[name=changed]", "kind": "text", "value": "Attendu", "label": "Réponse"},
    ]})
    assert issues == [{"label": "Réponse", "reason": "valeur relue différente"}]


@pytest.mark.parametrize(("label", "is_final"), [
    ("Suivant", False), ("Save and continue", False), ("Envoyer ma candidature", True), ("Submit", True),
])
def test_workday_never_confuses_next_with_final_submission(label, is_final) -> None:
    class Button:
        def inner_text(self, timeout):
            assert timeout == 3000
            return label

    class Locator:
        first = Button()

        @staticmethod
        def count():
            return 1

    class Page:
        @staticmethod
        def locator(selector):
            assert "bottom-navigation-next-button" in selector
            return Locator()

    button, detected = application_browser._workday_navigation(Page())
    assert button is not None and detected is is_final


def test_workday_has_a_bounded_number_of_steps() -> None:
    assert application_browser.WORKDAY_MAX_STEPS == 8


def test_render_and_profile_lock_waits_are_bounded_but_robust() -> None:
    assert application_browser.RENDER_WAIT_MS == 15_000
    assert application_browser.APPLICATION_LOCK_TIMEOUT_S >= 20


@pytest.mark.parametrize(("policy", "submitted", "expected"), [
    ("always", 50, True), ("never", 0, False),
    ("first_five", 4, True), ("first_five", 5, False),
])
def test_review_policy_is_explicit_and_bounded(monkeypatch, policy, submitted, expected) -> None:
    monkeypatch.setattr(application_browser.db, "assisted_submitted_count", lambda: submitted)
    assert application_browser._review_required({"review_policy": policy}) is expected


@pytest.mark.parametrize(("url", "text", "expected"), [
    ("https://jobs.test/thank-you", "", True),
    ("https://jobs.test/apply", "Merci pour votre candidature", True),
    ("https://jobs.test/apply", "Formulaire envoyé", False),
])
def test_confirmation_uses_explicit_text_or_url(url, text, expected) -> None:
    page = type("Page", (), {"url": url})()
    assert application_browser._confirmed(page, text.casefold()) is expected


def test_block_detection_ignores_footer_link_but_detects_login_form() -> None:
    class Locator:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class Page:
        password = False

        def locator(self, selector):
            return Locator(1 if self.password and "password" in selector else 0)

    page = Page()
    long_page = ("Description complète de l'offre. " * 100) + " Sign in"
    assert application_browser._blocked(page, long_page.casefold()) is False
    page.password = True
    assert application_browser._blocked(page, long_page.casefold()) is True
