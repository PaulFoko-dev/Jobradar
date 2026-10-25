"""Le contrôle ATS doit être discriminant : chaque défaut de mise en page classique fait baisser la lisibilité."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from jobradar.render import AtsReport, ats_check, render_cv
from jobradar.render.chromium import print_variants
from jobradar.render.jobradar_html import cv_html

FIXTURES = Path(__file__).parent / "fixtures" / "render"


def _load(name: str) -> dict:
    path = FIXTURES / "compositions" / f"{name}.json"
    return json.loads((path if path.exists() else FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def cv() -> dict:
    return _load("stage_cyber")


@pytest.fixture
def identity() -> dict:
    return _load("identity")


def _pdf(tmp_path: Path, html: str, name: str = "controle.pdf") -> Path:
    pdf, _, _ = print_variants([html])
    path = tmp_path / name
    path.write_bytes(pdf)
    return path


def _html(composition: dict, identity: dict) -> str:
    return cv_html(composition, identity, "sobre-ats", title="CV", author="Camille Martin")


def _check(pdf: Path, composition: dict, identity: dict, **options: object) -> AtsReport:
    return ats_check(pdf, composition=composition, identity=identity, offer_keywords=options.get("offer", []),
                     owned_keywords=options.get("owned", []), docx_path=options.get("docx"))


def _codes(report: AtsReport) -> set[str]:
    return {issue["code"] for issue in report.issues}


def test_reference_rendering_is_the_control_case(tmp_path: Path, cv: dict, identity: dict) -> None:
    report = _check(_pdf(tmp_path, _html(cv, identity)), cv, identity)
    assert report.readability == 100 and report.ready and report.issues == []
    assert "Analyste SOC (alternance)" in report.extracted_text


def test_missing_date_is_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    printed = copy.deepcopy(cv)
    printed["sections"][0]["items"][1]["fields"]["start"] = None
    printed["sections"][0]["items"][1]["fields"]["end"] = None
    report = _check(_pdf(tmp_path, _html(printed, identity)), cv, identity)
    missing = {issue["field"] for issue in report.issues if issue["code"] == "field_missing"}
    assert missing == {"experience.exp-2.start", "experience.exp-2.end"}
    assert report.readability < 100
    # Deux champs sur des dizaines laissent le score au-dessus de 95 %, mais une date essentielle manque : bloqué.
    assert report.readability >= 95 and report.ready is False
    assert any(issue["code"] == "essential_missing" for issue in report.issues)
    assert "Technicien support informatique (stage)" in next(
        issue["message"] for issue in report.issues if issue["field"] == "experience.exp-2.start")


def test_date_of_another_item_does_not_count(tmp_path: Path, cv: dict, identity: dict) -> None:
    printed = copy.deepcopy(cv)
    printed["sections"][2]["items"][1]["fields"]["start"] = "2025-09"  # la date de l'autre formation
    report = _check(_pdf(tmp_path, _html(printed, identity)), cv, identity)
    assert "education.edu-2.start" in {issue["field"] for issue in report.issues}
    assert report.ready is False


def test_nonstandard_section_title_is_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    html = _html(cv, identity).replace(">Expérience professionnelle<", ">Mon parcours<")
    report = _check(_pdf(tmp_path, html), cv, identity)
    assert "section_missing" in _codes(report)
    assert report.readability < 95 and report.ready is False


def test_reversed_items_are_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    printed = copy.deepcopy(cv)
    printed["sections"][0]["items"].reverse()
    report = _check(_pdf(tmp_path, _html(printed, identity)), cv, identity)
    assert "item_order" in _codes(report)
    assert report.readability < 100


def test_reversed_sections_are_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    printed = copy.deepcopy(cv)
    printed["sections"].reverse()
    report = _check(_pdf(tmp_path, _html(printed, identity)), cv, identity)
    assert "section_order" in _codes(report)
    assert report.readability < 95


def test_two_column_layout_is_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    html = _html(cv, identity)
    head, rest = html.split("<section>", 1)
    html = head + "<div style='display:grid;grid-template-columns:1fr 1fr;gap:16pt'><div><section>" + rest
    html = html.replace("<section class='keep'><h2>Formation", "</div><div><section><h2>Formation").replace(
        "</body>", "</div></div></body>")
    report = _check(_pdf(tmp_path, html), cv, identity)
    assert "layout_columns" in _codes(report)
    assert report.readability < 95 and report.ready is False


def test_text_in_image_is_detected(tmp_path: Path, cv: dict, identity: dict) -> None:
    html = _html(cv, identity)
    start, end = html.index("<section>"), html.index("<section class='keep'><h2>Formation")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(java_script_enabled=False, viewport={"width": 800, "height": 900})
            page.set_content(html[:end] + "</body></html>")
            image = page.screenshot(full_page=True)
        finally:
            browser.close()
    picture = f"<img alt='' style='width:100%' src='data:image/png;base64,{base64.b64encode(image).decode()}'>"
    report = _check(_pdf(tmp_path, html[:start] + picture + html[end:]), cv, identity)
    assert "image_content" in _codes(report)
    assert report.readability < 95
    assert "section_missing" in _codes(report)


def test_fully_scanned_document_scores_zero(tmp_path: Path, cv: dict, identity: dict) -> None:
    report = _check(_pdf(tmp_path, "<!doctype html><html><body><div style='height:20px;background:#000'></div>"
                         "</body></html>"), cv, identity)
    assert report.readability == 0 and "no_text" in _codes(report)


def test_docx_reading_is_also_required(tmp_path: Path, cv: dict, identity: dict) -> None:
    result = render_cv(cv, identity, template={"kind": "jobradar", "id": "sobre-ats"}, template_map=None,
                       out_dir=tmp_path, base_name="cv")
    other = copy.deepcopy(cv)
    other["sections"][0]["items"] = other["sections"][0]["items"][:1]
    stale = render_cv(other, identity, template={"kind": "jobradar", "id": "sobre-ats"}, template_map=None,
                      out_dir=tmp_path / "autre", base_name="cv")
    report = _check(result.pdf_path, cv, identity, docx=stale.docx_path)
    assert report.readability < 100
    assert any("DOCX" in issue["message"] for issue in report.issues)


def test_keyword_coverage_counts_owned_offer_keywords_only(tmp_path: Path, cv: dict, identity: dict) -> None:
    pdf = _pdf(tmp_path, _html(cv, identity))
    report = _check(pdf, cv, identity, offer=["Splunk", "python", "Kubernetes", "C++", "Wazuh", "EBIOS RM"],
                    owned=["Splunk", "Python", "Wazuh", "C++", "Docker"])
    assert report.keyword_coverage == {
        "owned_present": 3,
        "owned_total": 4,
        "missing_owned": ["C++"],  # « C » de « KQL » ou d'un sigle ne compte pas pour C++
        "not_owned": ["Kubernetes", "EBIOS RM"],
    }
    assert "owned_keywords_missing" in _codes(report)
    assert report.readability == 100


@pytest.mark.parametrize("target", ["absent", "garbage", "empty"])
def test_ats_check_never_raises(tmp_path: Path, cv: dict, identity: dict, target: str) -> None:
    path = tmp_path / "document.pdf"
    if target == "garbage":
        path.write_bytes(b"%PDF-1.7 ceci n'est pas un PDF")
    elif target == "empty":
        path.write_bytes(b"")
    report = ats_check(path, composition=cv, identity=identity, offer_keywords=["Splunk", "Kubernetes"],
                       owned_keywords=["Splunk"], docx_path=tmp_path / "absent.docx")
    assert report.readability == 0 and report.ready is False
    assert report.issues[0]["code"] == "unreadable_document"
    assert report.keyword_coverage["not_owned"] == ["Kubernetes"]
    assert report.to_json()["ready"] is False


def test_ats_check_tolerates_malformed_inputs(tmp_path: Path, cv: dict, identity: dict) -> None:
    pdf = _pdf(tmp_path, _html(cv, identity))
    for composition, owner in ((None, identity), ({"kind": "autre"}, identity), (cv, None)):
        report = ats_check(pdf, composition=composition, identity=owner, offer_keywords=None,  # type: ignore[arg-type]
                           owned_keywords=[None, 3])  # type: ignore[list-item]
        assert isinstance(report, AtsReport)
    broken = copy.deepcopy(cv)
    broken["sections"] = [{"type": "experience", "items": "pas une liste"}, None, {"type": "skills", "groups": [1]}]
    assert ats_check(pdf, composition=broken, identity=identity, offer_keywords=[], owned_keywords=[]).ready is True


def test_cv_sans_nom_n_est_jamais_pret(tmp_path: Path, cv: dict, identity: dict) -> None:
    """Profil sans nom au moment du rendu : le plan ATS ne l'attend pas, mais le CV reste bloqué."""
    anonymous = copy.deepcopy(identity) | {"full_name": ""}
    report = _check(_pdf(tmp_path, _html(cv, anonymous)), cv, anonymous)
    assert report.ready is False
    assert "name_missing" in _codes(report)
    assert any("Mon profil" in issue["message"] for issue in report.issues if issue["code"] == "name_missing")


def test_cv_produit_sans_nom_est_refuse_une_fois_le_nom_connu(tmp_path: Path, cv: dict, identity: dict) -> None:
    """Cas réel : CV généré avant que le nom soit saisi, puis revérifié avec l'identité actuelle."""
    anonymous = copy.deepcopy(identity) | {"full_name": ""}
    pdf = _pdf(tmp_path, _html(cv, anonymous))
    report = _check(pdf, cv, identity)
    assert report.ready is False
    assert any(issue["field"] == "identity.full_name" for issue in report.issues)
