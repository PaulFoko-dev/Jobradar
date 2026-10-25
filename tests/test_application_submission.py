"""Envoi assisté de bout en bout : un envoi n'est jamais présenté comme un échec, ni renvoyé en double."""
from __future__ import annotations

import http.server
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from jobradar import application_browser, application_flow, application_queue, capture, db, profile

FORM = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Candidature</title></head><body>
<form id="application_form" action="{action}" method="post" enctype="multipart/form-data">
<label for="email">E-mail *</label><input id="email" name="email" type="email" required>
<label for="resume">CV *</label><input id="resume" name="resume" type="file" required>
<button type="submit">Envoyer ma candidature</button></form></body></html>"""
SEARCH = """<form action="/recherche" method="get" class="search"><label for="k">Quoi ?</label>
<input id="k" name="k"><label for="l">Où ? Ville</label><input id="l" name="l"><button type="submit">Rechercher</button></form>"""
NEWSLETTER = """<form action="/newsletter" method="post" class="newsletter"><label for="nl">E-mail</label>
<input id="nl" name="email" type="email"><button type="submit">S'abonner</button></form>"""
JOBBOARD = """<!doctype html><html lang="fr"><body>{search}<h1>Stage DevSecOps</h1><p>Missions…</p>{link}</body></html>"""
CANDIDATE = {"identity": {"full_name": "Camille Durand", "email": "camille@example.test"}, "standard_answers": {}}


class _Site(http.server.BaseHTTPRequestHandler):
    """Formulaire local : /form confirme l'envoi, /form-muet renvoie une page sans confirmation."""
    posts: list[str] = []
    bodies: list[bytes] = []

    def do_GET(self) -> None:  # noqa: N802 - API de http.server
        pages = {"/form": FORM.format(action="/envoi"), "/form-muet": FORM.format(action="/envoi-muet"),
                 "/merci": "<html><body><h1>Merci pour votre candidature</h1></body></html>",
                 "/silence": "<html><body><p>Page suivante.</p></body></html>",
                 "/offre": JOBBOARD.format(search=SEARCH, link='<a href="/form">Postuler sur le site du recruteur</a>'),
                 "/offre-sans-lien": JOBBOARD.format(search=SEARCH, link=""),
                 "/double": "<html><body>" + NEWSLETTER + FORM.format(action="/envoi")[FORM.index("<form"):]}
        body = pages.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "").encode())

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Site.posts.append(self.path)
        _Site.bodies.append(body)
        self.send_response(303)
        self.send_header("Location", "/merci" if self.path == "/envoi" else "/silence")
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def site():
    _Site.posts, _Site.bodies = [], []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def invisible_browser(monkeypatch, tmp_path: Path):
    """Vrai Chromium, mais sans fenêtre : le contexte persistant est forcé en mode invisible."""
    real = application_browser.sync_playwright

    @contextmanager
    def headless():
        with real() as playwright:
            class Chromium:
                def __getattr__(self, name):
                    return getattr(playwright.chromium, name)

                def launch_persistent_context(self, *args, **kwargs):
                    return playwright.chromium.launch_persistent_context(*args, **kwargs | {"headless": True})

            class Wrapper:
                chromium = Chromium()

                def __getattr__(self, name):
                    return getattr(playwright, name)

            yield Wrapper()

    cv = tmp_path / "Camille_Durand_CV.pdf"
    cv.write_bytes(b"%PDF-1.4 CV de test")
    monkeypatch.setattr(application_browser, "sync_playwright", headless)
    monkeypatch.setattr(application_browser, "_headed_available", lambda: True)
    monkeypatch.setattr(application_browser, "_selected_files", lambda _attempt: {"cv": str(cv)})
    monkeypatch.setattr(capture, "is_offer_online", lambda _url, **_kw: True)
    monkeypatch.setattr(profile, "load_profile", lambda: CANDIDATE)


def _claimed_attempt(client, url: str, review_policy: str = "never") -> dict:
    application = client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme", "url": url}).json()
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                  {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    db.create_application_attempt(application["id"], mode="assisted", status="queued",
                                  cv_document_id=document["id"], letter_document_id=None, target_url=url,
                                  provider="generic", review_policy=review_policy)
    return db.claim_next_application_attempt()


def test_envoi_confirme_est_enregistre_et_ne_peut_pas_etre_renvoye(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/form")

    application_browser.process(attempt)

    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "submitted", saved
    assert saved["submit_clicked_at_utc"] and saved["submitted_at_utc"]
    assert db.get_application(attempt["application_id"])["status"] == "envoyee"
    assert db.get_document(attempt["cv_document_id"])["status"] == "envoye"
    assert _Site.posts == ["/envoi"]
    with pytest.raises(db.Conflict):
        application_flow.retry(attempt["id"])
    assert _Site.posts == ["/envoi"]


def test_envoi_non_confirme_demande_verification_sans_jamais_relancer(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/form-muet")

    application_browser.process(attempt)

    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "needs_user"
    assert saved["intervention"]["kind"] == "confirmation_missing"
    assert saved["intervention"]["action"] == "confirm_sent"
    assert _Site.posts == ["/envoi-muet"]
    for action in (application_flow.retry, application_flow.approve):
        with pytest.raises(db.Conflict):
            action(attempt["id"])
    with pytest.raises(db.Conflict, match="doublon"):
        db.answer_application_attempt(attempt["id"], [])
    assert db.get_application_attempt(attempt["id"])["status"] == "needs_user"
    assert _Site.posts == ["/envoi-muet"]


def test_confirmation_par_l_utilisateur_finalise_la_candidature(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/form-muet")
    application_browser.process(attempt)

    response = client.post(f"/api/application-attempts/{attempt['id']}/confirm-sent")

    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
    assert db.get_application(attempt["application_id"])["status"] == "envoyee"
    assert db.get_document(attempt["cv_document_id"])["status"] == "envoye"
    assert client.post(f"/api/application-attempts/{attempt['id']}/confirm-sent").status_code == 409


def test_elle_n_est_pas_partie_bascule_en_manuel(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/form-muet")
    application_browser.process(attempt)

    help_panel = application_flow.switch_to_manual(attempt["id"])

    assert help_panel["attempt"]["mode"] == "manual"
    assert db.get_application_attempt(attempt["id"])["status"] == "cancelled"


def test_redemarrage_apres_le_clic_ne_relance_pas_l_envoi(client) -> None:
    attempt = _claimed_attempt(client, "https://example.test/form")
    db.mark_submit_clicked(attempt["id"])

    db.recover_application_attempts()

    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "needs_user"
    assert saved["intervention"]["kind"] == "confirmation_missing"
    assert db.claim_next_application_attempt() is None


def test_redemarrage_avant_le_clic_reprend_normalement(client) -> None:
    attempt = _claimed_attempt(client, "https://example.test/form")

    db.recover_application_attempts()

    assert db.get_application_attempt(attempt["id"])["status"] == "queued"


def test_erreur_imprevue_apres_le_clic_n_est_pas_un_echec(client) -> None:
    attempt = _claimed_attempt(client, "https://example.test/form")
    db.transition_application_attempt(attempt["id"], expected={"running"}, status="queued")

    def crash_after_click(claimed: dict) -> None:
        db.mark_submit_clicked(claimed["id"])
        raise RuntimeError("panne après le clic")

    queue = application_queue.ApplicationQueue(crash_after_click)
    queue._thread.start()
    queue.wake()
    assert queue.wait_idle(10)
    queue.stop()

    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "needs_user"
    assert saved["intervention"]["kind"] == "confirmation_missing"


def test_page_d_offre_suit_le_lien_postuler_et_ne_soumet_jamais_la_recherche(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/offre")

    application_browser.process(attempt)

    assert db.get_application_attempt(attempt["id"])["status"] == "submitted"
    assert _Site.posts == ["/envoi"]  # jamais « /recherche » ni la barre de recherche


def test_page_d_offre_sans_formulaire_ni_lien_ne_remplit_ni_n_envoie_rien(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/offre-sans-lien")

    application_browser.process(attempt)

    saved = db.get_application_attempt(attempt["id"])
    assert saved["status"] == "needs_user"
    assert saved["intervention"]["kind"] == "not_application_form"
    assert saved["intervention"]["action"] == "manual_fallback"
    assert not saved["submit_clicked_at_utc"]
    assert _Site.posts == []


def test_remplissage_et_envoi_restent_dans_le_formulaire_de_candidature(client, site, invisible_browser) -> None:
    attempt = _claimed_attempt(client, f"{site}/double")

    application_browser.process(attempt)

    assert db.get_application_attempt(attempt["id"])["status"] == "submitted"
    assert _Site.posts == ["/envoi"]  # la newsletter, placée avant et dotée d'un champ « email », est intacte
    assert b"camille@example.test" in _Site.bodies[0]
