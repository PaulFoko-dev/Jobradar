"""Dépôt en deux temps (Société Générale, Taleo…) : « Joindre » puis cocher les lignes du tableau."""
from jobradar import application_browser

# Reproduction de l'étape « Pièces jointes » : le choix du fichier ne suffit pas, « Joindre » l'ajoute au
# tableau (après un délai réseau) et vide le champ ; un ancien CV du compte reste coché par défaut.
STAGING_FORM = """<form id="f" onsubmit="return false">
  <h2>Pièces jointes</h2>
  <div class="upload">
    <label for="up">Sélectionner le fichier</label><input id="up" type="file">
    <input id="comment" type="text" placeholder="Commentaires sur le fichier">
    <button type="button" id="join">Joindre</button>
  </div>
  <table id="docs"><thead><tr><th>Joindre</th><th>CV</th><th>Nom du fichier</th><th>Date</th>
    <th>Commentaires</th><th>Actions</th></tr></thead>
    <tbody><tr><td><input type="checkbox" checked></td><td><input type="checkbox" checked></td>
      <td>Ancien_CV_Autre_Entreprise.pdf</td><td>01/10/26</td><td></td><td>Supprimer</td></tr></tbody></table>
  <button type="button">Abandonner</button><button type="button">Sauvegarder comme brouillon</button>
  <button type="submit">Sauvegarder et continuer</button>
</form>
<script>
document.getElementById('join').onclick = () => {
  const input = document.getElementById('up'), comment = document.getElementById('comment');
  const file = input.files[0];
  if (!file) return;
  setTimeout(() => {
    const row = document.createElement('tr');
    row.innerHTML = `<td><input type="checkbox"></td><td><input type="checkbox"></td><td>${file.name}</td>
      <td>09/11/26</td><td>${comment.value}</td><td>Supprimer</td>`;
    document.querySelector('#docs tbody').appendChild(row);
    input.value = ''; comment.value = '';
  }, 400);
};
</script>"""


class _Journal:
    def __init__(self):
        self.notes = []

    def note(self, text):
        self.notes.append(text)


def _rows(page):
    return page.evaluate("""() => [...document.querySelectorAll('#docs tbody tr')].map(row => ({
        name: row.children[2].innerText, join: row.children[0].querySelector('input').checked,
        cv: row.children[1].querySelector('input').checked, comment: row.children[4].innerText}))""")


def test_cv_and_letter_are_joined_and_only_our_rows_are_ticked(tmp_path) -> None:
    from playwright.sync_api import sync_playwright

    cv, letter = tmp_path / "Camille_CV_Banque.pdf", tmp_path / "Camille_Lettre_Banque.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    letter.write_bytes(b"%PDF-1.4 lettre")
    plan = {"values": [{"selector": "#up", "value": str(cv), "kind": "file", "source": "document:cv",
                        "label": "Sélectionner le fichier"}], "interventions": [], "ready": True}
    journal = _Journal()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(STAGING_FORM)
        application_browser._fill(page, plan)

        staged = application_browser._stage_attachments(page, page, plan, {"cv": str(cv), "lettre": str(letter)},
                                                         journal)
        rows = {row["name"]: row for row in _rows(page)}
        browser.close()

    assert staged == ["cv", "lettre"]
    assert rows["Camille_CV_Banque.pdf"] == {"name": "Camille_CV_Banque.pdf", "join": True, "cv": True,
                                             "comment": "CV"}
    assert rows["Camille_Lettre_Banque.pdf"]["join"] and not rows["Camille_Lettre_Banque.pdf"]["cv"]
    assert rows["Camille_Lettre_Banque.pdf"]["comment"] == "Lettre de motivation"
    # Le CV adapté à une autre entreprise ne part pas avec cette candidature.
    assert not rows["Ancien_CV_Autre_Entreprise.pdf"]["join"] and not rows["Ancien_CV_Autre_Entreprise.pdf"]["cv"]


def test_a_file_already_joined_is_never_uploaded_twice(tmp_path) -> None:
    from playwright.sync_api import sync_playwright

    cv = tmp_path / "Camille_CV_Banque.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    plan = {"values": [{"selector": "#up", "value": str(cv), "kind": "file", "source": "document:cv",
                        "label": "Sélectionner le fichier"}], "interventions": [], "ready": True}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(STAGING_FORM)
        application_browser._stage_attachments(page, page, plan, {"cv": str(cv)})
        application_browser._stage_attachments(page, page, plan, {"cv": str(cv)})  # étape reprise
        names = [row["name"] for row in _rows(page)]
        browser.close()

    assert names.count("Camille_CV_Banque.pdf") == 1


def test_a_plain_file_field_is_left_to_the_usual_flow(tmp_path) -> None:
    from playwright.sync_api import sync_playwright

    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    plan = {"values": [{"selector": "#cv", "value": str(cv), "kind": "file", "source": "document:cv",
                        "label": "CV"}], "interventions": [], "ready": True}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content('<form><label for="cv">CV</label><input id="cv" type="file">'
                         '<button type="submit">Envoyer ma candidature</button></form>')
        staged = application_browser._stage_attachments(page, page, plan, {"cv": str(cv)})
        browser.close()

    assert staged == []


def test_save_and_continue_buttons_are_next_steps() -> None:
    for label in ("Sauvegarder et continuer", "Enregistrer et continuer", "Save & Continue", "Suivant"):
        assert application_browser.NEXT_LABELS.match(label), label
    assert not application_browser.NEXT_LABELS.match("Sauvegarder comme brouillon")


def test_a_save_and_continue_submit_button_is_a_step_not_the_final_send() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(STAGING_FORM)
        button = application_browser._step_button(page.locator("#f"), page)
        assert button is not None and application_browser._button_text(button) == "Sauvegarder et continuer"
        page.set_content('<form><button type="submit">Envoyer ma candidature</button></form>')
        assert application_browser._step_button(page.locator("form"), page) is None
        browser.close()


def test_account_is_created_automatically_and_its_password_stored_for_that_site_only(monkeypatch) -> None:
    from playwright.sync_api import sync_playwright

    stored = []
    monkeypatch.setattr(application_browser.login_credentials, "store",
                        lambda domain, user, password: stored.append((domain, user, len(password))))

    class Journal:
        def note(self, _text):
            pass

    supervision = type("S", (), {"values": {"email": "camille@example.test"}, "journal": Journal()})()
    form = """<form onsubmit="event.preventDefault(); document.body.dataset.done = '1'">
      <label for="m">Email Address</label><input id="m" type="email">
      <label for="p">Password</label><input id="p" type="password">
      <label for="v">Verify New Password</label><input id="v" type="password">
      <label><input type="checkbox" id="t"> I agree to the terms and conditions</label>
      <label><input type="checkbox" id="n"> Recevoir la newsletter</label>
      <button type="submit">Create Account</button></form>"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("https://acme.wd3.myworkdayjobs.com/**", lambda route: route.fulfill(body=form, content_type="text/html"))
        page.goto("https://acme.wd3.myworkdayjobs.com/fr/login")
        assert application_browser._create_account(page, supervision)
        assert page.input_value("#m") == "camille@example.test"
        assert page.input_value("#p") == page.input_value("#v") and len(page.input_value("#p")) >= 20
        assert page.is_checked("#t") and not page.is_checked("#n")
        assert page.evaluate("document.body.dataset.done") == "1"
        browser.close()
    assert stored == [("acme.wd3.myworkdayjobs.com", "camille@example.test", 24)]
