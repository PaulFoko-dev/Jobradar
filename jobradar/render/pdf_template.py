"""Analyse prudente du style d'un PDF Canva/Figma pour produire une approximation éditable et ATS."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ContentStream

from .models import PdfTemplateMap, RenderError


def _hex(components: list[float]) -> str:
    values = [max(0, min(255, round(float(value) * 255))) for value in components[:3]]
    return "#" + "".join(f"{value:02X}" for value in values)


def _brightness(color: str) -> float:
    rgb = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def _clean_font(value: object) -> str:
    name = str(value or "").lstrip("/")
    if "+" in name:  # préfixe de sous-ensemble PDF, ex. ABCDEF+Montserrat-Bold
        name = name.split("+", 1)[1]
    for suffix in ("-BoldItalic", "-Bold", "-Italic", "-Regular", "MT"):
        name = name.replace(suffix, "")
    return name.replace("-", " ").strip() or "Arial"


def analyze_pdf_template(path: Path) -> PdfTemplateMap:
    """Extrait famille de police et palette ; aucune donnée du PDF n'est envoyée à un service externe."""
    try:
        reader = PdfReader(path)
        if not reader.pages:
            raise RenderError("Le modèle PDF ne contient aucune page.")
        page = reader.pages[0]
        fonts: Counter[str] = Counter()
        resources = page.get("/Resources") or {}
        for item in (resources.get("/Font") or {}).values():
            font = item.get_object()
            fonts[_clean_font(font.get("/BaseFont"))] += 1
        colors: Counter[str] = Counter()
        for operands, operator in ContentStream(page.get_contents(), reader).operations:
            if operator in {b"rg", b"RG"} and len(operands) >= 3:
                try:
                    colors[_hex([float(value) for value in operands])] += 1
                except (TypeError, ValueError):
                    continue
        dark = [color for color, _ in colors.most_common() if _brightness(color) < 0.72]
        text = next((color for color in dark if _brightness(color) < 0.3), "#1A1A1A")
        accent = next((color for color in dark if color != text), text)
        issues = ["Reproduction approchée : la géométrie Canva/Figma n'est pas modifiable dans le PDF."]
        if not fonts:
            issues.append("Police non identifiable : remplacement par Arial.")
        if not colors:
            issues.append("Palette non identifiable : couleurs sobres de repli.")
        return PdfTemplateMap(path, fonts.most_common(1)[0][0] if fonts else "Arial", text,
                              "#56606A", accent, accent if accent != text else "#B8BEC4", issues)
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError("Le style du modèle PDF n'a pas pu être analysé.") from exc
