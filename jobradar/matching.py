"""Pertinence d'une offre (avant toute analyse IA) et détection des doublons entre sites."""
import re
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlsplit

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


def _published_day(o: dict) -> date | None:
    """Jour de publication (instant normalisé, sinon date de la source), à défaut jour où JobRadar l'a trouvée."""
    for value in (o.get("published_at_utc"), o.get("date_posted"), o.get("first_seen")):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def recency_adjustment(o: dict, *, today: date | None = None) -> tuple[int, str | None]:
    """Bonus/malus de score lié à l'âge réel de l'annonce, explicable dans l'interface.

    La récence compte, mais reste volontairement moins importante que l'adéquation métier : elle ne peut
    transformer à elle seule une offre hors sujet en bonne correspondance.
    """
    published = _published_day(o)
    if published is None:
        return 0, None
    age = max(0, ((today or date.today()) - published).days)
    if age <= 2:
        return 8, "Offre très récente (publiée il y a 2 jours ou moins) : +8 points"
    if age <= 7:
        return 5, "Offre récente (publiée il y a une semaine ou moins) : +5 points"
    if age <= 14:
        return 2, "Offre publiée il y a deux semaines ou moins : +2 points"
    if age <= 30:
        return 0, None
    if age <= 60:
        return -5, "Offre ancienne (publiée il y a plus de 30 jours) : -5 points"
    return -10, "Offre très ancienne (publiée il y a plus de 60 jours) : -10 points"


def scoring_order(o: dict, crit: dict) -> tuple[float, float]:
    """Ordre d'analyse IA : les offres les plus récentes d'abord, la pertinence départage un même jour."""
    day = _published_day(o)
    return (-day.toordinal() if day else 0.0, priority(o, crit))


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


# Sites qui republient les offres d'autres entreprises sous leur propre nom (Talents Handicap republie
# Sopra Steria ou Wavestone) : leur nom d'entreprise ne dit rien de l'employeur réel.
RELAY_COMPANIES = {"talents handicap", "handicap job", "hellowork", "jobijoba", "meteojob", "jooble", "talent com",
                   "optioncarriere", "jobteaser", "indeed", "linkedin"}


def is_relay(company: str | None) -> bool:
    return company_key(company) in RELAY_COMPANIES


# Premiers mots partagés par des entreprises sans lien (« Crédit Agricole » / « Crédit Mutuel »).
GENERIC_ROOTS = {"credit", "banque", "caisse", "societe", "compagnie", "entreprise", "agence", "cabinet", "institut",
                 "centre", "universite", "ecole", "ministere", "air", "laboratoire", "services", "solutions",
                 "consulting", "conseil", "digital", "tech", "global", "international", "le", "la", "les", "l"}


# Paramètres qui identifient l'offre elle-même (viewjob?jk=…, ?jobId=…) : conservés ; les autres (suivi) ignorés.
ID_PARAMS = re.compile(r"^(?:jk|jobid|job_id|job|id|gh_jid|reqid|requisitionid|postingid|vacancyid|offerid|ref)$", re.I)


def target_key(url: str | None) -> str:
    """Page normalisée : hôte sans www, chemin sans « / » final, paramètres d'identifiant seulement."""
    parts = urlsplit(url or "")
    host = parts.netloc.lower().removeprefix("www.")
    if not host or not parts.path.strip("/"):
        return ""
    ids = sorted(f"{name.lower()}={value}" for name, value in parse_qsl(parts.query) if ID_PARAMS.match(name))
    return f"{host}{parts.path.rstrip('/')}" + (f"?{'&'.join(ids)}" if ids else "")


def company_root(company: str | None) -> str:
    """Premier mot distinctif : « HGH Systèmes Infrarouges » et « HGH Infrared Systems » -> « hgh »."""
    words = company_key(company).split()
    return words[0] if words and len(words[0]) >= 3 and words[0] not in GENERIC_ROOTS else ""


def _city(o: dict) -> str:
    return norm((o.get("location") or "").split(",")[0])


class DedupIndex:
    """Retrouve une offre déjà connue de la même entreprise avec un intitulé quasi identique
    (« Stage Ingénieur(e) Réseau Sécurité - DSI » ≈ « Stage Ingénieur Réseau Sécurité - Dsi H/F »)."""

    # Une offre republiée par un cabinet ou un jobboard apparaît sous un autre nom d'entreprise :
    # au-delà de ce nombre de mots significatifs, un intitulé identique suffit.
    SAME_TITLE_MIN_TOKENS = 4
    # Un site relais (RELAY_COMPANIES) ne nomme pas l'employeur : un intitulé proche suffit dès 3 mots.
    RELAY_MIN_TOKENS = 3

    def __init__(self):
        self._by_company: dict[str, list[tuple[frozenset, str, int | None]]] = {}
        self._by_root: dict[str, list[tuple[frozenset, str, str, int | None]]] = {}
        self._by_title: dict[frozenset, tuple[str, int | None]] = {}
        self._all: list[tuple[frozenset, bool, str, int | None]] = []
        self._by_target: dict[str, tuple[str, int | None]] = {}

    @staticmethod
    def _targets(o: dict) -> set[str]:
        """Pages de l'offre et de son formulaire : deux annonces qui y mènent sont la même offre."""
        urls = [o.get("url"), o.get("apply_url")] + [link.get("url") for link in o.get("links") or []
                                                    if isinstance(link, dict)]
        return {key for key in map(target_key, urls) if key}

    def find(self, o: dict) -> tuple[str, int | None] | None:
        for target in self._targets(o):
            if target in self._by_target:
                return self._by_target[target]
        tokens = title_tokens(o["title"])
        for other, key, oid in self._by_company.get(company_key(o.get("company")), []):
            if similar(tokens, other):
                return key, oid
        # Variantes d'un même nom (langue, filiale) : même racine, même ville et intitulé identique.
        root, city = company_root(o.get("company")), _city(o)
        if root and city and len(tokens) >= 2:
            for other, other_city, key, oid in self._by_root.get(root, []):
                if tokens == other and city == other_city:
                    return key, oid
        if len(tokens) >= self.SAME_TITLE_MIN_TOKENS and tokens in self._by_title:
            return self._by_title[tokens]
        if len(tokens) >= self.RELAY_MIN_TOKENS:
            relay = is_relay(o.get("company"))
            for other, other_relay, key, oid in self._all:
                if (relay or other_relay) and len(other) >= self.RELAY_MIN_TOKENS and similar(tokens, other):
                    return key, oid
        return None

    def add(self, o: dict, key: str, oid: int | None = None):
        tokens = title_tokens(o["title"])
        self._by_company.setdefault(company_key(o.get("company")), []).append((tokens, key, oid))
        if root := company_root(o.get("company")):
            self._by_root.setdefault(root, []).append((tokens, _city(o), key, oid))
        if len(tokens) >= self.SAME_TITLE_MIN_TOKENS:
            self._by_title.setdefault(tokens, (key, oid))
        self._all.append((tokens, is_relay(o.get("company")), key, oid))
        for target in self._targets(o):
            self._by_target.setdefault(target, (key, oid))
