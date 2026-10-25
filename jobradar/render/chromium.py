"""Impression PDF hors ligne par Chromium (Playwright), sur le modèle de `jobradar.capture`."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Any

from playwright.sync_api import Browser, Playwright, sync_playwright
from pypdf import PdfReader, PdfWriter

from .models import RenderError

# Défense en profondeur : même si une balise passait l'échappement, rien ne serait chargé ni exécuté.
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"

_ENGINE_CHANNELS: tuple[str | None, ...] = (None, "chrome", "msedge")
_TIMEOUT_MS = 20_000


def _launch(playwright: Playwright) -> Browser:
    for channel in _ENGINE_CHANNELS:
        options: dict[str, Any] = {"headless": True, "timeout": _TIMEOUT_MS}
        if channel is not None:
            options["channel"] = channel
        try:
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    raise RenderError("Aucun navigateur Chromium, Chrome ou Edge utilisable n'est disponible pour produire le PDF.")


def page_count(pdf: bytes) -> int:
    try:
        return len(PdfReader(BytesIO(pdf)).pages)
    except Exception as exc:
        raise RenderError("Le navigateur a produit un PDF illisible.") from exc


def print_variants(variants: Sequence[str], *, target_pages: int | None = None) -> tuple[bytes, int, int]:
    """Imprime les variantes HTML dans l'ordre et s'arrête à la première qui tient en `target_pages` pages.

    Renvoie (PDF, nombre de pages, indice de la variante retenue). Sans variante conforme, la première est
    retenue si elle tient en deux pages, sinon la plus courte.
    """
    if not variants:
        raise RenderError("Aucun contenu à imprimer.")
    results: list[tuple[bytes, int]] = []
    try:
        with sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                context = browser.new_context(
                    java_script_enabled=False,
                    offline=True,
                    service_workers="block",
                    accept_downloads=False,
                    permissions=[],
                )
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(_TIMEOUT_MS)
                for html in variants:
                    page.set_content(html, wait_until="load")
                    pdf = page.pdf(prefer_css_page_size=True, print_background=True, tagged=True, outline=False)
                    if not pdf.startswith(b"%PDF"):
                        raise RenderError("Le navigateur a produit un PDF invalide.")
                    results.append((pdf, page_count(pdf)))
                    if target_pages is None or results[-1][1] <= target_pages:
                        break
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError("Le PDF n'a pas pu être produit par le navigateur.") from exc

    for index, (pdf, pages) in enumerate(results):
        if target_pages is None or pages <= target_pages:
            return pdf, pages, index
    if results[0][1] <= 2:
        return results[0][0], results[0][1], 0
    index = min(range(len(results)), key=lambda position: results[position][1])
    return results[index][0], results[index][1], index


def with_metadata(pdf: bytes, title: str, author: str) -> bytes:
    """Inscrit titre et auteur ; la structure (balisage, polices intégrées) est conservée telle quelle."""
    try:
        writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf)))
        writer.add_metadata({"/Title": title, "/Author": author, "/Creator": "JobRadar", "/Producer": "JobRadar"})
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as exc:
        raise RenderError("Les métadonnées du PDF n'ont pas pu être inscrites.") from exc
