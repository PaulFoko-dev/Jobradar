"""Production des CV et lettres en PDF et DOCX (modèles JobRadar ou DOCX d'origine)."""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

from docx.document import Document as DocumentObject

from . import chromium
from .common import (
    atomic_write_files,
    compact_text,
    document_title,
    ensure_output_dir,
    identity_parts,
    language_of,
    safe_base_name,
)
from .docx_html import docx_to_html
from .jobradar_docx import cv_docx, letter_docx
from .jobradar_html import DENSITIES, THEMES, Theme, cv_html, cv_html_theme, letter_html, letter_html_theme
from .latex import compile_pdf as compile_latex
from .latex import cv_source as latex_cv_source
from .latex import letter_source as latex_letter_source
from .models import PdfTemplateMap, RenderError, RenderResult, TemplateMap
from .template import fill_cv, fill_letter, finish, open_template

_NO_LIBREOFFICE = (
    "LibreOffice n'est pas installé (ou sa conversion a échoué) : le PDF est un rendu approché du DOCX "
    "d'origine (polices, tailles, couleurs et ordre conservés ; positions des zones flottantes, espacements "
    "fins et coupures de page peuvent différer). Le DOCX reste la référence : pour un PDF identique, "
    "installe LibreOffice ou exporte le DOCX en PDF depuis Word."
)


def list_templates() -> list[dict]:
    templates = [{"id": theme.id, "name": theme.name, "description": theme.description, "ats": True}
                 for theme in THEMES.values()]
    templates.append({"id": "latex-ats", "name": "LaTeX ATS",
                      "description": "Typographie LaTeX nette via Tectonic ; repli Sobre ATS si absent.", "ats": True,
                      "kind": "latex"})
    return templates


def _template_kind(template: dict, template_map: TemplateMap | PdfTemplateMap | None) -> tuple[str, str]:
    if not isinstance(template, dict):
        raise RenderError("Le modèle de document est invalide.")
    kind, template_id = template.get("kind"), template.get("id")
    if kind == "jobradar" and isinstance(template_id, str) and template_id in THEMES:
        return "jobradar", template_id
    if kind == "original":
        if not isinstance(template_map, TemplateMap):
            raise RenderError("La cartographie du modèle DOCX d'origine est requise.")
        return "original", ""
    if kind == "personal-pdf" and isinstance(template_map, PdfTemplateMap):
        return "personal-pdf", ""
    if kind == "latex" and template_id == "latex-ats":
        return "latex", template_id
    raise RenderError("Le modèle demandé n'existe pas.")


def _docx_bytes(document: DocumentObject) -> bytes:
    stream = BytesIO()
    try:
        document.save(stream)
    except Exception as exc:
        raise RenderError("Le DOCX rempli n'a pas pu être enregistré.") from exc
    return stream.getvalue()


def _soffice_pdf(docx: bytes) -> bytes | None:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if executable is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="jobradar-rendu-") as name:
            directory = Path(name)
            source = directory / "document.docx"
            source.write_bytes(docx)
            # Profil LibreOffice jetable : une instance ouverte par l'utilisateur ne bloque pas la conversion.
            profile = (directory / "profil").as_uri()
            result = subprocess.run(
                [executable, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", str(directory), str(source)],
                capture_output=True,
                check=False,
                timeout=90,
            )
            pdf = directory / "document.pdf"
            if result.returncode == 0 and pdf.is_file():
                content = pdf.read_bytes()
                if content.startswith(b"%PDF"):
                    return content
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _original_pdf(document: DocumentObject, docx: bytes, *, title: str, author: str, language: str,
                  warnings: list[str]) -> bytes:
    pdf = _soffice_pdf(docx)
    if pdf is None:
        warnings.append(_NO_LIBREOFFICE)
        html = docx_to_html(document, title=title, author=author, language=language)
        pdf, _, _ = chromium.print_variants([html])
    return chromium.remove_trailing_blank_page(pdf)


def _write(out_dir: Path, base_name: str, pdf: bytes, docx: bytes, warnings: list[str]) -> RenderResult:
    name = safe_base_name(base_name)
    directory = ensure_output_dir(out_dir)
    pdf_path, docx_path = directory / f"{name}.pdf", directory / f"{name}.docx"
    atomic_write_files({pdf_path: pdf, docx_path: docx})
    return RenderResult(pdf_path, docx_path, warnings)


def _page_warning(pages: int, language: str, *, kind: str) -> list[str]:
    if kind == "cv" and pages > 2:
        return [f"Le CV occupe {pages} pages : deux pages au maximum sont recommandées ; allège la composition."]
    if kind == "cv" and pages == 2:
        return ["Le CV occupe deux pages : pour un stage ou une alternance, vise une seule page."]
    if kind == "lettre" and pages > 1:
        return ["La lettre dépasse une page : raccourcis-la."]
    return []


def _guarded(function: Callable[..., RenderResult]) -> Callable[..., RenderResult]:
    """Toute erreur inattendue du rendu devient une RenderError au message français (contrat §3)."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> RenderResult:
        try:
            return function(*args, **kwargs)
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError("Le document n'a pas pu être produit.") from exc

    return wrapper


@_guarded
def render_cv(
    composition: dict,
    identity: dict,
    *,
    template: dict,
    template_map: TemplateMap | PdfTemplateMap | None,
    out_dir: Path,
    base_name: str,
) -> RenderResult:
    if not isinstance(composition, dict) or composition.get("kind") != "cv" or not isinstance(identity, dict):
        raise RenderError("La composition du CV est invalide.")
    safe_base_name(base_name)
    kind, template_id = _template_kind(template, template_map)
    language = language_of(composition)
    name, _ = identity_parts(identity, language)
    title = document_title(composition, name)
    warnings: list[str] = []
    if kind == "jobradar":
        variants = [cv_html(composition, identity, template_id, title=title, author=name, scale=scale)
                    for scale in DENSITIES]
        pdf, pages, chosen = chromium.print_variants(variants, target_pages=1)
        docx = cv_docx(composition, identity, template_id, title=title, author=name, scale=DENSITIES[chosen])
        warnings += _page_warning(pages, language, kind="cv")
    elif kind == "latex":
        source = latex_cv_source(composition, identity, title=title, author=name)
        pdf = compile_latex(source)
        if pdf is None:
            warnings.append("Tectonic n'est pas installé : repli sur le rendu « Sobre ATS ».")
            variants = [cv_html(composition, identity, "sobre-ats", title=title, author=name, scale=scale)
                        for scale in DENSITIES]
            pdf, pages, chosen = chromium.print_variants(variants, target_pages=1)
        else:
            pages, chosen = chromium.page_count(pdf), 0
        docx = cv_docx(composition, identity, "sobre-ats", title=title, author=name, scale=DENSITIES[chosen])
        warnings += _page_warning(pages, language, kind="cv")
    elif kind == "personal-pdf":
        assert isinstance(template_map, PdfTemplateMap)
        theme = Theme("personal-pdf", "Modèle PDF personnel", "Approximation ATS du PDF importé",
                      f"'{template_map.font}', Arial, sans-serif", f"'{template_map.font}', Arial, sans-serif",
                      template_map.text, template_map.muted, template_map.accent, template_map.rule, 22,
                      template_map.font)
        variants = [cv_html_theme(composition, identity, theme, title=title, author=name, scale=scale)
                    for scale in DENSITIES]
        pdf, pages, chosen = chromium.print_variants(variants, target_pages=1)
        docx = cv_docx(composition, identity, theme, title=title, author=name, scale=DENSITIES[chosen])
        warnings += template_map.issues + _page_warning(pages, language, kind="cv")
    else:
        assert template_map is not None
        if not template_map.ats_compatible:
            warnings.append("Le modèle d'origine présente des risques pour les ATS (voir la cartographie) : "
                            "vérifie le contrôle ATS ou choisis un modèle JobRadar.")
        document = open_template(template_map)
        warnings += fill_cv(document, template_map, composition, identity)
        finish(document, title, name)
        docx = _docx_bytes(document)
        pdf = _original_pdf(document, docx, title=title, author=name, language=language, warnings=warnings)
        warnings += _page_warning(chromium.page_count(pdf), language, kind="cv")
    pdf = chromium.with_metadata(pdf, title, name)
    return _write(out_dir, base_name, pdf, docx, warnings)


@_guarded
def render_letter(
    letter: dict,
    identity: dict,
    *,
    template: dict,
    template_map: TemplateMap | PdfTemplateMap | None,
    out_dir: Path,
    base_name: str,
) -> RenderResult:
    if not isinstance(letter, dict) or letter.get("kind") != "lettre" or not isinstance(identity, dict):
        raise RenderError("La composition de la lettre est invalide.")
    safe_base_name(base_name)
    if not isinstance(letter.get("paragraphs", []), list):
        raise RenderError("Les paragraphes de la lettre sont invalides.")
    kind, template_id = _template_kind(template, template_map)
    language = language_of(letter)
    name, _ = identity_parts(identity, language)
    title = document_title(letter, name)
    warnings: list[str] = []
    if kind == "jobradar":
        html = letter_html(letter, identity, template_id, title=title, author=name)
        pdf, pages, _ = chromium.print_variants([html])
        docx = letter_docx(letter, identity, template_id, title=title, author=name)
    elif kind == "latex":
        source = latex_letter_source(letter, identity, title=title, author=name)
        pdf = compile_latex(source)
        if pdf is None:
            warnings.append("Tectonic n'est pas installé : repli sur le rendu « Sobre ATS ».")
            html = letter_html(letter, identity, "sobre-ats", title=title, author=name)
            pdf, pages, _ = chromium.print_variants([html])
        else:
            pages = chromium.page_count(pdf)
        docx = letter_docx(letter, identity, "sobre-ats", title=title, author=name)
    elif kind == "personal-pdf":
        assert isinstance(template_map, PdfTemplateMap)
        theme = Theme("personal-pdf", "Modèle PDF personnel", "Approximation ATS du PDF importé",
                      f"'{template_map.font}', Arial, sans-serif", f"'{template_map.font}', Arial, sans-serif",
                      template_map.text, template_map.muted, template_map.accent, template_map.rule, 22,
                      template_map.font)
        html = letter_html_theme(letter, identity, theme, title=title, author=name)
        pdf, pages, _ = chromium.print_variants([html])
        docx = letter_docx(letter, identity, theme, title=title, author=name)
        warnings += template_map.issues
    else:
        assert template_map is not None
        document = open_template(template_map)
        warnings += fill_letter(document, template_map, letter, identity)
        finish(document, title, name)
        docx = _docx_bytes(document)
        pdf = _original_pdf(document, docx, title=title, author=name, language=language, warnings=warnings)
        pages = chromium.page_count(pdf)
    warnings += _page_warning(pages, language, kind="lettre")
    if not compact_text(name):
        warnings.append("L'identité ne contient pas de nom : le document n'est pas signé.")
    pdf = chromium.with_metadata(pdf, title, name)
    return _write(out_dir, base_name, pdf, docx, warnings)
