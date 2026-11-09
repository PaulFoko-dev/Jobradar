import json

from jobradar import application_browser, application_supervisor, redaction

PRIVATE = ["Camille Sentinelle", "camille.sentinelle@example.org", "+33 6 12 34 56 78", "12 rue Sentinelle"]
PAGE = """<html><head><meta name="csrf-token" content="SECRET-CSRF"><script>var t="SECRET-JS"</script></head>
<body><header>Bonjour Camille Sentinelle (camille.sentinelle@example.org)</header>
<form action="https://ats.example.com/apply?token=SECRET-URL#step2" onsubmit="SECRET-HANDLER()">
  <label for="mail">E-mail *</label><input id="mail" type="email" value="camille.sentinelle@example.org">
  <label for="tel">Téléphone</label><input id="tel" type="tel" value="06 12 34 56 78">
  <input type="hidden" name="authenticity_token" value="SECRET-HIDDEN">
  <input name="session_id" value="SECRET-SESSION">
  <select id="pays" aria-label="Pays"><option value="fr" selected>France</option><option>Belgique</option></select>
  <textarea name="motivation">Je suis Camille Sentinelle, au 12 rue Sentinelle.</textarea>
  <input type="checkbox" id="rgpd" checked><label for="rgpd">J'accepte le traitement de ma candidature</label>
  <a href="https://ats.example.com/job/42?utm_source=SECRET-UTM">Retour</a>
  <button type="submit">Envoyer ma candidature</button>
</form></body></html>"""


def test_sanitized_dom_keeps_the_structure_and_drops_every_private_value() -> None:
    html = redaction.sanitize_html(PAGE, PRIVATE)

    for secret in ("SECRET", "Camille", "Sentinelle", "sentinelle@", "56 78", "12 34"):
        assert secret.casefold() not in html.casefold(), secret
    # La structure utile au rejeu reste intacte.
    for kept in ('id="mail"', 'type="email"', "E-mail *", 'aria-label="Pays"', "Belgique", "France",
                 "J'accepte le traitement de ma candidature", "Envoyer ma candidature",
                 'action="https://ats.example.com/apply"', 'href="https://ats.example.com/job/42"'):
        assert kept in html, kept
    assert "selected" not in html and "checked" not in html and "<textarea name=\"motivation\"></textarea>" in html


def test_phone_numbers_are_masked_whatever_their_presentation() -> None:
    text = "Appelez le 06.12.34.56.78 ou le +33 (0)6 12 34 56 78"
    assert "34" not in redaction.redact_text(text, ["+33 6 12 34 56 78"])


class _Frame:
    url = "https://forms.example.com/embed?sid=SECRET-FRAME"

    def content(self):
        return "<form><input name='prenom' value='Camille Sentinelle'><label>Prénom</label></form>"


class _Page:
    url = "https://ats.example.com/apply?token=SECRET-URL"
    frames = [object(), _Frame()]

    def content(self):
        return PAGE

    def title(self):
        return "Candidature de Camille Sentinelle"


def test_save_dom_writes_a_sanitized_snapshot_and_an_index(tmp_path) -> None:
    path = application_supervisor.save_dom(_Page(), tmp_path, "etape-1", PRIVATE, page_kind="application_form")

    html = path.read_text(encoding="utf-8")
    assert "Prénom" in html and "forms.example.com/embed" in html
    assert "SECRET" not in html and "Sentinelle" not in html
    index = json.loads((tmp_path / application_supervisor.DOM_INDEX).read_text(encoding="utf-8"))
    assert index[0]["file"] == "dom-etape-1.html" and index[0]["page_kind"] == "application_form"
    assert index[0]["url"] == "https://ats.example.com/apply" and "Sentinelle" not in index[0]["title"]


def test_save_dom_never_raises(tmp_path) -> None:
    class Broken:
        def content(self):
            raise RuntimeError("page fermée")

    assert application_supervisor.save_dom(Broken(), tmp_path, "x", PRIVATE) is None


def test_private_values_cover_identity_and_standard_answers() -> None:
    candidate = {"identity": {"full_name": "Camille Sentinelle", "email": "c@example.org", "phone": "+33 6 12 34 56 78"},
                 "standard_answers": {"a": {"question": "Disponibilité", "answer": "Janvier 2027"}}}
    values = application_browser.private_values(candidate)
    assert {"Camille Sentinelle", "c@example.org", "Janvier 2027"} <= set(values)
