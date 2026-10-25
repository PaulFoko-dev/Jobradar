"""Contrats statiques du premier parcours de candidature de l'étape 5."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "jobradar/static/apply-ui.js"
STYLE = ROOT / "jobradar/static/apply.css"
APP = ROOT / "jobradar/static/app.js"
INDEX = ROOT / "jobradar/static/index.html"


def test_apply_module_is_valid_javascript() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js absent")
    subprocess.run([node, "--check", str(SCRIPT)], check=True, capture_output=True, text=True)


def test_offer_and_tracker_use_the_new_application_flow() -> None:
    app = APP.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    assert 'id="applyStart"' in app and 'id="appApply"' in app
    assert "JobRadarApply.open" in app
    assert '<script src="/static/apply-ui.js"></script>' in index
    assert '<link rel="stylesheet" href="/static/apply.css">' in index


def test_manual_panel_exposes_documents_identity_answers_and_confirmation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for route in ("/apply/options", "/apply/start", "/manual-sent"):
        assert route in source
    assert "standard_answers" in source and "data-copy-identity" in source
    assert "data-open-target" in source and "data-manual-sent" in source
    assert "safeUrl(data.target_url)" in source


def test_assisted_mode_exposes_queue_controls_and_explicit_approval() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")
    assert 'name="mode" value="assisted"' in source and "disabled" in source
    assert "data-cancel-attempt" in source and "data-retry-attempt" in source
    assert "data-manual-attempt" in source and "/manual" in source
    assert "data-approve-attempt" in source
    assert "/cancel" in source and "/retry" in source and "/approve" in source
    assert "data-answer-attempt" in source and "/answers" in source
    assert "Réponses préparées" in source
    assert 'name="reviewPolicy"' in source and "first_five" in source
    assert "always" in source and "never" in source
    assert "@media (max-width: 600px)" in style
