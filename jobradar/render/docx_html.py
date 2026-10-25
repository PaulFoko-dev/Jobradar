"""Conversion d'un DOCX en HTML fidèle (polices, tailles, couleurs, alignements, ordre), sans LibreOffice.

Sert de repli pour imprimer en PDF un DOCX d'origine rempli. Les propriétés sont résolues comme le fait
Word : valeurs par défaut du document, puis chaîne des styles de paragraphe, style de caractère, mise en
forme directe. Le positionnement absolu (zones de texte flottantes) n'est qu'approché.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from lxml import etree

from .chromium import CONTENT_SECURITY_POLICY
from .common import escaped

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_TXBX = qn("w:txbxContent")
_RUN_CONTAINERS = {qn(tag) for tag in ("w:hyperlink", "w:ins", "w:smartTag", "w:fldSimple", "w:customXml", "w:sdt",
                                       "w:sdtContent", "w:moveTo")}
_SKIPPED = {qn(tag) for tag in ("w:del", "w:moveFrom", "w:pPr", "w:rPr", "w:sdtPr", "w:sdtEndPr", "w:proofErr",
                                 "w:bookmarkStart", "w:bookmarkEnd")}
_BULLET_GLYPHS = {"": "•", "": "▪", "": "➢", "": "❖", "": "✓", "o": "◦", "§": "▪"}
_SERIF = ("times", "georgia", "cambria", "garamond", "book antiqua", "palatino", "caladea", "serif", "baskerville")
_MONO = ("courier", "consolas", "mono", "menlo", "lucida console")
_FONT_FALLBACKS = {
    "calibri": "Carlito", "cambria": "Caladea", "arial": "'Liberation Sans'", "helvetica": "'Liberation Sans'",
    "times new roman": "'Liberation Serif'", "courier new": "'Liberation Mono'", "aptos": "Carlito",
    "verdana": "'DejaVu Sans'", "tahoma": "'DejaVu Sans'", "segoe ui": "'Noto Sans'", "georgia": "'DejaVu Serif'",
}


def _twips_pt(value: str | None) -> float | None:
    try:
        return int(value) / 20 if value is not None else None
    except ValueError:
        return None


def _val(element: etree._Element | None, attribute: str = "w:val") -> str | None:
    return element.get(qn(attribute)) if element is not None else None


def _on(element: etree._Element | None) -> bool | None:
    """Propriété booléenne Word : présente = vraie, sauf val à 0/false/off."""
    if element is None:
        return None
    return _val(element) not in {"0", "false", "off", "none"}


@dataclass
class _RunStyle:
    font: str | None = None
    size: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    caps: bool | None = None
    small_caps: bool | None = None
    color: str | None = None
    hidden: bool | None = None

    def merge(self, other: _RunStyle) -> _RunStyle:
        merged = _RunStyle(**self.__dict__)
        for key, value in other.__dict__.items():
            if value is not None:
                setattr(merged, key, value)
        return merged


@dataclass
class _ParaStyle:
    align: str | None = None
    before: float | None = None
    after: float | None = None
    line: tuple[str, float] | None = None
    left: float | None = None
    right: float | None = None
    first: float | None = None
    num: tuple[str, int] | None = None
    right_tab: bool | None = None
    borders: dict[str, str] = field(default_factory=dict)
    shading: str | None = None

    def merge(self, other: _ParaStyle) -> _ParaStyle:
        merged = _ParaStyle(**{key: value for key, value in self.__dict__.items() if key != "borders"})
        merged.borders = dict(self.borders) | other.borders
        for key, value in other.__dict__.items():
            if key != "borders" and value is not None:
                setattr(merged, key, value)
        return merged


class _Resolver:
    def __init__(self, document: DocumentObject) -> None:
        self.document = document
        styles = document.styles.element
        self.styles = {style.get(qn("w:styleId")): style for style in styles.iter(qn("w:style"))}
        self.default_paragraph_style = next(
            (sid for sid, style in self.styles.items()
             if style.get(qn("w:type")) == "paragraph" and style.get(qn("w:default")) in {"1", "true"}),
            None,
        )
        defaults = styles.find(qn("w:docDefaults"))
        run_default = defaults.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}") if defaults is not None else None
        para_default = defaults.find(f"{qn('w:pPrDefault')}/{qn('w:pPr')}") if defaults is not None else None
        self.theme_fonts = self._theme_fonts()
        self.run_default = _RunStyle(font="Times New Roman", size=10.0).merge(self.run_props(run_default))
        self.para_default = self.para_props(para_default)
        self.numbering = self._numbering()
        self.counters: dict[tuple[str, int], int] = {}

    def _theme_fonts(self) -> dict[str, str]:
        fonts: dict[str, str] = {}
        for part in self.document.part.package.iter_parts():
            if not str(part.partname).startswith("/word/theme/"):
                continue
            try:
                root = etree.fromstring(part.blob)
            except etree.XMLSyntaxError:
                continue
            namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
            for kind in ("major", "minor"):
                latin = root.find(f".//{namespace}{kind}Font/{namespace}latin")
                if latin is not None and latin.get("typeface"):
                    fonts[kind] = latin.get("typeface")
            break
        return fonts

    def _numbering(self) -> dict[str, etree._Element]:
        try:
            numbering = self.document.part.numbering_part.element
        except Exception:
            return {}
        abstract = {node.get(qn("w:abstractNumId")): node for node in numbering.iter(qn("w:abstractNum"))}
        result: dict[str, etree._Element] = {}
        for num in numbering.iter(qn("w:num")):
            abstract_id = _val(num.find(qn("w:abstractNumId")))
            if abstract_id in abstract:
                result[num.get(qn("w:numId"))] = abstract[abstract_id]
        return result

    def run_props(self, rpr: etree._Element | None) -> _RunStyle:
        style = _RunStyle()
        if rpr is None:
            return style
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is not None:
            theme = fonts.get(qn("w:asciiTheme")) or fonts.get(qn("w:hAnsiTheme"))
            if theme:
                style.font = self.theme_fonts.get("major" if theme.startswith("major") else "minor", "Calibri")
            else:
                style.font = fonts.get(qn("w:ascii")) or fonts.get(qn("w:hAnsi")) or style.font
        size = _val(rpr.find(qn("w:sz")))
        if size and size.isdigit():
            style.size = int(size) / 2
        style.bold = _on(rpr.find(qn("w:b")))
        style.italic = _on(rpr.find(qn("w:i")))
        underline = rpr.find(qn("w:u"))
        if underline is not None:
            style.underline = _val(underline) != "none"
        style.strike = _on(rpr.find(qn("w:strike")))
        style.caps = _on(rpr.find(qn("w:caps")))
        style.small_caps = _on(rpr.find(qn("w:smallCaps")))
        style.hidden = _on(rpr.find(qn("w:vanish")))
        color = _val(rpr.find(qn("w:color")))
        if color and re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            style.color = f"#{color}"
        elif color == "auto":
            style.color = "#000000"
        return style

    def para_props(self, ppr: etree._Element | None) -> _ParaStyle:
        style = _ParaStyle()
        if ppr is None:
            return style
        align = _val(ppr.find(qn("w:jc")))
        if align:
            style.align = {"both": "justify", "distribute": "justify", "center": "center", "right": "right",
                           "end": "right", "left": "left", "start": "left"}.get(align, "left")
        spacing = ppr.find(qn("w:spacing"))
        if spacing is not None:
            style.before = _twips_pt(spacing.get(qn("w:before"))) if spacing.get(qn("w:before")) else None
            style.after = _twips_pt(spacing.get(qn("w:after"))) if spacing.get(qn("w:after")) else None
            line = spacing.get(qn("w:line"))
            if line and line.lstrip("-").isdigit():
                rule = spacing.get(qn("w:lineRule")) or "auto"
                style.line = ("auto", int(line) / 240) if rule == "auto" else ("exact", int(line) / 20)
        indent = ppr.find(qn("w:ind"))
        if indent is not None:
            style.left = _twips_pt(indent.get(qn("w:left")) or indent.get(qn("w:start")))
            style.right = _twips_pt(indent.get(qn("w:right")) or indent.get(qn("w:end")))
            if indent.get(qn("w:hanging")):
                hanging = _twips_pt(indent.get(qn("w:hanging")))
                style.first = -hanging if hanging is not None else None
            elif indent.get(qn("w:firstLine")):
                style.first = _twips_pt(indent.get(qn("w:firstLine")))
        numbering = ppr.find(qn("w:numPr"))
        if numbering is not None:
            num_id = _val(numbering.find(qn("w:numId")))
            level = _val(numbering.find(qn("w:ilvl"))) or "0"
            if num_id and num_id != "0":
                style.num = (num_id, int(level) if level.isdigit() else 0)
        tabs = ppr.find(qn("w:tabs"))
        if tabs is not None:
            style.right_tab = any(_val(tab) in {"right", "end"} for tab in tabs.iter(qn("w:tab")))
        borders = ppr.find(qn("w:pBdr"))
        if borders is not None:
            for side in ("top", "bottom", "left", "right"):
                border = borders.find(qn(f"w:{side}"))
                if border is not None and _val(border) not in {"nil", "none"}:
                    width = max(0.5, int(border.get(qn("w:sz"), "4")) / 8)
                    color = border.get(qn("w:color"))
                    color = f"#{color}" if color and re.fullmatch(r"[0-9A-Fa-f]{6}", color) else "#000"
                    style.borders[side] = f"{width:.2f}pt solid {color}"
        shading = ppr.find(qn("w:shd"))
        fill = shading.get(qn("w:fill")) if shading is not None else None
        if fill and re.fullmatch(r"[0-9A-Fa-f]{6}", fill):
            style.shading = f"#{fill}"
        return style

    def _style_chain(self, style_id: str | None) -> list[etree._Element]:
        chain: list[etree._Element] = []
        seen: set[str] = set()
        while style_id and style_id in self.styles and style_id not in seen:
            seen.add(style_id)
            style = self.styles[style_id]
            chain.append(style)
            style_id = _val(style.find(qn("w:basedOn")))
        return list(reversed(chain))

    def paragraph(self, p: etree._Element) -> tuple[_ParaStyle, _RunStyle]:
        ppr = p.find(qn("w:pPr"))
        style_id = _val(ppr.find(qn("w:pStyle"))) if ppr is not None else None
        para = self.para_default
        run = self.run_default
        for style in self._style_chain(style_id or self.default_paragraph_style):
            para = para.merge(self.para_props(style.find(qn("w:pPr"))))
            run = run.merge(self.run_props(style.find(qn("w:rPr"))))
        if ppr is not None:
            para = para.merge(self.para_props(ppr))
        return para, run

    def run(self, r: etree._Element, base: _RunStyle) -> _RunStyle:
        rpr = r.find(qn("w:rPr"))
        style = base
        style_id = _val(rpr.find(qn("w:rStyle"))) if rpr is not None else None
        for character_style in self._style_chain(style_id):
            style = style.merge(self.run_props(character_style.find(qn("w:rPr"))))
        return style.merge(self.run_props(rpr))

    def marker(self, num: tuple[str, int]) -> tuple[str, _ParaStyle, _RunStyle]:
        abstract = self.numbering.get(num[0])
        if abstract is None:
            return "•", _ParaStyle(), _RunStyle()
        level = next((lvl for lvl in abstract.iter(qn("w:lvl")) if lvl.get(qn("w:ilvl")) == str(num[1])), None)
        if level is None:
            return "•", _ParaStyle(), _RunStyle()
        fmt = _val(level.find(qn("w:numFmt"))) or "bullet"
        text = _val(level.find(qn("w:lvlText"))) or ""
        para = self.para_props(level.find(qn("w:pPr")))
        run = self.run_props(level.find(qn("w:rPr")))
        if fmt == "bullet":
            glyph = _BULLET_GLYPHS.get(text, text)
            if not glyph or any(0xE000 <= ord(char) <= 0xF8FF for char in glyph):
                glyph = "•"
            run.font = None
            return glyph, para, run
        if fmt == "none":
            return "", para, run
        key = (num[0], num[1])
        self.counters[key] = self.counters.get(key, 0) + 1
        return re.sub(r"%\d", str(self.counters[key]), text) or f"{self.counters[key]}.", para, run


def font_stack(font: str | None) -> str:
    name = (font or "Calibri").strip()
    lower = name.casefold()
    generic = "serif" if any(marker in lower for marker in _SERIF) else (
        "monospace" if any(marker in lower for marker in _MONO) else "sans-serif")
    parts = [f"'{name.replace(chr(39), '')}'"]
    if lower in _FONT_FALLBACKS:
        parts.append(_FONT_FALLBACKS[lower])
    parts.append(generic)
    return ", ".join(parts)


def _run_css(style: _RunStyle) -> str:
    rules = [f"font-family:{font_stack(style.font)}"]
    if style.size:
        rules.append(f"font-size:{style.size:.1f}pt")
    rules.append(f"font-weight:{700 if style.bold else 400}")
    rules.append(f"font-style:{'italic' if style.italic else 'normal'}")
    decorations = [name for flag, name in ((style.underline, "underline"), (style.strike, "line-through")) if flag]
    if decorations:
        rules.append(f"text-decoration:{' '.join(decorations)}")
    if style.caps:
        rules.append("text-transform:uppercase")
    elif style.small_caps:
        rules.append("font-variant:small-caps")
    if style.color:
        rules.append(f"color:{style.color}")
    return ";".join(rules)


class _Converter:
    def __init__(self, document: DocumentObject) -> None:
        self.document = document
        self.resolver = _Resolver(document)
        section = document.sections[0]
        self.text_width = ((section.page_width or 0) - (section.left_margin or 0) - (section.right_margin or 0)) / 12700

    def _image(self, blip: etree._Element, extent: etree._Element | None, part: object) -> str:
        relation = blip.get(_R_EMBED)
        try:
            image_part = part.related_parts[relation]  # type: ignore[attr-defined]
            content_type = image_part.content_type
            if not content_type.startswith("image/"):
                return ""
            data = base64.b64encode(image_part.blob).decode("ascii")
        except Exception:
            return ""
        size = ""
        if extent is not None:
            try:
                width = int(extent.get("cx", "0")) / 12700
                height = int(extent.get("cy", "0")) / 12700
                size = f"width:{width:.1f}pt;height:{height:.1f}pt;"
            except ValueError:
                size = ""
        return f"<img alt='' style='{size}vertical-align:middle' src='data:{escaped(content_type)};base64,{data}'>"

    def _drawing(self, element: etree._Element, part: object) -> tuple[str, str]:
        """Renvoie (HTML en ligne, HTML de bloc) d'un dessin : image ou zone de texte."""
        inline: list[str] = []
        blocks: list[str] = []
        anchor = element.find(f".//{_WP}anchor")
        floated = ""
        if anchor is not None:
            offset = anchor.find(f"{_WP}positionH/{_WP}posOffset")
            align = anchor.find(f"{_WP}positionH/{_WP}align")
            right = (align is not None and align.text == "right") or (
                offset is not None and (offset.text or "0").lstrip("-").isdigit()
                and int(offset.text or "0") / 12700 > self.text_width / 2)
            floated = "float:right;margin-left:12pt;" if right else "float:left;margin-right:12pt;"
        extent = element.find(f".//{_WP}extent")
        for box in element.iter(_TXBX):
            if any(ancestor.tag == _MC_FALLBACK for ancestor in box.iterancestors()):
                continue
            width = ""
            if extent is not None and extent.get("cx", "").isdigit():
                width = f"width:{int(extent.get('cx')) / 12700:.1f}pt;"
            blocks.append(f"<div class='textbox' style='{floated}{width}'>{self.blocks(box, part)}</div>")
        if not blocks:
            for blip in element.iter(_A_BLIP):
                image = self._image(blip, extent, part)
                if anchor is not None and image:
                    blocks.append(f"<div style='{floated}'>{image}</div>")
                else:
                    inline.append(image)
        return "".join(inline), "".join(blocks)

    def _runs(self, container: etree._Element, base: _RunStyle, part: object, blocks: list[str]) -> list[str]:
        pieces: list[str] = []
        for child in container:
            if child.tag in _SKIPPED:
                continue
            if child.tag in _RUN_CONTAINERS:
                pieces.extend(self._runs(child, base, part, blocks))
                continue
            if child.tag != qn("w:r"):
                continue
            style = self.resolver.run(child, base)
            if style.hidden:
                continue
            css = _run_css(style)
            for node in child:
                tag = node.tag
                if tag == qn("w:t"):
                    pieces.append(f"<span style=\"{css}\">{escaped(node.text or '')}</span>")
                elif tag == qn("w:tab"):
                    pieces.append("\t")
                elif tag in {qn("w:br"), qn("w:cr")}:
                    pieces.append("<br>")
                elif tag == qn("w:noBreakHyphen"):
                    pieces.append("‑")
                elif tag in {qn("w:drawing"), qn("w:pict"),
                             "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"}:
                    inline, block = self._drawing(node, part)
                    pieces.append(inline)
                    if block:
                        blocks.append(block)
        return pieces

    def paragraph(self, p: etree._Element, part: object) -> str:
        para, base = self.resolver.paragraph(p)
        extra_blocks: list[str] = []
        pieces = self._runs(p, base, part, extra_blocks)
        marker = ""
        if para.num is not None:
            glyph, level_para, level_run = self.resolver.marker(para.num)
            if para.left is None:
                para.left = level_para.left
                para.first = level_para.first
            if glyph:
                marker_style = base.merge(level_run)
                marker = (f"<span class='marker' style=\"{_run_css(marker_style)}\">{escaped(glyph)}</span>")
        rules = []
        if para.align:
            rules.append(f"text-align:{para.align}")
        rules.append(f"margin-top:{para.before or 0:.1f}pt;margin-bottom:{para.after or 0:.1f}pt")
        left = para.left or 0
        first = para.first or 0
        rules.append(f"padding-left:{max(left, 0):.1f}pt;padding-right:{max(para.right or 0, 0):.1f}pt")
        if para.line is not None:
            kind, value = para.line
            rules.append(f"line-height:{value * 1.15:.3f}" if kind == "auto" else f"line-height:{value:.1f}pt")
        else:
            rules.append("line-height:1.15")
        if base.size:
            rules.append(f"font-size:{base.size:.1f}pt")
        for side, border in para.borders.items():
            rules.append(f"border-{side}:{border};padding-{side}:1pt")
        if para.shading:
            rules.append(f"background:{para.shading}")
        content = "".join(pieces)
        if "\t" in content:
            segments = content.split("\t")
            if para.right_tab and len(segments) >= 2:
                content = (f"<span class='tabrow'><span>{''.join(segments[:-1])}</span>"
                           f"<span class='tabend'>{segments[-1]}</span></span>")
            else:
                content = "<span class='tab'></span>".join(segments)
        if marker:
            indent = -first if first < 0 else 14
            content = (f"<span class='marker-box' style='width:{indent:.1f}pt;margin-left:-{indent:.1f}pt'>"
                       f"{marker}</span>{content}")
        elif first:
            rules.append(f"text-indent:{first:.1f}pt")
        if not content.strip():
            content = "&#8203;"
        return f"<p style=\"{';'.join(rules)}\">{content}</p>{''.join(extra_blocks)}"

    def table(self, table: etree._Element, part: object) -> str:
        grid = [(_twips_pt(col.get(qn("w:w"))) or 0) for col in table.iter(qn("w:gridCol"))]
        first_row = table.find(qn("w:tr"))
        if first_row is not None:
            # Les largeurs de cellule (tcW) priment sur la grille, souvent laissée uniforme par les générateurs.
            widths = [_twips_pt(_val(cell.find(f"{qn('w:tcPr')}/{qn('w:tcW')}"), "w:w"))
                      for cell in first_row.findall(qn("w:tc"))]
            if widths and all(widths) and len(widths) == len(grid):
                grid = [float(width) for width in widths if width]
        properties = table.find(qn("w:tblPr"))
        style_id = _val(properties.find(qn("w:tblStyle"))) if properties is not None else None
        borders = properties.find(qn("w:tblBorders")) if properties is not None else None
        bordered = style_id == "TableGrid" or (
            borders is not None and any(_val(border) not in {"nil", "none", None} for border in borders))
        rows: list[str] = []
        for row in table.findall(qn("w:tr")):
            cells: list[str] = []
            for cell in row.findall(qn("w:tc")):
                tcpr = cell.find(qn("w:tcPr"))
                merge = tcpr.find(qn("w:vMerge")) if tcpr is not None else None
                if merge is not None and _val(merge) != "restart":
                    continue
                span = _val(tcpr.find(qn("w:gridSpan"))) if tcpr is not None else None
                width = _twips_pt(_val(tcpr.find(qn("w:tcW")), "w:w")) if tcpr is not None else None
                shading = tcpr.find(qn("w:shd")) if tcpr is not None else None
                fill = shading.get(qn("w:fill")) if shading is not None else None
                style = f"width:{width:.1f}pt;" if width else ""
                if fill and re.fullmatch(r"[0-9A-Fa-f]{6}", fill):
                    style += f"background:#{fill};"
                colspan = f" colspan='{int(span)}'" if span and span.isdigit() else ""
                cells.append(f"<td{colspan} style='{style}'>{self.blocks(cell, part)}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        columns = "".join(f"<col style='width:{width:.1f}pt'>" for width in grid if width)
        css_class = "grid bordered" if bordered else "grid"
        return f"<table class='{css_class}'><colgroup>{columns}</colgroup>{''.join(rows)}</table>"

    def blocks(self, container: etree._Element, part: object) -> str:
        output: list[str] = []
        for child in container:
            if child.tag == qn("w:p"):
                output.append(self.paragraph(child, part))
            elif child.tag == qn("w:tbl"):
                output.append(self.table(child, part))
            elif child.tag == qn("w:sdt"):
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    output.append(self.blocks(content, part))
        return "".join(output)

    def html(self, *, title: str, author: str, language: str) -> str:
        section = self.document.sections[0]

        def pt(value: object) -> float:
            return (value or 0) / 12700  # type: ignore[operator]

        width, height = pt(section.page_width) or 595.3, pt(section.page_height) or 841.9
        margins = [pt(section.top_margin), pt(section.right_margin), pt(section.bottom_margin), pt(section.left_margin)]
        header = section.first_page_header if section.different_first_page_header_footer else section.header
        footer = section.first_page_footer if section.different_first_page_header_footer else section.footer
        # Une première section « liée à la précédente » n'a pas d'en-tête : y accéder en créerait un.
        header_html = "" if header.is_linked_to_previous else self.blocks(header._element, header.part)
        footer_html = "" if footer.is_linked_to_previous else self.blocks(footer._element, footer.part)
        body = self.blocks(self.document.element.body, self.document.part)
        css = (
            f"@page{{size:{width:.1f}pt {height:.1f}pt;margin:{margins[0]:.1f}pt {margins[1]:.1f}pt "
            f"{margins[2]:.1f}pt {margins[3]:.1f}pt}}"
            "html,body{margin:0;padding:0;background:#fff}"
            f"body{{color:#000;font-family:{font_stack(self.resolver.run_default.font)};"
            f"font-size:{self.resolver.run_default.size or 10:.1f}pt;font-variant-ligatures:none;"
            "-webkit-print-color-adjust:exact;print-color-adjust:exact}"
            "p{margin:0;overflow-wrap:anywhere}.tab{display:inline-block;width:36pt}"
            ".tabrow{display:flex;justify-content:space-between;gap:8pt}.tabend{white-space:nowrap}"
            ".marker-box{display:inline-block}"
            "table.grid{border-collapse:collapse;width:100%;table-layout:fixed}"
            "table.grid td{vertical-align:top;padding:0 5.4pt}"
            "table.bordered td{border:0.5pt solid #000}"
            ".textbox{box-sizing:border-box}.page-header{margin-bottom:6pt}.page-footer{margin-top:12pt}"
        )
        return (
            "<!doctype html>"
            f"<html lang='{escaped(language)}'><head><meta charset='utf-8'>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{CONTENT_SECURITY_POLICY}\">"
            f"<meta name='author' content='{escaped(author)}'><title>{escaped(title)}</title>"
            f"<style>{css}</style></head><body>"
            + (f"<div class='page-header'>{header_html}</div>" if header_html else "")
            + body
            + (f"<div class='page-footer'>{footer_html}</div>" if footer_html else "")
            + "</body></html>"
        )


def docx_to_html(document: DocumentObject, *, title: str, author: str, language: str) -> str:
    return _Converter(document).html(title=title, author=author, language=language)
