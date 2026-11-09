"""Banc d'essai du robot : parcours complets sur des formulaires calqués sur les échecs réels observés.

Chaque cas reproduit la structure d'une plateforme rencontrée (Société Générale, relais HelloWork, Phenom…).
Un cas qui passe est une régression interdite ; un cas ``xfail`` est un écart connu, mesuré, que les lots
« résolution locale des champs » et « robot autonome » doivent faire passer. Lancer :

    .venv/bin/python -m pytest tests/test_robot_bench.py -rxX
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest
import test_application_supervision as support
from test_application_supervision import supervised_browser  # noqa: F401 - fixture réutilisée

from jobradar import application_browser, db

# Société Générale : étape « Pièces jointes » ; choisir le fichier ne suffit pas, « Joindre » le dépose (aller-retour
# serveur), le tableau garde un ancien CV du compte coché, puis « Sauvegarder et continuer » (button type=submit).
SG_IDENTITY = """<!doctype html><html lang="fr"><body><h1>Candidature pour : Analyste Cybersécurité</h1>
<form id="f" method="post" action="/sg/identite" enctype="multipart/form-data">
<label for="email">Adresse e-mail *</label><input id="email" name="email" type="email" required>
<label for="nom">Nom complet *</label><input id="nom" name="nom" required>
<button type="button">Abandonner</button><button type="submit">Sauvegarder et continuer</button></form></body></html>"""

SG_ATTACH = """<!doctype html><html lang="fr"><body><h1>Pièces jointes</h1>
<form id="f" onsubmit="return false"><p>Si vous le souhaitez, complétez votre candidature.</p>
<div class="upload"><label for="up">Sélectionner le fichier</label><input id="up" type="file">
<input id="comment" type="text" placeholder="Commentaires sur le fichier">
<button type="button" id="join">Joindre</button></div>
<table id="docs"><thead><tr><th>Joindre</th><th>CV</th><th>Nom du fichier</th><th>Date</th><th>Actions</th></tr></thead>
<tbody><tr><td><input type="checkbox" checked></td><td><input type="checkbox" checked></td>
<td>Ancien_CV_Autre_Entreprise.pdf</td><td>01/10/26</td><td>Supprimer</td></tr></tbody></table>
<button type="button">Abandonner</button><button type="button">Sauvegarder comme brouillon</button>
<button type="submit" id="next">Sauvegarder et continuer</button></form>
<script>
join.onclick = () => { const file = up.files[0]; if (!file) return; setTimeout(() => {
  const row = document.createElement('tr');
  row.innerHTML = `<td><input type="checkbox"></td><td><input type="checkbox"></td><td>${file.name}</td><td>09/11/26</td><td>x</td>`;
  docs.tBodies[0].appendChild(row); up.value = ''; }, 300); };
next.onclick = async () => {
  const rows = [...docs.tBodies[0].rows].map(r => ({name: r.cells[2].innerText,
    join: r.cells[0].firstElementChild.checked, cv: r.cells[1].firstElementChild.checked}));
  await fetch('/sg/pieces', {method: 'POST', body: JSON.stringify(rows)}); location.href = '/sg/envoi'; };
</script></body></html>"""

SG_SEND = """<!doctype html><html lang="fr"><body><h1>Récapitulatif</h1>
<form method="post" action="/envoi"><label><input type="checkbox" name="rgpd" required>
J'accepte le traitement de ma candidature</label><button type="submit">Envoyer ma candidature</button></form></body></html>"""

# Relais HelloWork : la page d'offre ne contient qu'un lien « Postuler » vers l'ATS de l'entreprise.
RELAY = """<!doctype html><html lang="fr"><body><h1>Stage cybersécurité H/F</h1><p>Missions : audit, SOC.</p>
<a class="btn" href="/ats/formulaire">Postuler sur le site de l'entreprise</a></body></html>"""
ATS_FORM = """<!doctype html><html lang="fr"><body><form action="/envoi" method="post" enctype="multipart/form-data">
<label for="email">E-mail *</label><input id="email" name="email" type="email" required>
<label for="nom">Nom complet *</label><input id="nom" name="nom" required>
<label for="cv">CV *</label><input id="cv" name="cv" type="file" required>
<button type="submit">Envoyer ma candidature</button></form></body></html>"""

# Phenom (Thales) : étape 1 avec un button[type=submit] « Continuer » que le robot prenait pour l'envoi final.
PHENOM_STEP = """<!doctype html><html lang="fr"><body><form id="f" onsubmit="event.preventDefault(); step2()">
<div id="s1"><label for="email">E-mail *</label><input id="email" name="email" type="email" required>
<label for="nom">Nom complet *</label><input id="nom" name="nom" required>
<label for="cv">CV *</label><input id="cv" name="cv" type="file" required>
<button type="submit">Continuer</button></div></form>
<script>function step2() { f.innerHTML = `<label><input type="checkbox" id="ok" required>
J'accepte la politique de confidentialité</label><button type="button" id="send">Envoyer ma candidature</button>`;
send.onclick = async () => { await fetch('/envoi-json', {method: 'POST', body: '{}'}); location.href = '/merci'; }; }
</script></body></html>"""

# Workday « How did you hear about us? » : liste à recherche maison (pas de <select>), cause de verification_failed.
SEARCH_COMBO = """<!doctype html><html lang="fr"><body><form action="/envoi" method="post" enctype="multipart/form-data">
<label for="email">E-mail *</label><input id="email" name="email" type="email" required>
<label for="cv">CV *</label><input id="cv" name="cv" type="file" required>
<label for="src">How Did You Hear About Us? *</label>
<div><input id="src" role="combobox" aria-expanded="false" placeholder="Search" autocomplete="off" aria-required="true">
<ul id="opts" role="listbox" hidden><li role="option">Job Board</li><li role="option">LinkedIn</li>
<li role="option">Referral</li></ul></div><span id="chosen">0 items selected</span>
<button type="submit">Envoyer ma candidature</button></form>
<script>src.oninput = () => { opts.hidden = !src.value; };
opts.querySelectorAll('li').forEach(li => li.onclick = () => { chosen.textContent = li.textContent; src.value = '';
  opts.hidden = true; });
document.forms[0].onsubmit = e => { if (chosen.textContent.startsWith('0 items')) { e.preventDefault();
  alert('required'); } };</script></body></html>"""

PAGES = {"/sg/identite": SG_IDENTITY, "/sg/pieces": SG_ATTACH, "/sg/envoi": SG_SEND, "/relais": RELAY,
         "/ats/formulaire": ATS_FORM, "/phenom": PHENOM_STEP, "/combo": SEARCH_COMBO,
         "/merci": "<html><body><h1>Merci pour votre candidature</h1></body></html>"}


class _Bench(http.server.BaseHTTPRequestHandler):
    posts: list[tuple[str, bytes]] = []

    def do_GET(self) -> None:  # noqa: N802 - API de http.server
        body = PAGES.get(self.path.split("?")[0])
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def do_POST(self) -> None:  # noqa: N802
        _Bench.posts.append((self.path, self.rfile.read(int(self.headers.get("Content-Length") or 0))))
        target = {"/sg/identite": "/sg/pieces", "/envoi": "/merci"}.get(self.path)
        self.send_response(303 if target else 200)
        if target:
            self.send_header("Location", target)
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def bench():
    _Bench.posts = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Bench)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _run(client, bench, path: str) -> dict:  # noqa: F811 - fixture importée
    attempt = support._claimed_attempt(client, f"{bench}{path}")
    application_browser.process(attempt)
    return db.get_application_attempt(attempt["id"])


def test_bench_relay_page_leads_to_the_company_form(client, bench, supervised_browser, monkeypatch) -> None:  # noqa: F811
    support._scripted_ai(monkeypatch)
    saved = _run(client, bench, "/relais")
    assert saved["status"] == "submitted", saved["intervention"]
    assert [path for path, _ in _Bench.posts] == ["/envoi"]


def test_bench_continue_submit_button_is_a_step_not_the_final_send(client, bench, supervised_browser,  # noqa: F811
                                                                  monkeypatch) -> None:
    monkeypatch.setattr(application_browser.application_supervisor, "mode", lambda: "off")  # règles seules
    saved = _run(client, bench, "/phenom")
    # Jamais « envoi cliqué sans confirmation » sur un simple bouton d'étape.
    assert saved["intervention"] is None or saved["intervention"].get("kind") != "confirmation_missing", saved
    assert saved["submit_clicked_at_utc"] is None or [p for p, _ in _Bench.posts] == ["/envoi-json"]


def test_bench_societe_generale_attachments_then_send(client, bench, supervised_browser, monkeypatch,  # noqa: F811
                                                       tmp_path: Path) -> None:
    support._scripted_ai(monkeypatch)
    cv, letter = tmp_path / "Camille_CV_SG.pdf", tmp_path / "Camille_Lettre_SG.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    letter.write_bytes(b"%PDF-1.4 lettre")
    monkeypatch.setattr(application_browser, "_selected_files", lambda _a: {"cv": str(cv), "lettre": str(letter)})
    saved = _run(client, bench, "/sg/identite")
    import json

    pieces = next(json.loads(body) for path, body in _Bench.posts if path == "/sg/pieces")
    rows = {row["name"]: row for row in pieces}
    assert rows["Camille_CV_SG.pdf"]["join"] and rows["Camille_CV_SG.pdf"]["cv"]
    assert rows["Camille_Lettre_SG.pdf"]["join"] and not rows["Camille_Lettre_SG.pdf"]["cv"]
    assert not rows["Ancien_CV_Autre_Entreprise.pdf"]["join"]
    assert saved["status"] == "submitted", saved["intervention"]


def test_bench_search_combobox_is_filled_locally(client, bench, supervised_browser, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(application_browser.application_supervisor, "mode", lambda: "off")
    saved = _run(client, bench, "/combo")
    assert saved["status"] == "submitted", saved["intervention"]


@pytest.mark.parametrize(("value", "options", "expected"), [
    ("LinkedIn", ["Job Board", "LinkedIn", "Referral"], "LinkedIn"),
    ("linkedin", ["LinkedIn Jobs", "LinkedIn Ads"], "LinkedIn Ads"),
    ("Paris", ["Paris 15e Arrondissement", "Paris", "Parisot"], "Paris"),
    ("France", ["Afrique du Sud", "Île-de-France"], None),
    ("Lyon", ["Marseille"], None),
])
def test_bench_search_list_option_choice(value, options, expected) -> None:
    assert application_browser.best_option(value, options) == expected

