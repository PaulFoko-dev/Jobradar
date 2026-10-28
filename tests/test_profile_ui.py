"""Contrats statiques de la page locale « Mon profil »."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "jobradar/static/profile-ui.js"
STYLE = ROOT / "jobradar/static/profile.css"
INDEX = ROOT / "jobradar/static/index.html"


def test_profile_module_is_valid_javascript() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js absent")
    subprocess.run([node, "--check", str(SCRIPT)], check=True, capture_output=True, text=True)


def test_profile_page_exposes_the_complete_local_workflow() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    for route in ("/api/profile", "/api/profile/versions", "/api/profile/sources",
                  "/api/profile/conflicts/resolve", "/api/profile/restore/"):
        assert route in source
    assert 'id="profileNav"' in index and 'id="profileRoot"' in index
    assert '<script src="/static/profile-ui.js"></script>' in index


def test_profile_import_is_bounded_and_responsive() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")
    assert "10 * 1024 * 1024" in source
    assert "ACCEPTED" in source and "readBase64(file)" in source
    assert "esc(source.filename)" in source and "CSS.escape(conflict.id)" in source
    assert "@media (max-width: 640px)" in style


def test_profile_exposes_reusable_application_fields() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for field in ("address_line1", "postal_code", "region", "country", "availability", "notice_period",
                  "work_authorization", "sponsorship", "desired_salary"):
        assert field in source
    assert "standard_answers = { ...(current.standard_answers || {}) }" in source


def test_interface_is_revalidated_after_an_update(client) -> None:
    page = client.get("/")
    script = client.get("/static/profile-ui.js")
    assert page.headers["cache-control"] == script.headers["cache-control"] == "no-cache"
    assert "LINK_FIELDS" in script.text and 'name="link-${label}"' in script.text
    assert client.get("/static/profile-ui.js", headers={"if-none-match": script.headers["etag"]}).status_code == 304
