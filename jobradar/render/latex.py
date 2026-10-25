"""Génération LaTeX interne et compilation Tectonic confinée.

Le code TeX est entièrement construit par JobRadar. Les textes utilisateur passent tous par ``escape`` ;
aucun fichier ``.tex`` importé n'est accepté ni compilé.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import (
    compact_text,
    identity_lines,
    identity_parts,
    language_of,
    letter_date,
    letter_recipient,
    section_views,
)
from .models import RenderError

_SPECIAL = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def escape(value: object) -> str:
    """Échappe caractère par caractère afin que les remplacements ne soient jamais réinterprétés."""
    return "".join(_SPECIAL.get(char, char) for char in str(value or ""))


def _preamble(title: str, author: str, language: str) -> str:
    babel = "french" if language == "fr" else "english"
    return rf"""\documentclass[10pt,a4paper]{{article}}
\usepackage[{babel}]{{babel}}
\usepackage[a4paper,top=13mm,bottom=13mm,left=16mm,right=16mm]{{geometry}}
\usepackage{{fontspec,xcolor,enumitem,hyperref}}
\setmainfont{{TeX Gyre Heros}}
\definecolor{{accent}}{{HTML}}{{33429A}}
\definecolor{{muted}}{{HTML}}{{545E70}}
\hypersetup{{hidelinks,pdftitle={{{escape(title)}}},pdfauthor={{{escape(author)}}}}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{2pt}}
\setlist[itemize]{{leftmargin=4mm,itemsep=0.5pt,topsep=1pt}}
\pagestyle{{empty}}
\newcommand{{\sectiontitle}}[1]{{\vspace{{5pt}}\textcolor{{accent}}{{\bfseries\MakeUppercase{{#1}}}}\par\vspace{{1pt}}\hrule\vspace{{3pt}}}}
\begin{{document}}
"""


def _identity(identity: dict, language: str, headline: str) -> str:
    name, contacts, links = identity_lines(identity, language)
    lines = [rf"{{\LARGE\bfseries {escape(name)}}}"] if name else []
    if headline:
        lines.append(rf"\textcolor{{accent}}{{\large\bfseries {escape(headline)}}}")
    for values in (contacts, links):
        if values:
            lines.append(rf"\textcolor{{muted}}{{\small {escape(' · '.join(values))}}}")
    return "\\\\[2pt]\n".join(lines) + "\n"


def cv_source(composition: dict, identity: dict, *, title: str, author: str) -> str:
    language = language_of(composition)
    parts = [_preamble(title, author, language), _identity(identity, language, compact_text(composition.get("headline")))]
    summary = compact_text(composition.get("summary"))
    if summary:
        parts.append(escape(summary) + "\n")
    for view in section_views(composition):
        parts.append(rf"\sectiontitle{{{escape(view.title)}}}" + "\n")
        for entry in view.entries:
            heading = rf"\textbf{{{escape(entry.title)}}}"
            if entry.period:
                heading += rf"\hfill\textcolor{{muted}}{{{escape(entry.period)}}}"
            parts.append(heading + "\\\\\n")
            if entry.subtitle:
                parts.append(rf"\textcolor{{muted}}{{{escape(entry.subtitle)}}}" + "\\\\\n")
            for detail in [*entry.details, *entry.tech]:
                parts.append(rf"{{\small\textcolor{{muted}}{{{escape(detail)}}}}}" + "\\\\\n")
            if entry.bullets:
                parts.append("\n\\begin{itemize}\n")
                parts.extend(rf"\item {escape(bullet)}" + "\n" for bullet in entry.bullets)
                parts.append("\\end{itemize}\n")
        for label, text in view.lines:
            prefix = rf"\textbf{{{escape(label)} : }}" if label else ""
            parts.append(prefix + escape(text) + "\\\\\n")
    parts.append("\\end{document}\n")
    return "".join(parts)


def letter_source(letter: dict, identity: dict, *, title: str, author: str) -> str:
    language = language_of(letter)
    name, _ = identity_parts(identity, language)
    parts = [_preamble(title, author, language), _identity(identity, language, "")]
    recipient = letter_recipient(letter)
    if recipient:
        parts.append("\\begin{flushright}" + "\\\\\n".join(escape(line) for line in recipient)
                     + "\\end{flushright}\n")
    parts.append(rf"\begin{{flushright}}{escape(letter_date(language, compact_text(identity.get('city'))))}\end{{flushright}}" + "\n")
    subject = compact_text(letter.get("subject"))
    if subject:
        parts.append(rf"\textbf{{{'Objet' if language == 'fr' else 'Subject'} : {escape(subject)}}}\par\medskip" + "\n")
    greeting = compact_text(letter.get("greeting"))
    if greeting:
        parts.append(escape(greeting) + "\\par\n")
    parts.extend(escape(paragraph) + "\\par\n" for paragraph in letter.get("paragraphs") or [] if compact_text(paragraph))
    closing = compact_text(letter.get("closing"))
    if closing:
        parts.append(escape(closing) + "\\par\n")
    if name:
        parts.append(rf"\medskip\textbf{{{escape(name)}}}" + "\n")
    parts.append("\\end{document}\n")
    return "".join(parts)


def compile_pdf(source: str) -> bytes | None:
    """Compile sans réseau et sans fonctions dangereuses ; ``None`` signifie Tectonic absent."""
    executable = shutil.which("tectonic")
    if executable is None:
        return None
    allowed_env = {key: os.environ[key] for key in ("PATH", "HOME", "XDG_CACHE_HOME", "SYSTEMROOT",
                                                     "LOCALAPPDATA", "APPDATA") if key in os.environ}
    allowed_env.update({"TECTONIC_UNTRUSTED_MODE": "1", "SOURCE_DATE_EPOCH": "0"})
    try:
        with tempfile.TemporaryDirectory(prefix="jobradar-tectonic-") as name:
            directory = Path(name)
            tex = directory / "document.tex"
            tex.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [executable, "-X", "compile", "--untrusted", "--only-cached", "--outdir", str(directory), str(tex)],
                cwd=directory, env=allowed_env, capture_output=True, text=True, check=False, timeout=90,
            )
            pdf = directory / "document.pdf"
            if result.returncode != 0 or not pdf.is_file():
                lines = (result.stderr or result.stdout or "").strip().splitlines()
                message = (lines[-1] if lines else "erreur inconnue")[:300]
                raise RenderError(f"Tectonic n'a pas pu compiler le modèle LaTeX : {message}")
            content = pdf.read_bytes()
            if not content.startswith(b"%PDF"):
                raise RenderError("Tectonic a produit un fichier PDF invalide.")
            return content
    except subprocess.TimeoutExpired as exc:
        raise RenderError("La compilation LaTeX a dépassé 90 secondes.") from exc
    except OSError as exc:
        raise RenderError("Tectonic n'a pas pu être exécuté.") from exc
