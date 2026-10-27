"""Tests de la capture des offres, sans accès à Internet."""

from __future__ import annotations

import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from jobradar import capture

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "capture"
PDF_SIGNATURE = b"%PDF"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _browser_launchable() -> bool:
    """Vérifie un lancement réel : un exécutable présent peut être bloqué par le bac à sable."""
    try:
        with sync_playwright() as playwright:
            for options in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
                try:
                    browser = playwright.chromium.launch(headless=True, **options)
                except Exception:
                    continue
                browser.close()
                return True
    except Exception:
        return False
    return False


BROWSER_LAUNCHABLE = _browser_launchable()
requires_browser = pytest.mark.skipif(not BROWSER_LAUNCHABLE, reason="aucun Chromium ne peut être lancé")


class _FixtureHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self.server.requested_paths.append(self.path)  # type: ignore[attr-defined]
        super().do_GET()


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[tuple[str, list[str]]]:
    handler = partial(_FixtureHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.requested_paths = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.requested_paths  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def offer() -> dict:
    return {
        "title": "Stage analyste sécurité",
        "company": "Acme",
        "location": "Paris",
        "contract": "Stage",
        "date_posted": "2026-10-20",
        "description": "Analyse des alertes, investigations et amélioration continue des procédures.",
        "url": "https://example.test/offre",
        "links": [{"source": "site carrière", "url": "https://example.test/offre"}],
        "analysis": {
            "summary": "Une mission orientée réponse aux incidents.",
            "missions": ["Qualifier les alertes", "Documenter les investigations"],
            "profile": ["Connaissance de Linux", "Rigueur"],
        },
    }


def _capture(url: str | None, tmp_path: Path, offer: dict, **options: object) -> capture.CaptureResult:
    return capture.capture_offer(
        url=url,
        dest_dir=tmp_path / "archive",
        offer=offer,
        browser_profile_dir=tmp_path / "profil",
        timeout_s=10,
        allow_headed=False,
        **options,
    )


def _assert_pdf(path: Path | None) -> None:
    assert path is not None
    assert path.name == "offre.pdf"
    assert path.read_bytes().startswith(PDF_SIGNATURE)


def test_browser_status_respecte_le_contrat_public() -> None:
    status = capture.browser_status()

    assert set(status) == {"available", "engine", "detail"}
    assert isinstance(status["available"], bool)
    assert status["engine"] in {"chromium-playwright", "chrome", "msedge", None}
    assert isinstance(status["detail"], str)
    assert bool(status["engine"]) is status["available"]


@requires_browser
def test_capture_nominale_produit_un_pdf_et_un_png_valides(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/nominal.html", tmp_path, offer)

    assert result.level == "page"
    assert result.error is None
    _assert_pdf(result.pdf_path)
    assert result.png_path is not None
    assert result.png_path.name == "offre.png"
    assert result.png_path.read_bytes().startswith(PNG_SIGNATURE)
    assert result.captured_at_utc.endswith("Z")


@requires_browser
def test_capture_refuse_le_bandeau_cookies_avant_de_valider_la_page(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/cookies.html", tmp_path, offer)

    assert result.level == "page"
    _assert_pdf(result.pdf_path)
    assert result.png_path is not None


@pytest.mark.parametrize("fixture_name", ["vide.html", "mur.html", "connexion.html"])
@requires_browser
def test_page_vide_ou_mur_descend_vers_la_copie_reconstituee(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict, fixture_name: str
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/{fixture_name}", tmp_path, offer)

    assert result.level == "reconstituee"
    _assert_pdf(result.pdf_path)
    assert result.png_path is None
    assert result.error


@requires_browser
def test_reconstitution_echappe_le_contenu_et_ne_declenche_aucune_requete(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, requested_paths = fixture_server
    requested_paths.clear()
    piege = '<script>fetch("/exfiltration")</script><img src="/pixel-secret">'
    malicious_offer = offer | {"title": piege, "description": f"Description piégée : {piege}"}

    result = _capture(f"{base_url}/vide.html", tmp_path, malicious_offer)

    assert result.level == "reconstituee"
    _assert_pdf(result.pdf_path)
    assert all("exfiltration" not in path and "pixel-secret" not in path for path in requested_paths)

    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        pytest.skip("pdftotext absent : l'absence d'exécution du contenu piégé reste vérifiée")
    extracted = subprocess.run(
        [pdftotext, str(result.pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    assert "<script>" in extracted
    assert "<img src=" in extracted


def test_echec_total_conserve_les_captures_precedentes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offer: dict
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    old_pdf = b"%PDF-ancienne-capture"
    old_png = PNG_SIGNATURE + b"ancienne-capture"
    (archive / "offre.pdf").write_bytes(old_pdf)
    (archive / "offre.png").write_bytes(old_png)
    def unavailable_playwright() -> None:
        raise RuntimeError("Navigateur indisponible pour le test")

    monkeypatch.setattr(capture, "sync_playwright", unavailable_playwright)

    result = _capture("https://example.test/offre", tmp_path, offer)

    assert result.level == "indisponible"
    assert result.pdf_path is None
    assert result.png_path is None
    assert result.error
    assert (archive / "offre.pdf").read_bytes() == old_pdf
    assert (archive / "offre.png").read_bytes() == old_png


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://example.test/offre"])
@requires_browser
def test_url_non_http_est_refusee_sans_navigation(tmp_path: Path, offer: dict, url: str) -> None:
    result = _capture(url, tmp_path, offer)

    assert result.level == "reconstituee"
    _assert_pdf(result.pdf_path)
    assert result.png_path is None
    assert result.error and "URL" in result.error


def _png_size(path: Path) -> tuple[int, int]:
    return struct.unpack(">II", path.read_bytes()[16:24])


@requires_browser
def test_capture_pleine_page_et_fenetres_surgissantes_fermees(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/long.html", tmp_path, offer)

    # La mention reCAPTCHA d'une page longue n'est pas un mur ; la fenêtre ouverte par la page est ignorée.
    assert result.level == "page"
    assert result.error is None
    assert result.png_path is not None
    width, height = _png_size(result.png_path)
    # Deux pixels d'image par pixel CSS : la largeur vaut deux fois celle de la fenêtre (1 280 px).
    assert width == 2 * capture._VIEWPORT["width"]
    assert height > 2 * 2_600 and height % 2 == 0
    # Le paragraphe placé sous un grand espace, en bas de la page, figure dans le PDF.
    pdf_text = capture._pdf_text(result.pdf_path.read_bytes())
    assert "Fin de l'annonce : le bas de page doit figurer dans la capture pleine page." in " ".join(pdf_text.split())


@requires_browser
def test_dsf_2_effectif_sur_la_capture_nominale(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/nominal.html", tmp_path, offer)

    assert result.png_path is not None
    width, height = _png_size(result.png_path)
    assert (width, height % 2) == (2_560, 0)
    assert height >= 2 * capture._VIEWPORT["height"]


@requires_browser
def test_page_plus_haute_que_la_limite_donne_le_pdf_seul_sans_png_partiel(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server
    archive = tmp_path / "archive"
    archive.mkdir()
    # Une image d'une capture précédente ne doit pas rester à côté du nouveau PDF.
    (archive / "offre.png").write_bytes(PNG_SIGNATURE + b"ancienne")

    result = capture.capture_offer(
        url=f"{base_url}/tres_longue.html",
        dest_dir=archive,
        offer=offer,
        browser_profile_dir=tmp_path / "profil",
        timeout_s=40,
        allow_headed=False,
    )

    assert result.level == "page"
    assert result.png_path is None
    assert sorted(path.name for path in archive.iterdir()) == ["offre.pdf"]
    _assert_pdf(result.pdf_path)
    assert result.error and "seul le PDF" in result.error
    pdf_text = " ".join(capture._pdf_text(result.pdf_path.read_bytes()).split())
    assert "Dernier paragraphe de l'offre : candidature avant le trente novembre" in pdf_text


@requires_browser
def test_screenshot_refuse_une_page_trop_haute_et_garde_une_page_a_la_limite() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 400, "height": 300}, device_scale_factor=2)
            page.set_content(f"<body style='margin:0'><div style='height:{capture._MAX_HEIGHT_PX}px'>haut</div>")
            at_limit = capture._screenshot(page)
            page.set_content(f"<body style='margin:0'><div style='height:{capture._MAX_HEIGHT_PX + 1}px'>haut</div>")
            too_tall = capture._screenshot(page)
            segments = capture._screenshot_segments(page)
        finally:
            browser.close()
    assert at_limit is not None and capture._png_dimensions(at_limit) == (800, 2 * capture._MAX_HEIGHT_PX)
    assert too_tall is None
    # Repli de la fenêtre visible : des tranches sous la limite, qui couvrent toute la hauteur.
    assert [capture._png_dimensions(png) for png in segments] == [(800, 2 * capture._MAX_HEIGHT_PX), (800, 2)]


@requires_browser
def test_contenu_masque_par_la_feuille_d_impression_figure_dans_le_pdf(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/impression_masquee.html", tmp_path, offer)

    assert result.level == "page"
    assert result.error is None
    assert "automatisez les sauvegardes" in " ".join(capture._pdf_text(result.pdf_path.read_bytes()).split())


@requires_browser
def test_couverture_insuffisante_du_pdf_signalee_sans_echec(
    monkeypatch: pytest.MonkeyPatch, fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server
    monkeypatch.setattr(capture, "_pdf_text", lambda _pdf: "Stage analyste sécurité")

    result = _capture(f"{base_url}/nominal.html", tmp_path, offer)

    assert result.level == "page"
    _assert_pdf(result.pdf_path)
    assert result.png_path is not None
    assert result.error and "texte visible de la page" in result.error


def test_couverture_texte_compte_les_mots_et_leurs_repetitions() -> None:
    assert capture._text_coverage("Offre offre de stage", "offre de stage") == pytest.approx(3 / 4)
    assert capture._text_coverage("Développeur Python", "developpeur python") == 1
    assert capture._text_coverage("", "texte") is None


@requires_browser
def test_extraction_du_texte_pdf_concorde_avec_pdftotext(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server
    result = _capture(f"{base_url}/nominal.html", tmp_path, offer)
    assert result.pdf_path is not None
    extracted = capture._pdf_text(result.pdf_path.read_bytes())
    assert "Nous recherchons une personne curieuse" in " ".join(extracted.split())

    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        pytest.skip("pdftotext absent : la comparaison avec une extraction de référence est ignorée")
    reference = subprocess.run(
        [pdftotext, str(result.pdf_path), "-"], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    assert capture._text_coverage(reference, extracted) >= 0.98


@requires_browser
def test_telechargement_refuse_et_aucun_fichier_parasite(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    result = _capture(f"{base_url}/archive.zip", tmp_path, offer)

    assert result.level == "reconstituee"
    assert sorted(path.name for path in (tmp_path / "archive").iterdir()) == ["offre.pdf"]


@requires_browser
def test_niveau_navigateur_dedie_quand_la_page_sans_session_echoue(
    monkeypatch: pytest.MonkeyPatch, fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server

    def ephemeral_failure(*_args: object, **_kwargs: object) -> object:
        raise capture._CaptureFailure("Mur simulé.", blocked=True)

    monkeypatch.setattr(capture, "_capture_ephemeral", ephemeral_failure)

    result = _capture(f"{base_url}/nominal.html", tmp_path, offer)

    assert result.level == "page_navigateur_dedie"
    assert result.error == "Mur simulé."
    _assert_pdf(result.pdf_path)
    assert result.png_path is not None and result.png_path.read_bytes().startswith(PNG_SIGNATURE)
    assert any((tmp_path / "profil").iterdir())


@requires_browser
def test_conversion_png_vers_pdf_hors_ligne() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 300, "height": 200})
            page.set_content("<p style='font-size:40px'>Capture visible</p>")
            png = page.screenshot(type="png")
            pdf = capture._png_to_pdf(browser, (png,), deadline=time.monotonic() + 10)
        finally:
            browser.close()
    assert pdf.startswith(PDF_SIGNATURE)


@requires_browser
def test_impression_hors_ligne_ne_declenche_aucune_requete(
    monkeypatch: pytest.MonkeyPatch, fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, requested_paths = fixture_server
    requested_paths.clear()
    # Gabarit volontairement non échappé : seul le blocage réseau et JavaScript empêche ces requêtes.
    hostile = (
        f"<link rel='stylesheet' href='{base_url}/css-secret'><img src='{base_url}/pixel-secret'>"
        f"<iframe src='{base_url}/cadre-secret'></iframe><script>fetch('{base_url}/fetch-secret')</script>"
    )
    monkeypatch.setattr(capture, "_reconstructed_html", lambda *_args: hostile)

    result = _capture(None, tmp_path, offer)

    assert result.level == "reconstituee"
    _assert_pdf(result.pdf_path)
    assert requested_paths == []


@requires_browser
def test_captures_simultanees_dans_plusieurs_threads(
    fixture_server: tuple[str, list[str]], tmp_path: Path, offer: dict
) -> None:
    base_url, _requests = fixture_server
    results: dict[int, capture.CaptureResult] = {}

    def worker(number: int) -> None:
        results[number] = _capture(f"{base_url}/nominal.html", tmp_path / str(number), offer)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert [results[number].level for number in range(2)] == ["page", "page"]


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeBrowser:
    browser = _FakeBrowser()
    monkeypatch.setattr(capture, "sync_playwright", lambda: _NullPlaywright())
    monkeypatch.setattr(
        capture, "_launch_first_engine", lambda *_args, **_kwargs: (browser, capture._ENGINES[0])
    )
    return browser


class _NullPlaywright:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> None:
        return None


def test_navigateur_ferme_et_captures_conservees_si_tout_echoue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offer: dict
) -> None:
    browser = _fake_engine(monkeypatch)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "offre.pdf").write_bytes(b"%PDF-ancienne")
    (archive / "offre.png").write_bytes(PNG_SIGNATURE + b"ancienne")

    def failure(*_args: object, **_kwargs: object) -> object:
        raise capture._CaptureFailure("Échec simulé.")

    def crash(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("Navigateur planté")

    monkeypatch.setattr(capture, "_capture_ephemeral", failure)
    monkeypatch.setattr(capture, "_capture_persistent", failure)
    monkeypatch.setattr(capture, "_render_reconstructed", crash)

    result = _capture("https://example.test/offre", tmp_path, offer)

    assert result.level == "indisponible"
    assert result.error and "Échec simulé." in result.error
    assert browser.closed
    assert (archive / "offre.pdf").read_bytes() == b"%PDF-ancienne"
    assert (archive / "offre.png").read_bytes() == PNG_SIGNATURE + b"ancienne"
    assert sorted(path.name for path in archive.iterdir()) == ["offre.pdf", "offre.png"]


def test_navigateur_ferme_meme_sur_erreur_inattendue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offer: dict
) -> None:
    browser = _fake_engine(monkeypatch)

    def crash(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("Erreur inattendue")

    monkeypatch.setattr(capture, "_capture_ephemeral", crash)

    result = _capture("https://example.test/offre", tmp_path, offer)

    assert result.level == "indisponible"
    assert browser.closed


@pytest.mark.parametrize(("blocked", "headed_attempted"), [(True, True), (False, False)])
def test_fenetre_visible_reservee_aux_murs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offer: dict, blocked: bool, headed_attempted: bool
) -> None:
    _fake_engine(monkeypatch)
    modes: list[bool] = []

    def ephemeral(*_args: object, **_kwargs: object) -> object:
        raise capture._CaptureFailure("Échec sans session.", blocked=blocked)

    def persistent(*_args: object, headless: bool, **_kwargs: object) -> tuple[bytes, bytes, tuple[str, ...]]:
        modes.append(headless)
        if headless:
            raise capture._CaptureFailure("Échec du profil dédié.", blocked=blocked)
        return b"%PDF-visible", PNG_SIGNATURE + b"visible", ()

    monkeypatch.setattr(capture, "_capture_ephemeral", ephemeral)
    monkeypatch.setattr(capture, "_capture_persistent", persistent)
    monkeypatch.setattr(capture, "_render_reconstructed", lambda *_args, **_kwargs: b"%PDF-reconstituee")
    monkeypatch.setattr(capture, "_display_available", lambda: True)

    result = capture.capture_offer(
        url="https://example.test/offre",
        dest_dir=tmp_path / "archive",
        offer=offer,
        browser_profile_dir=tmp_path / "profil",
        timeout_s=10,
        allow_headed=True,
    )

    assert modes == ([True, False] if headed_attempted else [True])
    assert result.level == ("page_navigateur_dedie" if headed_attempted else "reconstituee")


def test_aucun_navigateur_donne_un_resultat_indisponible_sans_fichier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, offer: dict
) -> None:
    monkeypatch.setattr(capture, "sync_playwright", lambda: _NullPlaywright())
    monkeypatch.setattr(capture, "_launch_first_engine", lambda *_args, **_kwargs: None)

    result = _capture("https://example.test/offre", tmp_path, offer)

    assert result == capture.CaptureResult(
        "indisponible", None, None, result.error, result.captured_at_utc
    )
    assert result.error and "Chrome" in result.error
    assert list((tmp_path / "archive").iterdir()) == []


def test_dest_dir_relatif_est_une_erreur_de_programmation(tmp_path: Path, offer: dict) -> None:
    with pytest.raises(ValueError):
        capture.capture_offer(
            url="https://example.test/offre",
            dest_dir=Path("relatif"),
            offer=offer,
            browser_profile_dir=tmp_path / "profil",
        )


@pytest.mark.parametrize(
    ("url", "title", "text", "expected"),
    [
        ("https://www.linkedin.com/authwall?trk=x", "LinkedIn", "x" * 5_000, True),
        ("https://example.test/offre", "Just a moment...", "", True),
        ("https://example.test/offre", "Offre", "Vérifiez que vous êtes humain.", True),
        ("https://example.test/offre", "Offre", "Description. " * 300 + "Protected by reCAPTCHA.", False),
        ("https://example.test/offre", "Offre", "Offre courte. Protected by reCAPTCHA.", False),
        ("https://secure.indeed.com/auth;jsessionid=x?continue=y", "Indeed", "", True),
        ("https://fr.indeed.com/viewjob?jk=1&from=bot-detection-anonymous", "Offre", "x" * 5_000, True),
        ("https://example.test/users/sign-in?next=/offre", "Offre", "x" * 5_000, True),
        ("https://example.test/login", "Offre", "x" * 5_000, True),
        ("https://example.test/offres/stage-authentification-forte", "Offre", "x" * 5_000, False),
        ("https://example.test/offre", "Offre", "Ready to take the next step? Create an account or sign in.", True),
    ],
)
def test_detection_des_murs(url: str, title: str, text: str, expected: bool) -> None:
    assert capture._looks_like_wall(url, title, text) is expected


def test_formulaire_de_connexion_reconnu_a_son_champ_mot_de_passe() -> None:
    text = "Prêt pour la suite ? Adresse e-mail Mot de passe Continuer"
    assert capture._looks_like_wall("https://example.test/offre", "Offre", text, password_field=True)
    assert not capture._looks_like_wall("https://example.test/offre", "Offre", text)
    # Une vraie offre peut proposer la connexion dans son en-tête : seule une page courte est un mur.
    assert not capture._looks_like_wall("https://example.test/offre", "Offre", "x" * 5_000, password_field=True)


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: (requests.append(request), handler(request))[1])
    real_client = httpx.Client

    def mock_client(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(capture.httpx, "Client", mock_client)
    return requests


@pytest.mark.parametrize("status_code", [404, 410])
def test_is_offer_online_reconnait_une_offre_retiree_par_statut(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(status_code))
    assert capture.is_offer_online("https://example.test/offre") is False


@pytest.mark.parametrize(
    "body",
    [
        "Cette offre n'est plus disponible.",
        "Cette offre a expiré.",
        "This job is no longer available.",
        "We are no longer accepting applications.",
    ],
)
def test_is_offer_online_reconnait_les_messages_de_retrait(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(200, text=body))
    assert capture.is_offer_online("https://example.test/offre") is False


def test_is_offer_online_suit_les_redirections_et_envoie_un_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/depart":
            return httpx.Response(302, headers={"Location": "/offre"})
        return httpx.Response(200, text="Cette offre reste ouverte aux candidatures.")

    requests = _install_mock_transport(monkeypatch, handler)

    assert capture.is_offer_online("https://example.test/depart") is True
    assert [request.url.path for request in requests] == ["/depart", "/offre"]
    assert "Mozilla" in requests[0].headers["User-Agent"]


def test_is_offer_online_reste_indetermine_sur_un_formulaire_de_connexion(monkeypatch: pytest.MonkeyPatch) -> None:
    body = '<form>Adresse e-mail <input type="email"> <input type=password> Continuer</form>'
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(200, text=body))
    assert capture.is_offer_online("https://example.test/offre") is None


@pytest.mark.parametrize("status_code", [403, 429, 500, 503])
def test_is_offer_online_reste_indetermine_sur_blocage_ou_erreur_serveur(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(status_code))
    assert capture.is_offer_online("https://example.test/offre") is None


@pytest.mark.parametrize("body", ["Just a moment... Cloudflare", "Veuillez résoudre le captcha pour continuer"])
def test_is_offer_online_reste_indetermine_devant_un_mur(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(200, text=body))
    assert capture.is_offer_online("https://example.test/offre") is None


def test_is_offer_online_ignore_les_motifs_presents_dans_les_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    page = (
        "<html><head><title>Stage</title><script>const t = {expired: 'This job is no longer available'};</script>"
        "<script src='https://www.google.com/recaptcha/api.js'></script></head>"
        "<body><h1>Stage</h1>" + "<p>Description complète de la mission.</p>" * 200
        + "<footer>This site is protected by reCAPTCHA.</footer></body></html>"
    )
    _install_mock_transport(monkeypatch, lambda _request: httpx.Response(200, text=page))
    assert capture.is_offer_online("https://example.test/offre") is True


def test_is_offer_online_url_invalide_est_indeterminee() -> None:
    assert capture.is_offer_online("file:///etc/passwd") is None


def test_is_offer_online_convertit_une_erreur_reseau_en_resultat_indetermine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("réseau de test indisponible", request=request)

    _install_mock_transport(monkeypatch, unavailable)
    assert capture.is_offer_online("https://example.test/offre") is None
