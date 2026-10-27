"""Règles métier déterministes pour prioriser et relancer les candidatures.

Ce module ne lit ni la base de données ni l'horloge système. Les appelants
doivent fournir l'instant courant, ce qui rend les décisions auditables et les
tests indépendants de la machine qui les exécute.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Paris"
KNOWN_APPLICATION_STATUSES = frozenset(
    {"consultee", "envoyee", "relance", "entretien_rh", "entretien_tech", "retenu", "refuse", "abandon"}
)
FOLLOW_UP_EXCLUDED_STATUSES = KNOWN_APPLICATION_STATUSES - {"envoyee"}
# Statuts qui impliquent que la candidature est partie : ils fixent la date d'envoi.
SENT_STATUSES = frozenset({"envoyee", "relance", "entretien_rh", "entretien_tech", "retenu", "refuse"})


class DeadlineState(StrEnum):
    """État d'une date limite par rapport à l'instant fourni."""

    ABSENTE = "absente"
    A_VENIR = "a_venir"
    AUJOURDHUI = "aujourdhui"
    EXPIREE = "expiree"


@dataclass(frozen=True)
class DeadlineDecision:
    """Résultat normalisé du contrôle d'une date limite."""

    state: DeadlineState
    due_at: datetime | None
    reason: str


@dataclass(frozen=True)
class PriorityDecision:
    """Clé de tri et explication de la priorité d'une offre."""

    key: tuple[int, int, int, float, int, float, float, int]
    reasons: tuple[str, ...]
    deadline: DeadlineDecision


@dataclass(frozen=True)
class FollowUpDecision:
    """Décision de relance calculée dans le fuseau métier."""

    due_at: datetime | None
    is_due: bool
    reason: str


def normalize_company(value: str) -> str:
    """Normalise un nom d'entreprise sans retirer sa forme juridique.

    NFKC rapproche notamment les variantes Unicode compatibles, ``casefold``
    assure une comparaison sans casse et tous les espaces sont réduits à un
    espace ASCII. Les suffixes tels que SAS, SARL ou GmbH sont conservés.
    """
    if not isinstance(value, str):
        raise TypeError("Le nom de l'entreprise doit être une chaîne de caractères.")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


_NATIONALITY = re.compile(
    r"nationalite\s+(?:francaise|europeenne|d[e'u]\s*(?:un\s+)?(?:pays|etat)s?\s+(?:membres?\s+)?de\s+l'?\s*"
    r"(?:union\s+europeenne|ue|eee|espace\s+economique\s+europeen))"
    r"|citoyennete\s+(?:francaise|europeenne)"
    r"|ressortissante?s?\s+(?:francaise?s?|europeen(?:ne)?s?|d(?:e\s+l'|'un\s+(?:pays|etat)\s+(?:membre\s+)?de\s+l')\s*"
    r"(?:union\s+europeenne|ue|eee))"
    r"|(?:french|european|eu)\s+(?:union\s+)?(?:nationality|citizenship|citizens?|nationals?|passport)"
    r"|(?:citizens?|nationals?)\s+of\s+(?:france|the\s+(?:european\s+union|eu))"
)
# Une alternative (titre de séjour, permis de travail…) dans la même phrase lève l'exigence.
_NATIONALITY_ALTERNATIVE = re.compile(
    r"titre\s+de\s+sejour|permis\s+de\s+travail|autorisation\s+de\s+travail|work\s+(?:permit|authori[sz]ation)"
    r"|right\s+to\s+work|visa|aucune\s+condition|sans\s+condition|not\s+required|n'est\s+pas\s+(?:requise|exigee)"
)


def nationality_requirement(text: str | None) -> str | None:
    """Phrase d'une offre qui exige la nationalité française ou européenne, sinon ``None``.

    Détection prudente : la phrase est écartée si elle propose une alternative (titre de séjour, permis de
    travail) ou écarte explicitement la condition.
    """
    if not text:
        return None
    for sentence in re.split(r"(?<=[.!?;:\n•])\s*", text):
        folded = unicodedata.normalize("NFKD", sentence.replace("’", "'")).encode("ascii", "ignore").decode().lower()
        if _NATIONALITY.search(folded) and not _NATIONALITY_ALTERNATIVE.search(folded):
            return " ".join(sentence.split())[:240]
    return None


def validate_timezone(name: str) -> ZoneInfo:
    """Retourne un fuseau IANA valide ou lève une erreur explicite."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Le fuseau horaire IANA est obligatoire.")
    if name != name.strip():
        raise ValueError("Le fuseau horaire IANA ne doit pas contenir d'espaces autour de son nom.")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Fuseau horaire IANA inconnu : {name!r}.") from exc


def validate_http_url(value: str) -> str:
    """Valide une URL HTTP(S) absolue dépourvue d'identifiants intégrés."""
    if not isinstance(value, str) or not value:
        raise ValueError("L'URL est obligatoire.")
    if value != value.strip() or re.search(r"[\x00-\x20\x7f]", value):
        raise ValueError("L'URL ne doit contenir ni espace ni caractère de contrôle.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("L'URL HTTP(S) est invalide.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise ValueError("L'URL doit être absolue et utiliser http ou https.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("L'URL ne doit pas contenir d'identifiant ni de mot de passe.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Le port de l'URL doit être compris entre 1 et 65535.")
    return value


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} doit contenir un fuseau horaire.")
    return value


def deadline_status(
    deadline: date | datetime | None,
    *,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> DeadlineDecision:
    """Évalue une échéance date seule ou horodatée.

    Une date seule reste valide jusqu'à la fin de cette journée dans le fuseau
    choisi. Un instant conserve son fuseau d'origine et est comparé précisément.
    """
    zone = validate_timezone(timezone_name)
    local_now = _aware(now, "L'instant courant").astimezone(zone)
    if deadline is None:
        return DeadlineDecision(DeadlineState.ABSENTE, None, "Aucune date limite indiquée.")

    if isinstance(deadline, datetime):
        due_at = _aware(deadline, "La date limite").astimezone(zone)
        if due_at < local_now:
            return DeadlineDecision(DeadlineState.EXPIREE, due_at, "La date limite précise est dépassée.")
        if due_at.date() == local_now.date():
            return DeadlineDecision(DeadlineState.AUJOURDHUI, due_at, "La date limite est aujourd'hui.")
        return DeadlineDecision(DeadlineState.A_VENIR, due_at, "La date limite précise est à venir.")

    if not isinstance(deadline, date):
        raise TypeError("La date limite doit être une date, un instant ou None.")
    due_at = datetime.combine(deadline, time.max, tzinfo=zone)
    if deadline < local_now.date():
        return DeadlineDecision(DeadlineState.EXPIREE, due_at, "La date limite calendaire est dépassée.")
    if deadline == local_now.date():
        return DeadlineDecision(DeadlineState.AUJOURDHUI, due_at, "La candidature reste possible aujourd'hui.")
    return DeadlineDecision(DeadlineState.A_VENIR, due_at, "La date limite calendaire est à venir.")


def is_urgent(decision: DeadlineDecision, *, now: datetime, timezone_name: str = DEFAULT_TIMEZONE,
              urgent_days: int = 7) -> bool:
    """Vrai si l'échéance tombe aujourd'hui ou dans les ``urgent_days`` jours calendaires locaux."""
    _check_days(urgent_days, "Le délai d'urgence")
    if decision.state is DeadlineState.AUJOURDHUI:
        return True
    if decision.state is not DeadlineState.A_VENIR or decision.due_at is None:
        return False
    zone = validate_timezone(timezone_name)
    today = _aware(now, "L'instant courant").astimezone(zone).date()
    return decision.due_at.astimezone(zone).date() <= today + timedelta(days=urgent_days)


def days_left(decision: DeadlineDecision, *, now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> int | None:
    """Jours calendaires locaux restants (0 aujourd'hui, négatif si dépassée, None sans échéance)."""
    if decision.due_at is None:
        return None
    zone = validate_timezone(timezone_name)
    today = _aware(now, "L'instant courant").astimezone(zone).date()
    return (decision.due_at.astimezone(zone).date() - today).days


def _check_days(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} doit être un entier positif ou nul.")


def priority(
    *,
    item_id: int,
    excluded: bool,
    deadline: date | datetime | None,
    favorite: bool,
    score: float | int | None,
    published_at: date | datetime | None,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
    urgent_days: int = 7,
) -> PriorityDecision:
    """Construit une clé croissante selon les règles de priorité produit.

    L'ordre est : non exclue, non expirée, échéance urgente (aujourd'hui ou
    dans ``urgent_days`` jours) la plus proche, favorite, meilleur score,
    publication la plus fraîche, puis identifiant stable. Une échéance lointaine
    ne donne aucun avantage : elle ne doit pas faire passer une offre moyenne
    devant une offre bien notée sans date limite.
    """
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 0:
        raise ValueError("L'identifiant de l'offre doit être un entier positif ou nul.")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(score) or not 0 <= score <= 100
    ):
        raise ValueError("Le score doit être un nombre fini compris entre 0 et 100.")

    zone = validate_timezone(timezone_name)
    local_now = _aware(now, "L'instant courant").astimezone(zone)
    deadline_decision = deadline_status(deadline, now=local_now, timezone_name=timezone_name)
    expired = deadline_decision.state is DeadlineState.EXPIREE
    urgent = is_urgent(deadline_decision, now=local_now, timezone_name=timezone_name, urgent_days=urgent_days)
    deadline_timestamp = deadline_decision.due_at.timestamp() if urgent else math.inf

    if published_at is None:
        published_timestamp = -math.inf
    elif isinstance(published_at, datetime):
        published_timestamp = _aware(published_at, "La date de publication").timestamp()
    elif isinstance(published_at, date):
        published_timestamp = datetime.combine(published_at, time.min, tzinfo=zone).timestamp()
    else:
        raise TypeError("La date de publication doit être une date, un instant ou None.")

    normalized_score = float(score) if score is not None else -1.0
    key = (
        int(excluded),
        int(expired),
        int(not urgent),
        deadline_timestamp,
        int(not favorite),
        -normalized_score,
        -published_timestamp,
        item_id,
    )
    reasons = (
        "Offre exclue." if excluded else "Offre autorisée.",
        deadline_decision.reason,
        "Date limite urgente." if urgent else "Pas de date limite urgente.",
        "Entreprise favorite." if favorite else "Entreprise non favorite.",
        f"Score : {normalized_score:g}/100." if score is not None else "Score absent.",
        "Date de publication absente." if published_at is None else "Fraîcheur prise en compte.",
        f"Identifiant de départage : {item_id}.",
    )
    return PriorityDecision(key, reasons, deadline_decision)


def follow_up_status(
    *,
    sent_at: datetime,
    status: str,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
    delay_days: int = 10,
) -> FollowUpDecision:
    """Calcule la relance après un nombre de jours calendaires locaux.

    L'heure locale d'envoi est conservée même lors d'un changement d'heure.
    Seul le statut ``envoyee`` est relançable ; une relance déjà faite ou une
    candidature ayant reçu une réponse ne génère plus de rappel automatique.
    """
    if status not in KNOWN_APPLICATION_STATUSES:
        raise ValueError(f"Statut de candidature inconnu : {status!r}.")
    if isinstance(delay_days, bool) or not isinstance(delay_days, int) or delay_days < 1:
        raise ValueError("Le délai de relance doit être un entier d'au moins un jour.")

    zone = validate_timezone(timezone_name)
    local_now = _aware(now, "L'instant courant").astimezone(zone)
    local_sent = _aware(sent_at, "La date d'envoi").astimezone(zone)
    if local_sent > local_now:
        raise ValueError("La date d'envoi ne peut pas être postérieure à l'instant courant.")
    if status in FOLLOW_UP_EXCLUDED_STATUSES:
        return FollowUpDecision(None, False, f"Le statut {status!r} exclut la relance automatique.")

    target_date = local_sent.date() + timedelta(days=delay_days)
    due_at = datetime.combine(target_date, local_sent.timetz())
    is_due = local_now >= due_at
    reason = "La relance est due." if is_due else f"La relance sera possible le {target_date.isoformat()}."
    return FollowUpDecision(due_at, is_due, reason)
