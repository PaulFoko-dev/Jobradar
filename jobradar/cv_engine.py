"""Classement explicable des éléments du profil pour une offre, calculé localement avant tout appel IA.

Chaque expérience, projet, certification, puce et compétence reçoit un score : termes de l'offre couverts
(pondérés : mots-clés du CV > intitulé et missions > description), technologies citées, récence et résultats
chiffrés. Le classement oriente la composition (``tailoring.build_prompt``) et sert ensuite à vérifier qu'aucun
élément très pertinent n'a été oublié. Aucun texte n'est inventé ici : le module ne fait que trier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .sources.common import norm

STOPWORDS = {
    "les", "des", "une", "aux", "avec", "pour", "dans", "sur", "par", "est", "sont", "vous", "votre", "nos", "notre",
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "stage", "stagiaire", "intern", "internship",
    "mois", "poste", "equipe", "team", "mission", "missions", "profil", "profile", "h", "f", "hf", "fh", "etc",
    "plus", "bonne", "bon", "tres", "ainsi", "afin", "cadre", "sein", "travail", "work", "entreprise", "company",
    "candidat", "candidate", "recherche", "recherchons", "rejoindre", "join", "cette", "ces", "leur", "leurs",
    "etre", "avoir", "faire", "experience", "experiences", "connaissance", "connaissances", "knowledge", "skills",
    "competences", "competence", "niveau", "level", "annee", "annees", "year", "years", "ingenieur", "engineer",
    # Mots de remplissage fréquents dans les offres, sans valeur pour choisir un projet.
    "qui", "que", "quoi", "selon", "mise", "place", "bonnes", "pratiques", "donnees", "projet", "projets",
    "creation", "faciliter", "gestion", "procedures", "suivi", "participer", "contribuer", "assurer", "realiser",
    "mettre", "oeuvre", "different", "differents", "differentes", "ensemble", "notamment", "egalement", "tout",
    "tous", "toutes", "chaque", "entre", "aussi", "comme", "lors", "nouvelles", "nouveaux", "nouveau",
}
WEIGHTS = {"keyword": 3.0, "title": 2.0, "mission": 2.0, "description": 1.0}
TOP_ITEMS = 3


@dataclass
class Ranked:
    id: str
    kind: str
    label: str
    score: float
    matched: list[str] = field(default_factory=list)
    bullets: list[tuple[str, float]] = field(default_factory=list)  # (id de puce, score), meilleures d'abord

    def reason(self) -> str:
        return ", ".join(self.matched[:6]) or "peu de termes de l'offre"


def _tokens(text: str) -> list[str]:
    return [word for word in norm(text).split() if len(word) >= 3 and word not in STOPWORDS and not word.isdigit()]


def offer_terms(offer: dict) -> dict[str, float]:
    """Termes pondérés de l'offre : mots-clés exacts (expressions conservées) et mots significatifs."""
    analysis = offer.get("analysis") if isinstance(offer.get("analysis"), dict) else {}
    terms: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        key = norm(term)
        if key and key not in STOPWORDS and len(key) >= 2:
            terms[key] = max(terms.get(key, 0.0), weight)

    for keyword in analysis.get("cv_keywords") or []:
        add(str(keyword), WEIGHTS["keyword"])
        for word in _tokens(str(keyword)):
            add(word, WEIGHTS["keyword"] - 1)
    for word in _tokens(str(offer.get("title") or "")):
        add(word, WEIGHTS["title"])
    for mission in analysis.get("missions") or []:
        for word in _tokens(str(mission)):
            add(word, WEIGHTS["mission"])
    for word in _tokens(str(offer.get("description") or "")):
        add(word, WEIGHTS["description"])
    return terms


def _matches(text: str, terms: dict[str, float]) -> list[str]:
    folded = f" {norm(text)} "
    return [term for term in terms if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", folded)]


def _score_text(text: str, terms: dict[str, float]) -> tuple[float, list[str]]:
    matched = _matches(text, terms)
    # Un même mot présent dans une expression déjà comptée (« splunk » dans « splunk siem ») ne compte qu'à moitié.
    score = sum(terms[term] * (0.5 if any(term != other and term in other.split() for other in matched) else 1.0)
                for term in matched)
    return score, sorted(matched, key=lambda term: -terms[term])


def _year(value: object) -> int | None:
    match = re.match(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def _recency(item: dict, today: date) -> float:
    end = _year(item.get("end")) if item.get("end") else today.year  # sans fin : en cours
    if end is None:
        return 0.0
    return 1.0 if today.year - end <= 1 else 0.5 if today.year - end <= 3 else 0.0


def _bullet_text(bullet: dict) -> str:
    return str(bullet.get("text") or "")


def _rank_item(item: dict, kind: str, terms: dict[str, float], today: date) -> Ranked:
    head = " ".join(str(item.get(key) or "") for key in ("title", "organization", "context", "setting", "name",
                                                         "issuer", "degree", "school", "details"))
    technologies = " ".join(str(value) for value in item.get("technologies") or [])
    bullets = [bullet for bullet in item.get("bullets") or [] if isinstance(bullet, dict)]
    head_score, head_matched = _score_text(f"{head} {technologies}", terms)
    ranked_bullets = []
    matched = list(head_matched)
    for bullet in bullets:
        score, found = _score_text(_bullet_text(bullet), terms)
        score += 0.5 if re.search(r"\d", _bullet_text(bullet)) else 0.0  # résultat chiffré
        ranked_bullets.append((str(bullet.get("id") or ""), score))
        matched += [term for term in found if term not in matched]
    bullet_score = sum(sorted((score for _, score in ranked_bullets), reverse=True)[:3])
    score = head_score + 0.7 * bullet_score + _recency(item, today)
    ranked_bullets.sort(key=lambda pair: -pair[1])
    label = str(item.get("title") or item.get("name") or item.get("degree") or item.get("id") or "")
    return Ranked(str(item.get("id") or ""), kind, label, round(score, 2), matched, ranked_bullets)


def rank(profile_data: dict, offer: dict, *, today: date | None = None) -> dict[str, list[Ranked]]:
    """Expériences, projets, certifications et compétences du profil, des plus pertinents aux moins pertinents."""
    terms, today = offer_terms(offer), today or date.today()
    out: dict[str, list[Ranked]] = {}
    for kind in ("experiences", "projects", "certifications"):
        items = [item for item in profile_data.get(kind) or [] if isinstance(item, dict) and item.get("id")]
        out[kind] = sorted((_rank_item(item, kind, terms, today) for item in items), key=lambda r: -r.score)
    skills = []
    for skill in profile_data.get("skills") or []:
        if not isinstance(skill, dict) or not skill.get("name"):
            continue
        score, matched = _score_text(str(skill["name"]), terms)
        skills.append(Ranked(str(skill.get("id") or skill["name"]), "skills", str(skill["name"]), round(score, 2),
                             matched))
    out["skills"] = sorted(skills, key=lambda r: (-r.score, r.label))
    return out


def prompt_section(ranking: dict[str, list[Ranked]]) -> str:
    """Consigne de sélection pour l'IA : classement local, raisons et puces à mettre en avant."""
    lines = ["CLASSEMENT DE PERTINENCE (calculé localement d'après l'offre ; suis-le pour sélectionner et ordonner) :"]
    titles = {"experiences": "Expériences", "projects": "Projets", "certifications": "Certifications"}
    for kind, title in titles.items():
        ranked = ranking.get(kind) or []
        if not ranked:
            continue
        lines.append(f"{title} :")
        for position, item in enumerate(ranked, 1):
            best = ", ".join(bullet for bullet, score in item.bullets[:3] if score > 0)
            lines.append(f"  {position}. {item.id} — score {item.score} — termes : {item.reason()}"
                         + (f" — puces à mettre en avant : {best}" if best else ""))
    relevant = [item.label for item in ranking.get("skills") or [] if item.score > 0][:15]
    if relevant:
        lines.append("Compétences du profil demandées par l'offre (à placer en tête de leur groupe) : "
                     + ", ".join(relevant))
    lines.append(f"Règles : garde les {TOP_ITEMS} expériences et projets les mieux classés sauf raison forte ; "
                 "place les éléments et les puces dans l'ordre du classement ; un élément à score faible peut "
                 "être retiré pour tenir sur une page ; reformule les puces retenues avec le vocabulaire exact de "
                 "l'offre, sans rien ajouter qui ne figure dans l'élément.")
    return "\n".join(lines)


def composition_refs(composition: dict | None) -> set[str]:
    refs: set[str] = set()
    for section in (composition or {}).get("sections") or []:
        for item in section.get("items") or []:
            if isinstance(item, dict) and item.get("ref"):
                refs.add(str(item["ref"]))
    return refs


def missed_top_items(composition: dict | None, ranking: dict[str, list[Ranked]]) -> list[Ranked]:
    """Éléments très pertinents (top du classement, score positif) absents du CV composé."""
    used = composition_refs(composition)
    missed = []
    for kind in ("experiences", "projects"):
        for item in (ranking.get(kind) or [])[:TOP_ITEMS]:
            if item.score > 0 and item.id not in used:
                missed.append(item)
    return missed


def explanation(composition: dict | None, ranking: dict[str, list[Ranked]]) -> list[dict]:
    """« Pourquoi ce contenu » : chaque élément avec son rang, son score, ses raisons et s'il a été retenu."""
    used = composition_refs(composition)
    rows = []
    for kind in ("experiences", "projects", "certifications"):
        for position, item in enumerate(ranking.get(kind) or [], 1):
            rows.append({"id": item.id, "kind": kind, "label": item.label, "rank": position, "score": item.score,
                         "terms": item.matched[:8], "kept": item.id in used})
    return rows
