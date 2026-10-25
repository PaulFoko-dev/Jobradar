"""Gabarits HTML/CSS embarqués des modèles JobRadar (une colonne, ordre de lecture = ordre visuel)."""

from __future__ import annotations

from dataclasses import dataclass

from .chromium import CONTENT_SECURITY_POLICY
from .common import (
    Entry,
    SectionView,
    colon,
    compact_text,
    escaped,
    identity_lines,
    identity_parts,
    language_of,
    letter_date,
    letter_recipient,
    section_views,
)


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    font: str
    heading_font: str
    text: str
    muted: str
    accent: str
    rule: str
    name_size: float
    docx_font: str


THEMES: dict[str, Theme] = {
    "sobre-ats": Theme(
        id="sobre-ats",
        name="Sobre ATS",
        description="Classique et compact, en noir et gris ; titres de section en capitales soulignés d'un filet.",
        font="Arial, 'Liberation Sans', Helvetica, sans-serif",
        heading_font="Arial, 'Liberation Sans', Helvetica, sans-serif",
        text="#1A1A1A",
        muted="#4A4A4A",
        accent="#1A1A1A",
        rule="#1A1A1A",
        name_size=21,
        docx_font="Arial",
    ),
    "moderne-ats": Theme(
        id="moderne-ats",
        name="Moderne ATS",
        description="Une colonne aérée avec un accent bleu canard discret sur le titre du poste et les sections.",
        font="Calibri, Carlito, 'Segoe UI', 'Liberation Sans', Arial, sans-serif",
        heading_font="Calibri, Carlito, 'Segoe UI', 'Liberation Sans', Arial, sans-serif",
        text="#1D2B30",
        muted="#50646B",
        accent="#0B6E79",
        rule="#BFD8DB",
        name_size=24,
        docx_font="Calibri",
    ),
    "technique-projets": Theme(
        id="technique-projets",
        name="Technique/Projets",
        description="Hiérarchie nette qui met en avant réalisations, projets et technologies ; accent indigo.",
        font="'Segoe UI', 'Noto Sans', 'DejaVu Sans', Arial, sans-serif",
        heading_font="'Segoe UI', 'Noto Sans', 'DejaVu Sans', Arial, sans-serif",
        text="#1C2230",
        muted="#545E70",
        accent="#33429A",
        rule="#C9CEDC",
        name_size=21,
        docx_font="Segoe UI",
    ),
}

# Densités essayées dans l'ordre pour tenir sur une page (facteur appliqué aux tailles et espacements).
DENSITIES = (1.0, 0.94, 0.88)


def _css(theme: Theme, scale: float) -> str:
    def pt(value: float) -> str:
        return f"{value * scale:.2f}pt"

    def mm(value: float) -> str:
        return f"{value * (0.6 + 0.4 * scale):.2f}mm"

    css = f"""
@page{{size:A4;margin:{mm(14)} {mm(16)} {mm(13)}}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0;background:#fff}}
body{{color:{theme.text};font-family:{theme.font};font-size:{pt(9.8)};line-height:1.34;
     font-variant-ligatures:none;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
h1,h2,p,ul{{margin:0;padding:0}}
.identity{{margin-bottom:{pt(9)}}}
.name{{font-family:{theme.heading_font};font-size:{pt(theme.name_size)};font-weight:700;line-height:1.1;
      letter-spacing:.2px;color:{theme.text}}}
.headline{{font-size:{pt(11.5)};font-weight:600;color:{theme.accent};margin-top:{pt(3)}}}
.contact{{font-size:{pt(8.8)};color:{theme.muted};margin-top:{pt(4)};line-height:1.45}}
.contact .item{{white-space:nowrap}}
.contact + .contact{{margin-top:{pt(1)}}}
.summary{{margin:0 0 {pt(2)};}}
section{{margin-top:{pt(10)}}}
section.keep{{break-inside:avoid}}
h2{{font-family:{theme.heading_font};font-size:{pt(10.5)};font-weight:700;color:{theme.accent};
    margin-bottom:{pt(5)};padding-bottom:{pt(2)};break-after:avoid}}
.entry{{margin-top:{pt(5)};break-inside:avoid}}
h2 + .entry{{margin-top:0}}
/* Un intitulé trop long pour partager sa ligne renvoie la date à la ligne suivante : jamais de date
   intercalée entre deux lignes d'intitulé, ce qui casserait la lecture spatiale des ATS. */
.entry-head{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;column-gap:{pt(10)}}}
.entry-title{{font-weight:700;flex:0 1 auto}}
.entry-inline{{font-weight:400;color:{theme.muted}}}
.entry.compact{{margin-top:{pt(2.5)}}}
.entry-date{{flex:0 0 auto;margin-left:auto;white-space:nowrap;color:{theme.muted};font-size:{pt(9.2)}}}
.entry-sub{{color:{theme.muted};font-style:italic}}
.entry-detail,.entry-tech{{color:{theme.muted};font-size:{pt(9.2)}}}
ul{{margin:{pt(2)} 0 0 {pt(13)}}}
li{{margin:{pt(1.2)} 0 0;padding-left:{pt(2)}}}
.line{{margin-top:{pt(2)}}}
.line .label{{font-weight:700}}
.letter .identity{{margin-bottom:{pt(24)}}}
.letter p{{font-size:{pt(10.5)};line-height:1.5;margin:0 0 {pt(9)}}}
.letter .identity p.headline,.letter .identity p.contact{{margin-bottom:0}}
.letter .identity p.contact{{font-size:{pt(8.8)};line-height:1.45}}
.letter .subject{{font-weight:700;margin:0 0 {pt(16)}}}
.letter .recipient{{margin-left:55%}}.letter .recipient p{{margin:0;line-height:1.4}}
.letter .date{{text-align:right;margin:{pt(14)} 0 {pt(22)}}}
.letter .greeting{{margin-bottom:{pt(10)}}}
.letter .closing{{margin-top:{pt(4)}}}
.letter .signature{{font-weight:700;margin-top:{pt(18)}}}
"""
    if theme.id == "sobre-ats":
        css += f"""
.headline{{color:{theme.text}}}
h2{{text-transform:uppercase;letter-spacing:1px;border-bottom:0.8pt solid {theme.rule}}}
"""
    elif theme.id == "moderne-ats":
        css += f"""
.identity{{border-bottom:2pt solid {theme.accent};padding-bottom:{pt(8)}}}
h2{{font-size:{pt(11.5)};border-bottom:0.8pt solid {theme.rule};padding-bottom:{pt(2.5)}}}
.entry-title{{color:{theme.text}}}
li::marker{{color:{theme.accent}}}
"""
    else:
        css += f"""
.identity{{padding-bottom:{pt(6)};border-bottom:0.8pt solid {theme.rule}}}
h2{{text-transform:uppercase;letter-spacing:1.3px;font-size:{pt(9.8)};border-bottom:0.8pt solid {theme.rule}}}
.entry-date{{font-variant-numeric:tabular-nums}}
.entry-sub{{font-style:normal;color:{theme.accent}}}
.entry-tech{{font-family:'DejaVu Sans Mono',Consolas,'Liberation Mono',monospace;font-size:{pt(8.4)}}}
.line .label{{color:{theme.accent}}}
li::marker{{color:{theme.accent}}}
"""
    return css


def _document(body: str, title: str, author: str, language: str, css: str) -> str:
    return (
        "<!doctype html>"
        f"<html lang='{escaped(language)}'><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{CONTENT_SECURITY_POLICY}\">"
        f"<meta name='author' content='{escaped(author)}'><title>{escaped(title)}</title>"
        f"<style>{css}</style></head><body>{body}</body></html>"
    )


def _identity_block(identity: dict, language: str, headline: str) -> str:
    name, contacts, links = identity_lines(identity, language)
    parts = ["<header class='identity'>"]
    if name:
        parts.append(f"<h1 class='name'>{escaped(name)}</h1>")
    if headline:
        parts.append(f"<p class='headline'>{escaped(headline)}</p>")
    # Coordonnées puis liens sur deux lignes distinctes : pas de séparateur orphelin en fin de ligne.
    for line in (contacts, links):
        if line:
            items = "<span class='sep'> · </span>".join(f"<span class='item'>{escaped(item)}</span>" for item in line)
            parts.append(f"<p class='contact'>{items}</p>")
    parts.append("</header>")
    return "".join(parts)


def _entry_html(entry: Entry, *, inline_subtitle: bool) -> str:
    title = escaped(entry.title)
    if inline_subtitle and entry.subtitle:
        title += f"<span class='entry-inline'> — {escaped(entry.subtitle)}</span>"
    head = f"<span class='entry-title'>{title}</span>"
    if entry.period:
        head += f"<span class='entry-date'>{escaped(entry.period)}</span>"
    css_class = "entry compact" if inline_subtitle else "entry"
    parts = [f"<div class='{css_class}'><div class='entry-head'>{head}</div>"]
    if entry.subtitle and not inline_subtitle:
        parts.append(f"<div class='entry-sub'>{escaped(entry.subtitle)}</div>")
    parts.extend(f"<div class='entry-detail'>{escaped(detail)}</div>" for detail in entry.details)
    parts.extend(f"<div class='entry-tech'>{escaped(line)}</div>" for line in entry.tech)
    if entry.bullets:
        parts.append("<ul>" + "".join(f"<li>{escaped(bullet)}</li>" for bullet in entry.bullets) + "</ul>")
    parts.append("</div>")
    return "".join(parts)


def _section_html(view: SectionView, language: str) -> str:
    # Une certification tient sur une ligne : « Intitulé — Organisme ........ date ».
    inline = view.type == "certification"
    content = "".join(_entry_html(entry, inline_subtitle=inline) for entry in view.entries)
    for label, text in view.lines:
        label_html = f"<span class='label'>{escaped(label + colon(language).rstrip())}</span> " if label else ""
        content += f"<p class='line'>{label_html}{escaped(text)}</p>"
    # Les sections courtes ne sont jamais coupées entre deux pages (pas d'élément isolé en haut de page).
    keep = " class='keep'" if view.type not in {"experience", "project"} else ""
    return f"<section{keep}><h2>{escaped(view.title)}</h2>{content}</section>"


def cv_html(composition: dict, identity: dict, theme_id: str, *, title: str, author: str, scale: float = 1.0) -> str:
    return cv_html_theme(composition, identity, THEMES[theme_id], title=title, author=author, scale=scale)


def cv_html_theme(composition: dict, identity: dict, theme: Theme, *, title: str, author: str,
                  scale: float = 1.0) -> str:
    language = language_of(composition)
    body = _identity_block(identity, language, compact_text(composition.get("headline")))
    summary = compact_text(composition.get("summary"))
    if summary:
        body += f"<p class='summary'>{escaped(summary)}</p>"
    body += "".join(_section_html(view, language) for view in section_views(composition))
    return _document(body, title, author, language, _css(theme, scale))


def letter_html(letter: dict, identity: dict, theme_id: str, *, title: str, author: str, scale: float = 1.0) -> str:
    return letter_html_theme(letter, identity, THEMES[theme_id], title=title, author=author, scale=scale)


def letter_html_theme(letter: dict, identity: dict, theme: Theme, *, title: str, author: str,
                      scale: float = 1.0) -> str:
    language = language_of(letter)
    name, _ = identity_parts(identity, language)
    body = "<div class='letter'>" + _identity_block(identity, language, "")
    recipient = letter_recipient(letter)
    if recipient:
        body += "<div class='recipient'>" + "".join(f"<p>{escaped(line)}</p>" for line in recipient) + "</div>"
    body += f"<p class='date'>{escaped(letter_date(language, compact_text(identity.get('city'))))}</p>"
    subject = compact_text(letter.get("subject"))
    if subject:
        label = "Subject:" if language == "en" else "Objet :"
        body += f"<p class='subject'>{label} {escaped(subject)}</p>"
    greeting = compact_text(letter.get("greeting"))
    if greeting:
        body += f"<p class='greeting'>{escaped(greeting)}</p>"
    paragraphs = letter.get("paragraphs")
    for paragraph in paragraphs if isinstance(paragraphs, list) else []:
        if compact_text(paragraph):
            body += f"<p>{escaped(compact_text(paragraph))}</p>"
    closing = compact_text(letter.get("closing"))
    if closing:
        body += f"<p class='closing'>{escaped(closing)}</p>"
    if name:
        body += f"<p class='signature'>{escaped(name)}</p>"
    body += "</div>"
    return _document(body, title, author, language, _css(theme, scale))
