"""Garde-fous du moteur navigateur, sans lancer de navigateur réel."""
import pytest

from jobradar import application_adapters, application_browser, db


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


def test_fill_continues_when_one_dynamic_field_disappears() -> None:
    class Locator:
        first = None

        def __init__(self, selector):
            self.selector = selector
            self.first = self

        def fill(self, _value):
            if self.selector == "[name=gone]":
                raise application_browser.PlaywrightError("detached from DOM")

        def evaluate(self, _script):
            return None

    class Root:
        def locator(self, selector):
            return Locator(selector)

    monkey_plan = {"values": [
        {"selector": "[name=gone]", "kind": "text", "value": "A", "label": "Champ disparu"},
        {"selector": "[name=ok]", "kind": "text", "value": "B", "label": "Champ suivant"},
    ]}
    issues = application_browser._fill(Root(), monkey_plan)
    assert issues == [{"label": "Champ disparu", "reason": "remplissage impossible"}]


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

        def evaluate(self, *_args):
            return False

    page = Page()
    long_page = ("Description complète de l'offre. " * 100) + " Sign in"
    assert application_browser._blocked(page, long_page.casefold()) is False
    page.password = True
    assert application_browser._blocked(page, long_page.casefold()) is True


def test_unnamed_fields_and_checkbox_question_are_read_from_the_live_page() -> None:
    from playwright.sync_api import sync_playwright

    # Flatchr : ni name ni id, question posée par un simple <label> au-dessus des cases.
    html = """<form data-jobradar-form><input name="email" type="email" placeholder="E-mail">
    <div><div>Déposez votre CV ou cliquez ici</div><input type="file"></div>
    <div><label>Comment vous nous avez connu? *</label>
      <div><label><input type="checkbox"><p>Offre Linkedin</p></label></div>
      <div><label><input type="checkbox"><p>Site HGH.com</p></label></div></div>
    <div><label><input type="checkbox"> J'accepte la politique de confidentialité</label></div>
    <button type="submit">Envoyer</button></form>"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        match = application_browser._marked_match(page)
        fields = {field.label: field for field in match.fields}
        assert fields["Déposez votre CV ou cliquez ici"].kind == "file"
        assert fields["Site HGH.com"].group == "Comment vous nous avez connu? *"
        assert fields["J'accepte la politique de confidentialité"].group == ""
        plan = application_adapters.fill_plan(match.fields, {"identity": {}}, {})
        # Le site reconstruit son formulaire (React) : les repères disparaissent puis sont reposés à l'identique.
        page.evaluate("document.querySelectorAll('[data-jobradar-field]').forEach(el => "
                      "el.removeAttribute('data-jobradar-field'))")
        application_browser._fill(page.locator(match.selector).first, plan)
        assert page.locator("input[type=checkbox]").evaluate_all("els => els.map(el => el.checked)") == [
            False, True, False]
        browser.close()


def test_validation_errors_are_read_inside_the_application_form_only() -> None:
    from playwright.sync_api import sync_playwright

    html = """<header role="alert">Stage Ingénieur - cybersécurité applicative (H/F)</header>
    <form data-jobradar-form><input name="email" aria-invalid="false"><p role="alert"></p>
    <span role="alert">Paul_FOKO_CV.pdf successfully uploaded</span></form>"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        assert application_browser._validation_errors(page) == []
        page.evaluate("document.querySelector('form p').textContent = 'E-mail invalide'")
        assert application_browser._validation_errors(page) == ["E-mail invalide"]
        browser.close()


def test_custom_listbox_is_read_filled_and_verified() -> None:
    from playwright.sync_api import sync_playwright

    # Workday : un bouton « Select One » ouvre une liste d'options rendue hors du formulaire.
    html = """<form data-jobradar-form><div data-automation-id="formField-age">
      <label>Are you over 18 years of age?*</label>
      <button type="button" aria-haspopup="listbox" id="age">Select One</button></div>
      <input name="email" type="email"></form>
    <ul role="listbox" id="list" hidden><li role="option">Yes</li><li role="option">No</li></ul>
    <script>
      age.onclick = () => list.hidden = false;
      document.addEventListener('keydown', e => { if (e.key === 'Escape') list.hidden = true; });
      list.querySelectorAll('li').forEach(li => li.onclick = () => { age.textContent = li.textContent; list.hidden = true; });
    </script>"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        match = application_browser._marked_match(page)
        field = next(field for field in match.fields if field.kind == "listbox")
        assert field.label == "Are you over 18 years of age?*" and field.options == ("Yes", "No") and field.required
        assert page.locator("#list").is_hidden()  # refermée après lecture
        plan = {"values": [{"selector": field.selector, "value": "Yes", "kind": "listbox", "label": field.label}]}
        form = page.locator(match.selector).first
        application_browser._fill(form, plan)
        assert page.locator("#age").inner_text() == "Yes"
        assert application_browser._verify_filled(form, plan) == []
        browser.close()


def test_frozen_tab_is_reloaded_before_connecting(tmp_path) -> None:
    import socket

    from playwright.sync_api import sync_playwright

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    frozen = tmp_path / "fige.html"
    frozen.write_text("<p>Page figée</p><script>setTimeout(() => { while (true) {} }, 300);</script>")
    healthy = tmp_path / "ok.html"
    healthy.write_text("<p>Page normale</p>")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=[f"--remote-debugging-port={port}"])
        page = browser.new_page()
        page.goto(healthy.as_uri())
        stuck = browser.new_page()
        stuck.goto(frozen.as_uri())
        stuck.wait_for_timeout(800)  # la boucle infinie a démarré
        reloaded = application_browser._unfreeze_tabs(port)
        assert reloaded == [frozen.as_uri()]
        browser.close()
