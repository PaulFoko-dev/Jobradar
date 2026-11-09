"""Orchestration sûre d'une candidature manuelle ou assistée.

Ce module ne pilote pas encore le navigateur : il fige d'abord le contrat commun
aux futurs adaptateurs (URL, documents exacts, préférences et interdictions).
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import application_adapters, config, db, matching, profile, tailoring, workflow

LOGGER = logging.getLogger(__name__)
MANUAL_ONLY_HOSTS = {"indeed", "linkedin"}
ASSISTED_PROVIDERS = {"greenhouse", "lever", "smartrecruiters", "workday", "welcometothejungle", "generic"}
REVIEW_POLICIES = {"first_five", "always", "never"}
DEFAULT_PREFERENCES = {"mode": "manual", "include_letter": True, "review_policy": "first_five"}


def provider_for(url: str) -> str:
    """Classe le formulaire depuis son hôte, sans effectuer de requête réseau."""
    workflow.validate_http_url(url)
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    if host == "indeed.com" or host.endswith(".indeed.com") or host.endswith(".indeed.fr"):
        return "indeed"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "myworkdayjobs.com" in host or host.endswith("workday.com"):
        return "workday"
    if host == "welcometothejungle.com" or host.endswith(".welcometothejungle.com"):
        return "welcometothejungle"
    return "generic"


def _preferences() -> dict:
    raw = config.load_config().get("application", {})
    if not isinstance(raw, dict):
        return dict(DEFAULT_PREFERENCES)
    mode = raw.get("mode") if raw.get("mode") in {"manual", "assisted"} else "manual"
    review_policy = raw.get("review_policy")
    if review_policy not in REVIEW_POLICIES:
        review_policy = "first_five"
    return {"mode": mode, "include_letter": raw.get("include_letter", True) is True,
            "review_policy": review_policy}


def save_preferences(*, mode: str, include_letter: bool, review_policy: str = "first_five") -> dict:
    if mode not in {"manual", "assisted"}:
        raise ValueError("Mode de candidature invalide.")
    if review_policy not in REVIEW_POLICIES:
        raise ValueError("Politique de validation avant envoi invalide.")
    value = {"mode": mode, "include_letter": bool(include_letter), "review_policy": review_policy}
    config.save_config_section("application", value)
    return value


def options(application_id: int) -> dict:
    application = db.get_application(application_id)
    url = str(application.get("url") or "").strip()
    if not url:
        raise ValueError("Cette candidature n'a pas de lien vers un formulaire.")
    provider = provider_for(url)
    documents = db.list_documents(application_id)
    candidates = [item for item in documents if item["status"] == "pret" and item.get("ats", {}).get("ready") is True]
    # Défense supplémentaire pour les anciennes bases qui pourraient encore contenir plusieurs
    # versions « prêtes » : la liste est triée par version décroissante, une seule par type sort.
    active = {}
    for item in candidates:
        active.setdefault(item["kind"], item)
    candidates = list(active.values())
    ready, stale = [], []
    for item in candidates:
        check = tailoring.current_check(item["id"])
        if check["ok"]:
            ready.append(item)
        else:
            stale.append({"id": item["id"], "kind": item["kind"], "version": item["version"],
                          "message": check["message"]})
    latest = {item["kind"]: item["id"] for item in ready}
    assisted_allowed = provider not in MANUAL_ONLY_HOSTS
    reason = None if assisted_allowed else "LinkedIn et Indeed sont disponibles uniquement en mode manuel."
    return {
        "application": {key: application.get(key) for key in ("id", "title", "company", "url", "status")},
        "provider": provider,
        "assisted": {"allowed": assisted_allowed, "implemented": provider in application_adapters.SUPPORTED,
                     "reason": reason},
        "documents": ready,
        "stale_documents": stale,
        "attempts": db.list_application_attempts(application_id),
        "defaults": _preferences() | {"cv_document_id": latest.get("cv"),
                                      "letter_document_id": latest.get("lettre")},
    }


def _manual_help(application_id: int, attempt: dict) -> dict:
    candidate = profile.load_profile()
    selected = [value for value in (attempt["cv_document_id"], attempt["letter_document_id"]) if value]
    documents = []
    for document_id in selected:
        document = db.get_document(document_id)
        pdf = next((item for item in document["files"] if item["name"].lower().endswith(".pdf")), None)
        selected_file = pdf or (document["files"][0] if document["files"] else None)
        documents.append({
            "id": document["id"], "kind": document["kind"], "version": document["version"],
            "files": ([{"name": selected_file["name"],
                        "url": f"/api/documents/{document['id']}/file?name={quote(selected_file['name'], safe='')}"}]
                      if selected_file else []),
        })
    return {
        "attempt": attempt,
        "identity": candidate["identity"],
        "standard_answers": candidate["standard_answers"],
        "documents": documents,
        "target_url": attempt["target_url"],
    }


ACTIVE_ATTEMPTS = {"manual_open", "queued", "running", "needs_user"}


def _sent(application: dict) -> bool:
    return bool(application.get("applied_at_utc")) or application.get("status") not in {"consultee", "abandon"}


def twin_of(application: dict, others: list[dict]) -> dict | None:
    """Candidature équivalente parmi ``others`` : même page, ou même entreprise (variantes, sites relais compris)
    et même intitulé, selon les règles de dédoublonnage des offres."""
    index = matching.DedupIndex()
    by_id = {}
    for other in others:
        if other["id"] == application["id"]:
            continue
        index.add(other, str(other["id"]), other["id"])
        by_id[other["id"]] = other
    found = index.find(application)
    return by_id.get(found[1]) if found else None


def already_applied_elsewhere(application_id: int) -> dict | None:
    """Candidature équivalente déjà envoyée (autre annonce de la même offre) : un second envoi serait un doublon."""
    applications = db.list_applications()
    current = next((item for item in applications if item["id"] == application_id), None)
    if current is None:
        return None
    sent = [item for item in applications if item["id"] != application_id and _sent(item)]
    return same_target_sent(application_id, current.get("url")) or twin_of(current, sent)


def merge_duplicate_applications() -> list[tuple[int, int]]:
    """Fusionne les candidatures en double : celle qui n'est pas partie passe en « Abandon », avec une note qui renvoie
    vers l'autre (réversible). Ordre de préférence : déjà envoyée, annonce de l'entreprise plutôt que d'un site relais,
    meilleur score, plus ancienne. Deux candidatures déjà envoyées ne sont jamais modifiées."""
    applications = [item for item in db.list_applications() if item.get("status") != "abandon"]
    attempts = {item["id"]: db.list_application_attempts(item["id"]) for item in applications}
    # Protègent une candidature : un robot en train de travailler, un panneau manuel ouvert, ou un clic d'envoi.
    # Une tentative bloquée (« needs_user », « queued ») sur un doublon est annulée : la reprendre enverrait un doublon.
    active = {app_id for app_id, items in attempts.items() for attempt in items
              if attempt["status"] in {"running", "manual_open"} or attempt.get("submit_clicked_at_utc")}
    ordered = sorted(applications, key=lambda item: (not _sent(item), matching.is_relay(item.get("company")),
                                                     -(item.get("score") or 0), item["id"]))
    kept: list[dict] = []
    merged = []
    for item in ordered:
        twin = twin_of(item, kept)
        if twin is None or _sent(item) or item["id"] in active:
            kept.append(item)
            continue
        note = (f"Doublon de la candidature n°{twin['id']} « {twin.get('title') or ''} » ({twin.get('company') or ''}) : "
                "même offre publiée sous une autre annonce. Mise en « Abandon » automatiquement pour éviter un double "
                "envoi ; remets-la dans une autre colonne si c'est une erreur.")
        for attempt in attempts.get(item["id"], []):
            if attempt["status"] in {"needs_user", "queued"}:
                db.transition_application_attempt(
                    attempt["id"], expected={"needs_user", "queued"}, status="cancelled",
                    intervention={"kind": "duplicate", "action": "skip",
                                  "message": f"Doublon de la candidature n°{twin['id']} : tentative annulée."})
        current = (item.get("notes") or "").strip()
        db.update_application(item["id"], {"status": "abandon",
                                           "notes": f"{current}\n\n{note}".strip() if current else note})
        merged.append((item["id"], twin["id"]))
    return merged


def backfill_removed_offers() -> int:
    """Retraits d'annonce constatés par le robot avant que l'état soit enregistré : candidature et offre marquées
    « annonce retirée » (masquée des résultats), tentative restée bloquée close (plus rien à faire)."""
    count = 0
    for application in db.list_applications():
        for attempt in db.list_application_attempts(application["id"]):
            if (attempt.get("intervention") or {}).get("kind") != "offer_removed":
                continue
            if application.get("offer_online") is not False and application.get("offer_online") != 0:
                db.set_archive_fields(application["id"], {
                    "offer_online": 0, "offer_checked_at_utc": attempt.get("updated_at_utc") or db.now()})
                count += 1
            if attempt["status"] in {"needs_user", "queued"} and not attempt.get("submit_clicked_at_utc"):
                db.transition_application_attempt(attempt["id"], expected={"needs_user", "queued"}, status="cancelled",
                                                  intervention=attempt["intervention"] | {"action": "skip"})
            break
    return count


def same_target_sent(application_id: int, url: str | None) -> dict | None:
    """Autre candidature déjà envoyée vers la même page (annonces republiées sous un autre titre)."""
    key = matching.target_key(url)
    if not key:
        return None
    for other in db.list_applications():
        if other["id"] == application_id:
            continue
        targets = {matching.target_key(other.get("url"))} | {
            matching.target_key(attempt.get("target_url")) for attempt in db.list_application_attempts(other["id"])}
        sent = other.get("applied_at_utc") or other.get("status") not in {"consultee", "abandon"}
        if key in targets and sent:
            return other
    return None


def start(application_id: int, *, mode: str, cv_document_id: int | None,
          letter_document_id: int | None, remember: bool = False,
          review_policy: str = "first_five") -> dict:
    current = options(application_id)
    active = next((attempt for attempt in current["attempts"]
                   if attempt["status"] in {"manual_open", "queued", "running", "needs_user"}), None)
    if active:
        raise db.Conflict("Une session de candidature est déjà ouverte pour cette offre : reprends-la ou annule-la.")
    if any(attempt["status"] == "submitted" for attempt in current["attempts"]):
        raise db.Conflict("Une candidature a déjà été envoyée pour cette offre.")
    twin = already_applied_elsewhere(application_id)
    if twin:
        raise db.Conflict(f"Tu as déjà postulé à cette même offre via « {twin['title']} » ({twin.get('company') or ''}) : "
                          "les deux annonces mènent au même formulaire. Rien n'a été envoyé.")
    if mode not in {"manual", "assisted"}:
        raise ValueError("Mode de candidature invalide.")
    if review_policy not in REVIEW_POLICIES:
        raise ValueError("Politique de validation avant envoi invalide.")
    if mode == "assisted":
        if not current["assisted"]["allowed"]:
            raise db.Conflict(current["assisted"]["reason"])
    stale = {item["id"]: item["message"] for item in current["stale_documents"]}
    for document_id in (cv_document_id, letter_document_id):
        if document_id in stale:
            raise db.Conflict(stale[document_id])
    attempt = db.create_application_attempt(
        application_id, mode=mode, status="queued" if mode == "assisted" else "manual_open",
        cv_document_id=cv_document_id,
        letter_document_id=letter_document_id, target_url=current["application"]["url"],
        provider=current["provider"], review_policy=review_policy,
    )
    if remember:
        save_preferences(mode=mode, include_letter=letter_document_id is not None,
                         review_policy=review_policy)
    if mode == "assisted":
        from . import application_queue
        application_queue.wake()
        return {"attempt": attempt}
    return _manual_help(application_id, attempt)


def cancel(attempt_id: int) -> dict:
    return db.transition_application_attempt(
        attempt_id, expected={"queued", "needs_user", "failed"}, status="cancelled"
    )


def already_sent(application_id: int) -> bool:
    """Candidature envoyée (par le robot ou à la main) ou abandonnée : plus rien à envoyer."""
    status = db.get_application(application_id).get("status")
    return status in workflow.SENT_STATUSES or status == "abandon"


def retry(attempt_id: int) -> dict:
    if already_sent(db.get_application_attempt(attempt_id)["application_id"]):
        raise db.Conflict("Cette candidature est déjà envoyée ou abandonnée : elle ne sera pas relancée.")
    resume_url = (db.get_application_attempt(attempt_id).get("intervention") or {}).get("resume_url")
    attempt = db.transition_application_attempt(
        attempt_id, expected={"needs_user", "failed"}, status="queued",
        intervention={"kind": "resume", "resume_url": resume_url} if resume_url else None,
    )
    from . import application_queue
    application_queue.wake()
    return attempt


def switch_to_manual(attempt_id: int) -> dict:
    """Ferme proprement une tentative assistée et ouvre le panneau manuel avec les mêmes documents."""
    current = db.get_application_attempt(attempt_id)
    if current["mode"] != "assisted" or current["status"] not in {"needs_user", "failed"}:
        raise db.Conflict("Cette session ne peut pas basculer en mode manuel.")
    db.transition_application_attempt(attempt_id, expected={current["status"]}, status="cancelled")
    manual = db.create_application_attempt(
        current["application_id"], mode="manual", status="manual_open",
        cv_document_id=current["cv_document_id"], letter_document_id=current["letter_document_id"],
        target_url=current["target_url"], provider=current["provider"],
        review_policy=current.get("review_policy") or "first_five",
    )
    return _manual_help(current["application_id"], manual)


def approve(attempt_id: int) -> dict:
    current = db.get_application_attempt(attempt_id)
    if current["status"] != "needs_user" or (current.get("intervention") or {}).get("kind") != "review_required":
        raise db.Conflict("Cette session n'attend pas une validation avant envoi.")
    attempt = db.transition_application_attempt(
        attempt_id, expected={"needs_user"}, status="queued", intervention={"kind": "review_approved"}
    )
    from . import application_queue
    application_queue.wake()
    return attempt


def answer_fields(attempt_id: int, answers: list[dict]) -> dict:
    """Valide les valeurs par rapport aux champs détectés avant toute reprise du navigateur."""
    current = db.get_application_attempt(attempt_id)
    fields = {field.get("selector"): field for field in (current.get("intervention") or {}).get("fields") or []}
    cleaned = []
    for answer in answers:
        selector = answer.get("selector")
        field = fields.get(selector)
        value = answer.get("value")
        if field is None:
            raise ValueError("La réponse ne correspond pas à un champ demandé.")
        if field.get("input_kind") == "checkbox":
            if not isinstance(value, bool):
                raise ValueError(f"La réponse « {field.get('label')} » doit être oui ou non.")
        else:
            value = str(value or "").strip()
            if not value or len(value) > 2_000:
                raise ValueError(f"La réponse « {field.get('label')} » est vide ou trop longue.")
            options = field.get("options") or []
            if field.get("input_kind") in {"select", "radio", "listbox"} and options and value not in options:
                raise ValueError(f"La réponse « {field.get('label')} » n'est pas proposée par le formulaire.")
        cleaned.append({"selector": selector, "label": str(field.get("label") or "")[:300],
                        "kind": field.get("input_kind") or "text", "value": value,
                        "sensitive": field.get("kind") == "sensitive", "source": "utilisateur"})
    profile.remember_form_answers(cleaned)
    attempt = db.answer_application_attempt(attempt_id, cleaned)
    from . import application_queue
    application_queue.wake()
    return attempt


def confirm_manual_sent(attempt_id: int) -> dict:
    attempt = db.get_application_attempt(attempt_id)
    if attempt["mode"] != "manual":
        raise db.Conflict("Cette session n'est pas une candidature manuelle.")
    # Tout valider avant la première copie évite un envoi partiel si un document
    # a été modifié depuis l'ouverture du formulaire.
    documents = [db.get_document(document_id) for document_id in
                 (attempt["cv_document_id"], attempt["letter_document_id"]) if document_id]
    if any(document["status"] not in {"pret", "envoye"} for document in documents):
        raise db.Conflict("Un document sélectionné n'est plus prêt à envoyer.")
    # Les copies exactes vont dans documents/envoye avant le changement de statut.
    for document in documents:
        if document["status"] == "pret":
            tailoring.mark_sent(document["id"])
    submitted = db.submit_application_attempt(attempt_id)
    db.update_application(attempt["application_id"], {"status": "envoyee"})
    from . import archive  # évite une dépendance circulaire au chargement
    archive.refresh(attempt["application_id"])
    return submitted


def finalize_assisted_submission(attempt_id: int) -> dict:
    """Fige d'abord la session comme envoyée, puis met à jour documents, candidature, archive et notification.

    L'ordre est volontaire : une fois la candidature partie, aucune erreur des étapes suivantes
    ne doit pouvoir la faire passer pour un échec, ce qui inviterait à la renvoyer.
    """
    attempt = db.get_application_attempt(attempt_id)
    if attempt["status"] != "submitted":
        attempt = db.transition_application_attempt(attempt_id, expected={"running", "needs_user"},
                                                    status="submitted")
    from . import archive, notifications  # évite une dépendance circulaire au chargement

    def mark_documents() -> None:
        for document_id in (attempt["cv_document_id"], attempt["letter_document_id"]):
            if document_id and db.get_document(document_id)["status"] == "pret":
                tailoring.mark_sent(document_id)

    def mark_application() -> None:
        if db.get_application(attempt["application_id"]).get("status") in {"consultee", "a_postuler"}:
            db.update_application(attempt["application_id"], {"status": "envoyee"})

    steps = (("documents", mark_documents), ("statut", mark_application),
             ("archive", lambda: archive.refresh(attempt["application_id"])),
             ("notification", lambda: notifications.application_submitted(attempt)))
    for name, step in steps:
        try:
            step()
        except Exception:
            LOGGER.exception("Candidature %s envoyée : étape « %s » à refaire", attempt_id, name)
    return attempt


def confirm_assisted_sent(attempt_id: int) -> dict:
    """L'utilisateur a vérifié sur le site que la candidature assistée est bien partie."""
    attempt = db.get_application_attempt(attempt_id)
    if attempt["mode"] != "assisted" or attempt["status"] != "needs_user" or not attempt.get("submit_clicked_at_utc"):
        raise db.Conflict("Cette session n'attend pas de confirmation d'envoi.")
    return finalize_assisted_submission(attempt_id)


def selected_file_paths(attempt_id: int) -> list[Path]:
    """Chemins internes réservés aux futurs adaptateurs ; jamais fournis par le client."""
    attempt = db.get_application_attempt(attempt_id)
    paths = []
    for document_id in (attempt["cv_document_id"], attempt["letter_document_id"]):
        if document_id:
            document = db.get_document(document_id)
            pdf = next((item for item in document["files"] if item["name"].lower().endswith(".pdf")), None)
            selected = pdf or (document["files"][0] if document["files"] else None)
            if selected:
                paths.append(tailoring.document_file(document_id, selected["name"]))
    return paths
