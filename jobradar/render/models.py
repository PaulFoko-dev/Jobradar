"""Objets publics du rendu de documents JobRadar."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RenderError(RuntimeError):
    """Échec contrôlé de production d'un document."""


@dataclass(frozen=True)
class RenderResult:
    pdf_path: Path
    docx_path: Path | None
    warnings: list[str]


@dataclass(frozen=True)
class TemplateMap:
    source_path: Path
    sections: list[dict]
    header: dict
    issues: list[str]
    ats_compatible: bool

    def to_json(self) -> dict:
        return {
            "source_path": str(self.source_path),
            "sections": self.sections,
            "header": self.header,
            "issues": self.issues,
            "ats_compatible": self.ats_compatible,
        }

    @classmethod
    def from_json(cls, data: dict) -> TemplateMap:
        if not isinstance(data, dict):
            raise ValueError("La cartographie du modèle doit être un objet JSON.")
        source = data.get("source_path")
        sections = data.get("sections")
        header = data.get("header")
        issues = data.get("issues")
        compatible = data.get("ats_compatible")
        if not isinstance(source, str) or not source:
            raise ValueError("Le chemin source de la cartographie est invalide.")
        if not isinstance(sections, list) or not all(isinstance(item, dict) for item in sections):
            raise ValueError("Les sections de la cartographie sont invalides.")
        if not isinstance(header, dict):
            raise ValueError("L'emplacement des coordonnées est invalide.")
        if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
            raise ValueError("Les problèmes de la cartographie sont invalides.")
        if not isinstance(compatible, bool):
            raise ValueError("L'indicateur ATS de la cartographie est invalide.")
        return cls(Path(source), sections, header, issues, compatible)


@dataclass(frozen=True)
class PdfTemplateMap:
    """Style reproductible extrait d'un PDF graphique, sans prétendre reconstruire sa géométrie."""

    source_path: Path
    font: str
    text: str
    muted: str
    accent: str
    rule: str
    issues: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"source_path": str(self.source_path), "font": self.font, "text": self.text,
                "muted": self.muted, "accent": self.accent, "rule": self.rule, "issues": self.issues}


@dataclass(frozen=True)
class AtsReport:
    readability: float
    keyword_coverage: dict
    issues: list[dict]
    extracted_text: str
    ready: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "readability": self.readability,
            "keyword_coverage": self.keyword_coverage,
            "issues": self.issues,
            "extracted_text": self.extracted_text,
            "ready": self.ready,
        }
