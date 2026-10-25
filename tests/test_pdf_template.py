"""Étape 6 : extraction locale et prudente du style d'un modèle PDF graphique."""
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from jobradar.render import analyze_pdf_template


def graphical_pdf(path: Path) -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    stream = DecodedStreamObject()
    stream.set_data(b"0.08 0.12 0.18 rg 10 10 100 20 re f\n0.10 0.45 0.55 RG 10 40 m 200 40 l S")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_pdf_style_is_extracted_without_external_service(tmp_path: Path) -> None:
    mapping = analyze_pdf_template(graphical_pdf(tmp_path / "modele.pdf"))
    assert mapping.source_path.name == "modele.pdf"
    assert mapping.text == "#141F2E"
    assert mapping.accent == "#1A738C"
    assert mapping.font == "Arial"
    assert any("Police non identifiable" in issue for issue in mapping.issues)
    assert mapping.to_json()["accent"] == mapping.accent
