"""Contrôle ATS local, qui ne lève jamais.

Le PDF est relu par deux moteurs d'extraction (lecture linéaire du flux de texte, puis lecture spatiale
qui reconstruit les lignes d'après leur position, comme le font beaucoup d'ATS) et, s'il est fourni, le DOCX
est relu sans ses zones de texte ni ses en-têtes et pieds de page, souvent ignorés par les ATS. Une
information n'est comptée lisible que si chaque lecture la retrouve, sous le bon titre de section, dans
le bon élément et dans le bon ordre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from .common import (
    CV_SECTION_TYPES,
    compact_text,
    format_date,
    identify_section,
    language_of,
    normalize,
    normalize_keyword,
    project_setting_label,
    section_title,
    string_value,
)
from .models import AtsReport
from .template import location_of, paragraph_text, story_paragraphs

_LOOKBACK = 80  # caractères admis avant l'intitulé d'un élément (date placée avant le poste, par exemple)
_CURRENT = ("Aujourd'hui", "Present", "En cours", "À ce jour", "Current", "Now")
_ITEM_NOUNS = {"experience": "l'expérience", "project": "le projet", "education": "la formation",
               "certification": "la certification", "language": "la langue"}


@dataclass(frozen=True)
class _Expected:
    field: str
    label: str
    candidates: tuple[str, ...]


@dataclass
class _Item:
    anchor: _Expected
    fields: list[_Expected]


@dataclass
class _Section:
    type: str
    items: list[_Item] = field(default_factory=list)
    loose: list[_Expected] = field(default_factory=list)  # compétences : sans notion d'élément


@dataclass
class _Plan:
    identity: list[_Expected]
    sections: list[_Section]
    letter: list[_Expected]

    def all(self) -> list[_Expected]:
        expected = list(self.identity) + list(self.letter)
        for section in self.sections:
            for item in section.items:
                expected += [item.anchor, *item.fields]
            expected += section.loose
        return expected


@dataclass(frozen=True)
class _View:
    name: str
    text: str
    headings: tuple[tuple[str, int, int], ...]  # (type, début, fin) de chaque titre reconnu
    # Lignes sans adresses web ni e-mail : le nom doit y figurer en toutes lettres, pas seulement
    # dans « github.com/camille-martin », que la normalisation transformerait en « camille martin ».
    prose_lines: tuple[str, ...] = ()
    first_heading_line: int = 0


def _issue(code: str, message: str, field_name: str) -> dict:
    return {"code": code, "message": message, "field": field_name}


def _expected(field_name: str, label: str, *values: object) -> _Expected | None:
    candidates = tuple(dict.fromkeys(text for text in (normalize(value) for value in values) if text))
    return _Expected(field_name, label, candidates) if candidates else None


def _date_values(value: object, *, current: bool) -> tuple[object, ...]:
    if current:
        return _CURRENT
    return (compact_text(value), format_date(value))


def _identity_plan(identity: dict) -> list[_Expected]:
    expected: list[_Expected | None] = [
        _expected("identity.full_name", "Le nom", identity.get("full_name")),
        _expected("identity.email", "L'adresse e-mail", identity.get("email")),
        _expected("identity.phone", "Le téléphone", identity.get("phone")),
        _expected("identity.city", "La ville", identity.get("city")),
    ]
    links = identity.get("links")
    for index, link in enumerate(links if isinstance(links, list) else []):
        if isinstance(link, dict):
            url = compact_text(link.get("url"))
            bare = url.split("://", 1)[-1].removeprefix("www.").rstrip("/")
            expected.append(_expected(f"identity.links[{index}]", "Un lien", url, bare))
    return [item for item in expected if item is not None]


def _item_plan(section_type: str, key: str, item: dict, language: str) -> _Item | None:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    prefix = f"{section_type}.{key}"
    expected: list[_Expected | None] = []
    if section_type in {"experience", "project"}:
        title = compact_text(fields.get("title"))
        noun = _ITEM_NOUNS[section_type]
        ref = f"de {noun} « {title} »" if title else f"de {noun}"
        expected.append(_expected(f"{prefix}.title", f"L'intitulé {ref}", title))
        structure = compact_text(fields.get("organization"))
        values: tuple[object, ...] = (structure,)
        if not structure and section_type == "project":
            values = (project_setting_label(fields.get("setting"), language),)
        expected.append(_expected(f"{prefix}.organization", f"La structure {ref}", *values))
        if compact_text(fields.get("start")) or compact_text(fields.get("end")):
            expected.append(_expected(f"{prefix}.start", f"La date de début {ref}",
                                      *_date_values(fields.get("start"), current=False)))
            expected.append(_expected(f"{prefix}.end", f"La date de fin {ref}",
                                      *_date_values(fields.get("end"), current=not compact_text(fields.get("end")))))
    elif section_type == "education":
        degree = compact_text(fields.get("degree") or fields.get("title"))
        ref = f"de la formation « {degree} »" if degree else "de la formation"
        expected.append(_expected(f"{prefix}.degree", f"Le diplôme {ref}", degree))
        expected.append(_expected(f"{prefix}.school", f"L'établissement {ref}",
                                  fields.get("school") or fields.get("organization")))
        if compact_text(fields.get("start")) or compact_text(fields.get("end")):
            expected.append(_expected(f"{prefix}.start", f"La date de début {ref}",
                                      *_date_values(fields.get("start"), current=False)))
            expected.append(_expected(f"{prefix}.end", f"La date de fin {ref}",
                                      *_date_values(fields.get("end"), current=not compact_text(fields.get("end")))))
    elif section_type == "certification":
        name = compact_text(fields.get("name") or fields.get("title"))
        ref = f"de la certification « {name} »" if name else "de la certification"
        expected.append(_expected(f"{prefix}.name", f"L'intitulé {ref}", name))
        expected.append(_expected(f"{prefix}.issuer", f"L'organisme {ref}", fields.get("issuer")))
        when = fields.get("date") or fields.get("start")
        if compact_text(when):
            expected.append(_expected(f"{prefix}.date", f"La date {ref}", *_date_values(when, current=False)))
    elif section_type == "language":
        name = compact_text(fields.get("name") or fields.get("title"))
        expected.append(_expected(f"{prefix}.name", "Une langue", name))
        expected.append(_expected(f"{prefix}.level", f"Le niveau de langue « {name} »", fields.get("level")))
    present = [item for item in expected if item is not None]
    if not present:
        return None
    return _Item(present[0], present[1:])


_ESSENTIAL_SUFFIXES = (".title", ".organization", ".start", ".end", ".degree", ".school")


def _is_essential(field_name: str) -> bool:
    """Champs sans lesquels un CV est défectueux, quel que soit le score global : leur perte bloque « prêt »."""
    if field_name in ("identity.full_name", "identity.email"):
        return True
    return not field_name.startswith(("identity.", "letter.", "skills.")) and field_name.endswith(_ESSENTIAL_SUFFIXES)


def _plan(document: dict, identity: dict) -> _Plan:
    language = language_of(document)
    plan = _Plan(_identity_plan(identity), [], [])
    if document.get("kind") == "lettre":
        subject = _expected("letter.subject", "L'objet", document.get("subject"))
        greeting = _expected("letter.greeting", "La formule d'appel", document.get("greeting"))
        paragraphs = document.get("paragraphs")
        body = [_expected(f"letter.paragraphs[{index}]", f"Le paragraphe {index + 1}", text)
                for index, text in enumerate(paragraphs if isinstance(paragraphs, list) else [])]
        closing = _expected("letter.closing", "La formule de politesse", document.get("closing"))
        plan.letter = [item for item in (subject, greeting, *body, closing) if item is not None]
        return plan
    sections = document.get("sections")
    seen: set[str] = set()
    for section_index, section in enumerate(sections if isinstance(sections, list) else []):
        if not isinstance(section, dict):
            continue
        section_type = string_value(section.get("type"))
        if section_type not in CV_SECTION_TYPES or section_type in seen:
            continue
        seen.add(section_type)
        planned = _Section(section_type)
        if section_type == "skills":
            groups = section.get("groups")
            for group_index, group in enumerate(groups if isinstance(groups, list) else []):
                skills = group.get("skills") if isinstance(group, dict) else None
                for skill_index, skill in enumerate(skills if isinstance(skills, list) else []):
                    if isinstance(skill, dict):
                        key = compact_text(skill.get("ref")) or f"{group_index}.{skill_index}"
                        text = compact_text(skill.get("text"))
                        item = _expected(f"skills.{key}", f"La compétence « {text} »", text)
                        if item is not None:
                            planned.loose.append(item)
        else:
            items = section.get("items")
            for item_index, item in enumerate(items if isinstance(items, list) else []):
                if isinstance(item, dict):
                    key = compact_text(item.get("ref")) or f"{section_index}.{item_index}"
                    planned_item = _item_plan(section_type, key, item, language)
                    if planned_item is not None:
                        planned.items.append(planned_item)
        if planned.items or planned.loose:
            plan.sections.append(planned)
    return plan


_ADDRESS = re.compile(r"\S+@\S+|(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+/\S*", re.IGNORECASE)


def _view(name: str, raw: str) -> _View:
    parts: list[str] = []
    prose: list[str] = []
    headings: list[tuple[str, int, int]] = []
    first_heading_line = None
    position = 0
    for line in raw.splitlines():
        normalized = normalize(line)
        if not normalized:
            continue
        section_type = identify_section(normalized) if len(normalized) <= 45 else None
        if section_type is not None:
            headings.append((section_type, position, position + len(normalized)))
            if first_heading_line is None:
                first_heading_line = len(prose)
        parts.append(normalized)
        prose.append(normalize(_ADDRESS.sub(" ", line)))
        position += len(normalized) + 1
    return _View(name, " ".join(parts), tuple(headings), tuple(prose),
                 len(prose) if first_heading_line is None else first_heading_line)


def _in_lines(lines: tuple[str, ...], expected: _Expected) -> bool:
    return any(f" {candidate} " in f" {line} " for line in lines for candidate in expected.candidates)


def _find(view: _View, expected: _Expected, low: int, high: int) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    for candidate in expected.candidates:
        start = max(low, 0)
        while True:
            position = view.text.find(candidate, start, high)
            if position < 0:
                break
            end = position + len(candidate)
            # Correspondance sur des mots entiers : « 2024 » ne doit pas être trouvé dans « 20245 ».
            before_ok = position == 0 or view.text[position - 1] == " "
            after_ok = end == len(view.text) or view.text[end] == " "
            if before_ok and after_ok:
                if best is None or position < best[0]:
                    best = (position, end)
                break
            start = position + 1
    return best


def _increasing(values: list[int]) -> set[int]:
    """Indices de la plus longue sous-suite croissante : les autres éléments sont hors de leur place."""
    if not values:
        return set()
    lengths = [1] * len(values)
    previous = [-1] * len(values)
    for index in range(len(values)):
        for before in range(index):
            if values[before] < values[index] and lengths[before] + 1 > lengths[index]:
                lengths[index], previous[index] = lengths[before] + 1, before
    index = max(range(len(values)), key=lambda position: lengths[position])
    kept: set[int] = set()
    while index >= 0:
        kept.add(index)
        index = previous[index]
    return kept


def _evaluate(view: _View, plan: _Plan, language: str) -> tuple[set[str], list[dict]]:
    found: set[str] = set()
    issues: list[dict] = []
    first_heading = {}
    for section_type, start, end in view.headings:
        first_heading.setdefault(section_type, (start, end))
    boundaries = sorted(start for _, start, _ in view.headings)
    top_end = boundaries[0] if boundaries else len(view.text)

    if plan.letter:
        anchor = None
        for expected in plan.letter[:2]:
            located = _find(view, expected, 0, len(view.text))
            if located is not None:
                anchor = located[0]
                break
        top_end = anchor if anchor is not None else len(view.text)
    for expected in plan.identity:
        if expected.field == "identity.full_name":
            # Une lettre peut être signée en fin de document ; un CV porte le nom dans son en-tête.
            header = view.prose_lines if plan.letter else view.prose_lines[:view.first_heading_line]
            if _in_lines(header, expected):
                found.add(expected.field)
            elif _in_lines(view.prose_lines, expected):
                issues.append(_issue("identity_misplaced", f"{expected.label} n'est pas en tête du document "
                                     f"({view.name}).", expected.field))
            continue
        if _find(view, expected, 0, top_end) is not None:
            found.add(expected.field)
        elif expected.field != "identity.city" and _find(view, expected, 0, len(view.text)) is not None:
            issues.append(_issue("identity_misplaced", f"{expected.label} n'est pas en tête du document "
                                 f"({view.name}).", expected.field))

    if plan.letter:
        cursor = 0
        for expected in plan.letter:
            located = _find(view, expected, cursor, len(view.text))
            if located is not None:
                found.add(expected.field)
                cursor = located[1]
            elif _find(view, expected, 0, len(view.text)) is not None:
                issues.append(_issue("letter_order", f"{expected.label} n'est pas à sa place ({view.name}).",
                                     expected.field))
        return found, issues

    present = [section for section in plan.sections if section.type in first_heading]
    for section in plan.sections:
        if section.type not in first_heading:
            issues.append(_issue("section_missing", f"Le titre de section « {section_title(section.type, language)} » "
                                 f"n'est pas reconnu ({view.name}) : utilise un intitulé standard.",
                                 f"sections.{section.type}"))
    kept = _increasing([first_heading[section.type][0] for section in present])
    for index, section in enumerate(present):
        if index not in kept:
            issues.append(_issue("section_order", f"La section « {section_title(section.type, language)} » n'est pas "
                                 f"à sa place dans l'ordre de lecture ({view.name}).", f"sections.{section.type}"))
    for index, section in enumerate(present):
        if index not in kept:
            continue
        heading_start, heading_end = first_heading[section.type]
        span_end = next((start for start in boundaries if start > heading_start), len(view.text))
        for expected in section.loose:
            if _find(view, expected, heading_end, span_end) is not None:
                found.add(expected.field)
        anchors: list[tuple[int, int] | None] = []
        cursor = heading_end
        for item in section.items:
            located = _find(view, item.anchor, cursor, span_end)
            if located is not None:
                anchors.append(located)
                cursor = located[1]
                continue
            anchors.append(None)
            if _find(view, item.anchor, heading_end, span_end) is not None:
                issues.append(_issue("item_order", f"{item.anchor.label} n'est pas dans l'ordre attendu "
                                     f"({view.name}).", item.anchor.field))
        for position, item in enumerate(section.items):
            located = anchors[position]
            previous_end = max((a[1] for a in anchors[:position] if a is not None), default=heading_end)
            next_start = min((a[0] for a in anchors[position + 1:] if a is not None), default=span_end)
            if located is not None:
                found.add(item.anchor.field)
                low, high = located[0], next_start
                back = max(previous_end, located[0] - _LOOKBACK)
            else:
                if any(issue["field"] == item.anchor.field for issue in issues):
                    continue
                low, high, back = previous_end, next_start, previous_end
            for expected in item.fields:
                if _find(view, expected, low, high) is not None or _find(view, expected, back, low) is not None:
                    found.add(expected.field)
    return found, issues


def _pdf_resources(resources: object, images: list[bool], fonts: list[bool], depth: int = 0) -> None:
    if depth > 4 or resources is None:
        return
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    if not isinstance(resources, dict):
        return
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    for reference in (xobjects or {}).values():
        xobject = reference.get_object()
        if xobject.get("/Subtype") == "/Image":
            images.append(True)
        elif xobject.get("/Subtype") == "/Form":
            _pdf_resources(xobject.get("/Resources"), images, fonts, depth + 1)
    font_dict = resources.get("/Font")
    font_dict = font_dict.get_object() if hasattr(font_dict, "get_object") else font_dict
    for reference in (font_dict or {}).values():
        font = reference.get_object()
        subtype = font.get("/Subtype")
        if subtype == "/Type3":
            continue
        if subtype == "/Type0":
            descendants = font.get("/DescendantFonts")
            descendants = descendants.get_object() if hasattr(descendants, "get_object") else descendants
            font = descendants[0].get_object() if descendants else font
        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if descriptor is not None else None
        fonts.append(bool(descriptor) and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")))


def _extract_pdf(path: Path) -> tuple[str, str, bool, bool]:
    reader = PdfReader(path)
    plain: list[str] = []
    spatial: list[str] = []
    images: list[bool] = []
    fonts: list[bool] = []
    for page in reader.pages:
        plain.append(page.extract_text() or "")
        try:
            spatial.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            spatial.append("")
        _pdf_resources(page.get("/Resources"), images, fonts)
    return "\n".join(plain), "\n".join(spatial), bool(images), all(fonts)


def _extract_docx(path: Path) -> tuple[str, bool]:
    """Texte du corps dans l'ordre du document, cellules comprises, sans zones de texte ni en-têtes."""
    document = Document(str(path))
    lines = [paragraph_text(p) for p in story_paragraphs(document.element.body) if location_of(p) != "textbox"]
    blip = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    has_image = any(True for _ in document.element.body.iter(blip))
    return "\n".join(lines), has_image


def _keyword_report(extracted_text: str, offer_keywords: object, owned_keywords: object) -> dict:
    text = f" {normalize_keyword(extracted_text)} "
    offer = [compact_text(k) for k in offer_keywords if compact_text(k)] if isinstance(offer_keywords, list) else []
    owned = [compact_text(k) for k in owned_keywords if compact_text(k)] if isinstance(owned_keywords, list) else []
    owned_norms = {normalize_keyword(keyword) for keyword in owned}
    if offer:
        # Mots-clés de l'offre que le candidat possède, dans la formulation exacte de l'offre.
        considered = [keyword for keyword in offer if normalize_keyword(keyword) in owned_norms]
    else:
        considered = owned
    unique: list[str] = []
    for keyword in considered:
        if normalize_keyword(keyword) not in {normalize_keyword(item) for item in unique}:
            unique.append(keyword)
    present = [keyword for keyword in unique if f" {normalize_keyword(keyword)} " in text]
    not_owned: list[str] = []
    for keyword in offer:
        normalized = normalize_keyword(keyword)
        if normalized not in owned_norms and normalized not in {normalize_keyword(item) for item in not_owned}:
            not_owned.append(keyword)
    return {
        "owned_present": len(present),
        "owned_total": len(unique),
        "missing_owned": [keyword for keyword in unique if keyword not in present],
        "not_owned": not_owned,
    }


def _safe_keywords(extracted_text: str, offer_keywords: object, owned_keywords: object) -> dict:
    try:
        return _keyword_report(extracted_text, offer_keywords, owned_keywords)
    except Exception:
        return {"owned_present": 0, "owned_total": 0, "missing_owned": [], "not_owned": []}


def ats_check(
    pdf_path: Path,
    *,
    composition: dict,
    identity: dict,
    offer_keywords: list[str],
    owned_keywords: list[str],
    docx_path: Path | None = None,
) -> AtsReport:
    """Mesure la lisibilité ATS du document produit ; renvoie toujours un rapport, sans lever."""
    extracted_text = ""
    try:
        if not isinstance(composition, dict) or composition.get("kind") not in {"cv", "lettre"}:
            raise ValueError("composition invalide")
        identity = identity if isinstance(identity, dict) else {}
        language = language_of(composition)
        plain, spatial, has_image, fonts_embedded = _extract_pdf(Path(pdf_path))
        extracted_text = plain
        views = [_view("lecture linéaire du PDF", plain), _view("lecture spatiale du PDF", spatial)]
        docx_has_image = False
        if docx_path is not None:
            docx_text, docx_has_image = _extract_docx(Path(docx_path))
            views.append(_view("DOCX", docx_text))
        plan = _plan(composition, identity)
        expected = plan.all()

        issues: list[dict] = []
        found_by_view: list[set[str]] = []
        for view in views:
            found, view_issues = _evaluate(view, plan, language)
            found_by_view.append(found)
            issues += view_issues
        for item in expected:
            missing = [view.name for view, found in zip(views, found_by_view, strict=True) if item.field not in found]
            if missing and not any(issue["field"] == item.field for issue in issues):
                issues.append(_issue("field_missing", f"{item.label} est introuvable à sa place "
                                     f"({', '.join(missing)}).", item.field))
        readable = sum(1 for item in expected if all(item.field in found for found in found_by_view))

        plain_headings = {kind for kind, _, _ in views[0].headings}
        spatial_headings = {kind for kind, _, _ in views[1].headings}
        if plain_headings != spatial_headings:
            issues.append(_issue("layout_columns", "La lecture spatiale ne retrouve pas les mêmes titres de section que "
                                 "la lecture linéaire : mise en page en colonnes ou éléments côte à côte probables.",
                                 "document.layout"))
        if not views[0].text:
            issues.append(_issue("no_text", "Aucun texte n'a pu être extrait du PDF : il est peut-être composé "
                                 "d'images, illisibles pour un ATS.", "document.text"))
        if has_image or docx_has_image:
            issues.append(_issue("image_content", "Le document contient une image : un texte porté par une image "
                                 "n'est pas lu par un ATS.", "document.images"))
        if not fonts_embedded:
            issues.append(_issue("font_not_embedded", "Au moins une police du PDF n'est pas intégrée au fichier.",
                                 "document.fonts"))
        if not views[0].text:
            readability = 0.0
        else:
            readability = round(100 * readable / len(expected), 1) if expected else 100.0
        keyword_coverage = _safe_keywords(extracted_text, offer_keywords, owned_keywords)
        if keyword_coverage["missing_owned"]:
            issues.append(_issue("owned_keywords_missing", "Mots-clés de l'offre que tu possèdes mais absents du "
                                 f"document : {', '.join(keyword_coverage['missing_owned'])}.", "keywords"))
        blocking = [item.field for item in expected
                    if _is_essential(item.field) and not all(item.field in found for found in found_by_view)]
        if composition.get("kind") == "cv" and not compact_text(identity.get("full_name")):
            # Sans nom dans le profil, le plan ATS ne l'attend pas : il faut donc le bloquer explicitement.
            blocking.append("identity.full_name")
            issues.append(_issue("name_missing", "Le nom du candidat est absent du CV : un CV sans nom ne peut pas "
                                 "être envoyé. Renseigne-le dans « Mon profil », puis régénère le CV.",
                                 "identity.full_name"))
        if blocking:
            issues.append(_issue("essential_missing", "Information essentielle introuvable (intitulé, structure, "
                                 "dates, nom ou e-mail) : le document ne peut pas être marqué prêt tant qu'elle "
                                 "n'est pas lisible.", ", ".join(blocking)))
        return AtsReport(readability, keyword_coverage, issues, extracted_text, readability >= 95 and not blocking)
    except Exception:
        return AtsReport(
            0.0,
            _safe_keywords(extracted_text, offer_keywords, owned_keywords),
            [_issue("unreadable_document", "Le document n'a pas pu être analysé par le contrôle ATS : vérifie qu'il "
                    "s'agit bien du PDF (et du DOCX) produit pour cette composition.", "document")],
            extracted_text,
            False,
        )

