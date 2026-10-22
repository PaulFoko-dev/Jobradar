"""Pertinence d'une offre (avant toute analyse IA) et détection des doublons entre sites."""
import re
from datetime import date, timedelta

from .sources.common import matched_keywords, matches_any, norm, strip_contract_words

EARLY_CAREER = {"stage", "alternance", "professionnalisation", "vie"}
# Indices de contrat « débutant » dans un intitulé ou le champ contrat d'une source
TITLE_EARLY = re.compile(
    r"\b(stages?|stagiaires?|interns?|internships?|trainee|alternance|alternante?s?|apprentie?s?|apprentissage|"
    r"fin d etudes|pfe|v i e|vie|graduate program|professionnalisation)\b"
)
# Dans une description, on exige une formulation explicite (« stage » seul apparaît aussi dans des CDI)
DESC_EARLY = re.compile(
    r"\b(convention de stage|stage de fin d etudes|offre de stage|stage de \d|stage d une duree|duree du stage|"
    r"en stage|stagiaire|internship|en alternance|contrat d apprentissage|contrat de professionnalisation)\b"
)

UNKNOWN_CONTRACT = "contrat à vérifier"

STOP = {
    "stage", "stages", "stagiaire", "intern", "internship", "alternance", "alternant", "apprenti", "cdi", "cdd",
    "h", "f", "x", "e", "hf", "fh", "de", "du", "des", "la", "le", "les", "l", "d", "en", "et", "a", "au", "aux",
    "the", "of", "for", "and", "in", "pfe", "fin", "etudes", "mois", "an", "ans", "ou", "pour", "sur",
}
LEGAL = {"sa", "sas", "sasu", "sarl", "se", "group", "groupe", "france", "inc", "ltd", "gmbh", "the", "et", "and"}


def _domain_keywords(crit: dict) -> list[str]:
    titles = [strip_contract_words(t) for t in crit.get("titles", [])]
    return list(dict.fromkeys(crit.get("include_keywords", []) + titles))


def relevance(o: dict, crit: dict) -> str | None:
    """None si l'offre mérite d'être analysée, sinon la raison de son exclusion
    (UNKNOWN_CONTRACT : impossible de savoir le type de contrat sans lire la description)."""
    title, desc = o["title"], o.get("description") or ""
    if matches_any(title, crit.get("exclude_keywords", [])):
        return "mot exclu dans l'intitulé"
    days = int(crit.get("posted_within_days") or 30)
    try:
        if date.fromisoformat(str(o.get("date_posted"))[:10]) < date.today() - timedelta(days=int(days * 1.5)):
            return "trop ancienne"
    except ValueError:
        pass
    domain = _domain_keywords(crit)
    if domain and not matches_any(title, domain) and len(matched_keywords(desc, domain)) < 3:
        return "hors domaine"
    if set(crit.get("contract_types", [])) <= EARLY_CAREER:
        if TITLE_EARLY.search(norm(f"{title} {o.get('contract') or ''}")) or DESC_EARLY.search(norm(desc)):
            return None
        return UNKNOWN_CONTRACT if len(desc) < 200 else "autre type de contrat"
    return None


def priority(o: dict, crit: dict) -> float:
    """Plus la valeur est basse, plus l'offre est analysée tôt."""
    domain = _domain_keywords(crit)
    early = set(crit.get("contract_types", [])) <= EARLY_CAREER
    return -(
        (2 if early and TITLE_EARLY.search(norm(o["title"])) else 0)
        + (2 if matches_any(o["title"], domain) else 0)
        + min(len(matched_keywords(o.get("description") or "", domain)), 5) * 0.3
    )


# ---------------------------------------------------------------- doublons

def title_tokens(title: str) -> frozenset[str]:
    return frozenset(t for t in norm(title).split() if t not in STOP and not re.fullmatch(r"\d{4}", t))


def company_key(company: str | None) -> str:
    return " ".join(t for t in norm(company).split() if t not in LEGAL)


def dedup_key(o: dict) -> str:
    return f"{company_key(o.get('company'))}|{' '.join(sorted(title_tokens(o['title'])))}"


def similar(a: frozenset, b: frozenset) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 3:
        return False
    return len(a & b) / len(a | b) >= 0.8


class DedupIndex:
    """Retrouve une offre déjà connue de la même entreprise avec un intitulé quasi identique
    (« Stage Ingénieur(e) Réseau Sécurité - DSI » ≈ « Stage Ingénieur Réseau Sécurité - Dsi H/F »)."""

    # Une offre republiée par un cabinet ou un jobboard apparaît sous un autre nom d'entreprise :
    # au-delà de ce nombre de mots significatifs, un intitulé identique suffit.
    SAME_TITLE_MIN_TOKENS = 4

    def __init__(self):
        self._by_company: dict[str, list[tuple[frozenset, str, int | None]]] = {}
        self._by_title: dict[frozenset, tuple[str, int | None]] = {}

    def find(self, o: dict) -> tuple[str, int | None] | None:
        tokens = title_tokens(o["title"])
        for other, key, oid in self._by_company.get(company_key(o.get("company")), []):
            if similar(tokens, other):
                return key, oid
        if len(tokens) >= self.SAME_TITLE_MIN_TOKENS:
            return self._by_title.get(tokens)
        return None

    def add(self, o: dict, key: str, oid: int | None = None):
        tokens = title_tokens(o["title"])
        self._by_company.setdefault(company_key(o.get("company")), []).append((tokens, key, oid))
        if len(tokens) >= self.SAME_TITLE_MIN_TOKENS:
            self._by_title.setdefault(tokens, (key, oid))
