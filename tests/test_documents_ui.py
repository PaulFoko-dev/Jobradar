"""Contrats statiques du module autonome de préparation des documents."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "jobradar/static/documents-ui.js"
STYLE = ROOT / "jobradar/static/documents.css"
APP = ROOT / "jobradar/static/app.js"
INDEX = ROOT / "jobradar/static/index.html"


def test_documents_module_is_valid_javascript() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js absent")
    subprocess.run([node, "--check", str(SCRIPT)], check=True, capture_output=True, text=True)


def test_public_contract_and_api_routes_are_present() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "window.JobRadarUI" in source
    assert "window.JobRadarDocuments" in source
    for exported in ("prepare", "renderDocumentsTab", "bindDocumentsTab"):
        assert exported in source
    for route in ("/documents/prepare", "/documents/answers", "/documents/refine", "/documents/render", "/api/templates",
                  "/api/profile", "/ready", "/sent"):
        assert route in source
    assert "data-improve-document" in source
    assert "Générer une version améliorée" in source
    assert "/refine" in source


def test_dynamic_business_content_is_escaped_and_urls_are_constructed_locally() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "safeFileUrl" in source and "encodeURIComponent(name)" in source
    assert "javascript:" not in source.lower()
    assert "innerHTML = result" not in source
    assert "esc(note)" in source and "esc(paragraph)" in source and "esc(issue.message || issue)" in source


def test_safety_accessibility_and_responsive_contracts() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")
    assert "state.busy" in source
    assert "window.confirm" in source
    assert "aria-live=\"assertive\"" in source and "role=\"alert\"" in source
    assert "document.ats?.ready !== true" in source
    assert "@media (max-width: 760px)" in style
    assert "prefers-reduced-motion" in style


def test_documents_module_is_mounted_in_application_dialog() -> None:
    app = APP.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    assert 'id="applicationDocuments"' in app
    assert "JobRadarDocuments.renderDocumentsTab" in app
    assert "CV et lettres de motivation : bientôt" not in app
    assert '<script src="/static/documents-ui.js"></script>' in index


def test_answers_follow_the_strict_server_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'value="garder_une_ligne"' in source
    assert "question: label" in source and "answer:" in source and "remember:" in source
    assert "evidence_note" not in source


def test_questions_use_a_progressive_wizard_with_free_text_answers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")
    assert "bindQuestionWizard" in source
    assert "data-question-progress" in source and "data-question-bar" in source
    assert "data-question-next" in source and "data-question-prev" in source
    assert 'textarea name="skill-evidence-' in source
    assert "Précise librement ta réponse" in source
    assert ".doc-question-step[hidden]" in style
    assert ".doc-answer-choices" in style


def test_offer_and_apply_dialog_can_open_document_preparation_directly() -> None:
    app = APP.read_text(encoding="utf-8")
    apply_ui = (ROOT / "jobradar/static/apply-ui.js").read_text(encoding="utf-8")
    assert 'id="prepareDocsStart"' in app
    assert "openApplicationDocuments" in app
    assert "data-prepare-documents" in apply_ui
    assert "openApplicationDocuments(applicationId, true)" in apply_ui
