"""Isolation des tests : aucune donnée utilisateur n'est jamais ouverte.

``JOBRADAR_DATA_DIR`` et ``JOBRADAR_ARCHIVE_DIR`` sont fixés avant tout import
de ``jobradar`` : même un import du serveur (qui initialise la base) travaille
dans un dossier jetable. Chaque test qui touche à la base reçoit ensuite son
propre dossier de données et d'archives. Le module de capture est remplacé par
un faux pour tous les tests : aucun navigateur réel n'est lancé par les archives.
"""
import asyncio
import atexit
import itertools
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

_SESSION_DIR = tempfile.mkdtemp(prefix="jobradar-tests-")
os.environ["JOBRADAR_DATA_DIR"] = _SESSION_DIR
os.environ["JOBRADAR_ARCHIVE_DIR"] = os.path.join(_SESSION_DIR, "candidatures")
atexit.register(shutil.rmtree, _SESSION_DIR, ignore_errors=True)

from jobradar import application_queue, archive, config, db, mailer, notifications, preparation  # noqa: E402

FIXED_NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)  # 10 h à Paris


class MemoryKeyring(KeyringBackend):
    """Trousseau en mémoire : les tests n'écrivent jamais dans le trousseau réel."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("absent")


keyring.set_keyring(MemoryKeyring())  # avant tout test : le trousseau réel n'est même pas initialisé


@pytest.fixture(autouse=True)
def isolated_mail(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """Pour tous les tests : trousseau simulé, pas de variable de mot de passe, aucune connexion SMTP réelle."""
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    monkeypatch.delenv(mailer.ENV_PASSWORD, raising=False)
    monkeypatch.setattr(mailer, "_last_test", -float("inf"))

    def forbidden(*_a, **_k):
        raise AssertionError("connexion SMTP réelle interdite dans les tests")

    monkeypatch.setattr(mailer.smtplib, "SMTP", forbidden)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", forbidden)
    return memory


def fake_capture_module(level: str = "reconstituee", online: bool | None = None,
                        error: str | None = None) -> types.ModuleType:
    """Faux ``jobradar.capture`` respectant le contrat ; ``calls`` garde chaque appel."""
    module = types.ModuleType("jobradar.capture")
    module.calls = []

    def capture_offer(*, url, dest_dir, offer, browser_profile_dir, timeout_s=45, allow_headed=True):
        module.calls.append({"url": url, "dest_dir": dest_dir, "offer": offer,
                             "browser_profile_dir": browser_profile_dir, "allow_headed": allow_headed})
        pdf = None
        if level != "indisponible":
            pdf = dest_dir / "offre.pdf"
            pdf.write_bytes(b"%PDF-1.4 faux")
        return types.SimpleNamespace(level=level, pdf_path=pdf, png_path=None, error=error,
                                     captured_at_utc="2026-10-20T08:00:05Z")

    def is_offer_online(url, *, timeout_s=20):
        module.calls.append({"online": url})
        return online

    module.capture_offer = capture_offer
    module.is_offer_online = is_offer_online
    module.pdf_shows_wall = lambda pdf: b"mur de connexion" in pdf
    module.browser_status = lambda: {"available": False, "engine": None, "detail": "Navigateur simulé."}
    return module


def fake_render_module() -> types.ModuleType:
    """Faux moteur de rendu conforme au contrat, sans navigateur ni LibreOffice."""
    module = types.ModuleType("jobradar.render")
    module.calls = []

    class TemplateMap:
        def __init__(self, source_path):
            self.source_path = source_path

    class Report:
        def __init__(self, readability=100.0):
            self.readability = readability
            self.ready = readability >= 95

        def to_json(self):
            return {"readability": self.readability, "keyword_coverage": {"owned_present": 1,
                    "owned_total": 1, "missing_owned": [], "not_owned": []}, "issues": [],
                    "extracted_text": "CV simulé", "ready": self.ready}

    def analyze_docx_template(path):
        module.calls.append({"analyze": path})
        return TemplateMap(path)

    class RenderError(Exception):
        pass

    def render_document(content, identity, *, template, template_map, out_dir, base_name):
        if module.fail:
            raise RenderError(module.fail)
        module.calls.append({"content": content, "identity": identity, "template": template,
                             "template_map": template_map, "base_name": base_name})
        pdf, docx = out_dir / f"{base_name}.pdf", out_dir / f"{base_name}.docx"
        pdf.write_bytes(b"%PDF-1.4 rendu simule")
        docx.write_bytes(b"PK\x03\x04 rendu simule")
        return types.SimpleNamespace(pdf_path=pdf, docx_path=docx, warnings=list(module.render_warnings))

    module.RenderError = RenderError
    module.fail = ""
    module.render_warnings = []
    module.analyze_docx_template = analyze_docx_template
    module.render_cv = render_document
    module.render_letter = render_document
    module.ats_readability = 100.0
    module.ats_calls = []

    def ats_check(pdf_path, **kwargs):
        module.ats_calls.append({"pdf_path": pdf_path} | kwargs)
        return Report(module.ats_readability)

    module.ats_check = ats_check
    module.list_templates = lambda: [{"id": "sobre-ats", "name": "Sobre ATS",
                                      "description": "Modèle simulé", "ats": True}]
    return module


@pytest.fixture(autouse=True)
def fake_capture(monkeypatch: pytest.MonkeyPatch):
    """Faux module de capture pour tous les tests ; la file éventuellement démarrée est arrêtée à la fin."""
    module = fake_capture_module()
    monkeypatch.setitem(sys.modules, "jobradar.capture", module)
    yield module
    archive.stop_queue()


@pytest.fixture(autouse=True)
def fake_render(monkeypatch: pytest.MonkeyPatch):
    module = fake_render_module()
    monkeypatch.setitem(sys.modules, "jobradar.render", module)
    return module


@pytest.fixture(autouse=True)
def fake_application_browser(monkeypatch: pytest.MonkeyPatch):
    """Aucun test d'API ne doit ouvrir un formulaire externe ni soumettre une candidature."""
    def process(attempt):
        db.transition_application_attempt(
            attempt["id"], expected={"running"}, status="needs_user",
            intervention={"kind": "test_browser", "message": "Navigateur simulé.",
                          "action": "manual_fallback"},
        )

    monkeypatch.setattr(application_queue, "_process", process)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dossier de données propre au test, base migrée, horloge figée et bureau muet."""
    monkeypatch.setenv("JOBRADAR_ARCHIVE_DIR", str(tmp_path / "candidatures"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobradar.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "CLAUDE_CWD", tmp_path / "claude_cwd")
    monkeypatch.setattr(notifications, "notify", lambda *_a, **_k: False)
    monkeypatch.setattr(db, "utcnow", lambda: FIXED_NOW)
    db.init()
    return tmp_path


@pytest.fixture
def clock(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Fixe l'instant courant vu par JobRadar."""
    def set_now(instant: datetime) -> None:
        monkeypatch.setattr(db, "utcnow", lambda: instant)

    return set_now


@pytest.fixture
def write_config(data_dir: Path):
    """Écrit un config.json partiel ; les autres réglages gardent leur valeur par défaut."""
    def write(**overrides: object) -> None:
        config.CONFIG_PATH.write_text(json.dumps(overrides), encoding="utf-8")

    return write


@pytest.fixture
def search_id(data_dir: Path) -> int:
    return db.save_search({"name": "Stage cyber", "criteria": {"contract_types": ["stage"]}})


@pytest.fixture
def make_offer(search_id: int):
    """Insère une offre de stage (pertinente pour la recherche de test) et renvoie son identifiant."""
    counter = itertools.count(1)

    def make(**fields: object) -> int:
        n = next(counter)
        url = f"https://example.test/offre/{n}"
        offer = {
            "dedup_key": f"cle-{n}",
            "title": f"Stage analyste {n}",
            "company": "Acme",
            "url": url,
            "links": [{"source": "test", "url": url}],
        } | fields
        db.upsert_offer(search_id, offer)
        with db.connect() as c:
            return c.execute("SELECT id FROM offers WHERE dedup_key=?", (offer["dedup_key"],)).fetchone()[0]

    return make


@pytest.fixture
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    import anyio.to_thread
    import fastapi.dependencies.utils
    import fastapi.routing
    import httpx
    import starlette._exception_handler
    import starlette.background
    import starlette.datastructures
    import starlette.middleware.errors
    import starlette.routing

    from jobradar import server

    async def run_inline(callable_, *args, **kwargs):
        return callable_(*args, **kwargs)

    async def run_sync_inline(callable_, *args, **_kwargs):
        return callable_(*args)

    # Contournement ciblé d'une incompatibilité AnyIO/Python 3.14 : le worker
    # créé pour les endpoints synchrones reste endormi indéfiniment. Les tests
    # API n'exécutent aucune E/S distante ; les appeler dans la boucle est sûr.
    for module in (fastapi.routing, fastapi.dependencies.utils, starlette._exception_handler,
                   starlette.middleware.errors, starlette.routing, starlette.datastructures,
                   starlette.background):
        monkeypatch.setattr(module, "run_in_threadpool", run_inline)
    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync_inline)

    class LocalClient:
        """Client synchrone minimal au-dessus d'ASGITransport, sans portail AnyIO."""

        @staticmethod
        def request(method: str, url: str, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=server.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as session:
                    return await session.request(method, url, **kwargs)

            return asyncio.run(send())

        def get(self, url: str, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url: str, **kwargs):
            return self.request("POST", url, **kwargs)

        def put(self, url: str, **kwargs):
            return self.request("PUT", url, **kwargs)

        def patch(self, url: str, **kwargs):
            return self.request("PATCH", url, **kwargs)

        def delete(self, url: str, **kwargs):
            return self.request("DELETE", url, **kwargs)

    archive.start_queue()
    application_queue.start_queue()
    preparation.start_queue()
    try:
        yield LocalClient()
    finally:
        preparation.stop_queue()
        application_queue.stop_queue()
        archive.stop_queue()


@pytest.fixture(autouse=True)
def supervision_off(monkeypatch: pytest.MonkeyPatch):
    """La supervision IA de l'envoi n'appelle jamais l'IA réelle : les tests qui la couvrent la réactivent."""
    from jobradar import application_supervisor
    monkeypatch.setattr(application_supervisor, "mode", lambda: "off")
