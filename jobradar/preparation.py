"""Préparations CV/lettre exécutées côté serveur, et chaîne d'auto-candidature.

Une préparation est une tâche durable (table ``preparation_jobs``) traitée par un fil unique : la page qui
l'a demandée peut être fermée, le résultat reste en base et dans la session d'adaptation, et la page le
reprend à sa réouverture. Une tâche interrompue par un arrêt repart au démarrage suivant.

L'auto-candidature réutilise la même file (origine ``auto``) : préparation sans questions, rendu, contrôle
ATS, « prêt à envoyer », puis envoi assisté sans relecture. Chaque étape garde ses propres garde-fous ;
la moindre difficulté arrête la chaîne pour cette offre et produit une notification, sans nouvel essai.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from datetime import time as day_time

from . import application_flow, archive, db, tailoring, workflow
from .config import settings

LOGGER = logging.getLogger(__name__)
Handler = Callable[[dict], None]
AUTO_MIN_READABILITY = 95
_SCHEDULE_LOCK = threading.Lock()


class AutoStop(RuntimeError):
    """Arrêt volontaire de la chaîne automatique (le message est montré à l'utilisateur)."""


# ---------------------------------------------------------------- lancement et lecture


def start_manual(application_id: int, params: dict) -> dict:
    """Préparation demandée depuis la page : répond tout de suite, le travail continue sans elle."""
    tailoring.check_preparation(application_id, **params)
    job = db.create_preparation_job(application_id, origin="manual", params=params)
    wake()
    return view(job)


def view(job: dict | None) -> dict | None:
    """Tâche telle que la page la reçoit ; en ``done``, le résultat reflète la session enregistrée."""
    if job is None:
        return None
    out = {key: job[key] for key in ("id", "application_id", "origin", "status", "step", "error",
                                     "created_at_utc", "started_at_utc", "finished_at_utc", "consumed_at_utc",
                                     "attempt_id")}
    for key in ("title", "company"):
        if key in job:
            out[key] = job[key]
    out["result"] = None
    if job["status"] == "done" and job["result"] is not None:
        out["result"] = dict(job["result"])
        try:
            session = tailoring.load_session(job["session_id"], job["application_id"])
            out["result"].update({key: session.get(key) for key in ("composition", "letter", "questions")})
        except (FileNotFoundError, tailoring.CompositionError):
            pass  # session effacée : le résultat d'origine reste affichable
    return out


def latest(application_id: int) -> dict | None:
    return view(db.latest_preparation_job(application_id))


# ---------------------------------------------------------------- exécution


def _run_manual(job: dict) -> None:
    result = tailoring.prepare(job["application_id"], **job["params"])
    db.update_preparation_job(job["id"], status="done", step="prepared", session_id=result["session"],
                              result=result)


def _ready_documents(job: dict, result: dict) -> dict[str, int]:
    """Rend, contrôle et marque prêts les documents ; arrête la chaîne si l'un n'est pas irréprochable."""
    db.update_preparation_job(job["id"], step="render")
    rendered = tailoring.render_documents(job["application_id"], result["composition"], result["letter"])
    ids = {}
    for document in rendered["documents"]:
        ats = document.get("ats") or {}
        if not ats.get("ready") or float(ats.get("readability", 0)) < AUTO_MIN_READABILITY:
            raise AutoStop(f"Le contrôle ATS du document « {document['kind']} » est sous "
                           f"{AUTO_MIN_READABILITY} % : relis-le avant tout envoi.")
        tailoring.mark_ready(document["id"])  # refuse aussi un document périmé
        ids[document["kind"]] = document["id"]
    return ids


def _run_auto(job: dict) -> None:
    application_id = job["application_id"]
    params = job["params"]
    db.update_preparation_job(job["id"], step="prepare")
    result = tailoring.prepare(application_id, kinds=params.get("kinds") or ["cv", "lettre"])
    db.update_preparation_job(job["id"], session_id=result["session"], result=result)
    # Les questions éventuelles sont ignorées : rien n'est ajouté au CV sans réponse de l'utilisateur.
    ids = _ready_documents(job, result)
    options = application_flow.options(application_id)
    if not options["assisted"]["allowed"] or not options["assisted"]["implemented"]:
        reason = options["assisted"]["reason"] or "Ce formulaire n'est pas pris en charge par l'envoi assisté."
        db.update_preparation_job(job["id"], status="done", step="manual")
        _notify(job, "auto_apply_manual", f"Documents prêts. {reason}")
        return
    db.update_preparation_job(job["id"], step="apply")
    started = application_flow.start(application_id, mode="assisted", cv_document_id=ids.get("cv"),
                                     letter_document_id=ids.get("lettre"), review_policy="never")
    db.update_preparation_job(job["id"], status="done", step="queued_for_submission",
                              attempt_id=started["attempt"]["id"])


def process(job: dict) -> None:
    """Traite une tâche réservée ; une erreur est enregistrée sur la tâche, jamais relancée en boucle."""
    try:
        (_run_auto if job["origin"] == "auto" else _run_manual)(job)
    except Exception as exc:  # IA, validation, rendu, ATS, garde-fous de l'envoi
        if not isinstance(exc, AutoStop | tailoring.CompositionError | db.Conflict):
            LOGGER.exception("Préparation %s : échec inattendu", job["id"])
        message = str(exc) or type(exc).__name__
        failed = db.update_preparation_job(job["id"], status="failed", error=message[:1000])
        if job["origin"] == "auto":
            _notify(failed, "auto_apply_failed", message)


def _notify(job: dict, kind: str, message: str) -> None:
    from . import notifications
    try:
        notifications.auto_apply(job, kind, message)
    except Exception:
        LOGGER.exception("Notification de l'auto-candidature %s impossible", job["id"])


# ---------------------------------------------------------------- auto-candidature


def _local_midnight_utc() -> datetime:
    zone = workflow.validate_timezone(settings().timezone)
    today = db.utcnow().astimezone(zone).date()
    return datetime.combine(today, day_time.min, tzinfo=zone).astimezone(timezone.utc)


def eligible_offers(search: dict) -> list[dict]:
    """Offres que l'auto-candidature peut prendre, de la mieux notée à la moins bien notée."""
    minimum = search["auto_apply"]["min_score"]
    return [offer for offer in db.list_offers(search["id"])
            if offer["score"] is not None and offer["score"] > minimum
            and offer.get("app_id") is None and not offer.get("auto_apply_at_utc")
            and offer["status"] != "ignored" and offer.get("in_zone") is not False
            and offer.get("company_rule") != "excluded" and offer.get("already_applied") is None
            and workflow.nationality_requirement(
                f"{offer.get('title') or ''} {offer.get('description') or ''}"
            ) is None
            and (offer.get("deadline") or {}).get("state") != workflow.DeadlineState.EXPIREE.value]


def _recent_item(raw: dict) -> dict:
    item = view(raw)
    attempt = db.get_application_attempt(raw["attempt_id"]) if raw.get("attempt_id") else None
    sent = application_flow.already_sent(raw["application_id"])
    item.update(attempt=attempt, proofs={}, supervision=None, sent_by_hand=sent and attempt is not None
                and attempt["status"] != "submitted", resumable=not sent and resumable(attempt))
    try:
        proof = archive.directory_of(raw["application_id"]) / "envoi"
    except db.NotFound:  # archive absente ou déplacée : l'état reste affichable, sans preuves
        return item
    item["proofs"] = {
        name: f"/api/applications/{raw['application_id']}/archive/file?path=envoi/{name}"
        for name in ("avant-envoi.png", "confirmation.png", "journal.md") if (proof / name).is_file()
    }
    try:
        supervision = json.loads((proof / "supervision.json").read_text(encoding="utf-8"))
        if attempt and supervision.get("attempt_id") == attempt["id"]:
            item["supervision"] = {key: supervision.get(key) for key in ("page_kind", "understanding", "at")}
    except (OSError, ValueError):
        pass
    return item


# Relancer ces arrêts referait la même chose : offre close, déjà envoyée, site à finir à la main
# ou validation explicite attendue (elle passe par « Valider l'envoi », pas par une reprise).
NOT_RESUMABLE = {"offer_removed", "already_confirmed", "manual_only_destination", "review_required"}


def resumable(attempt: dict | None) -> bool:
    return bool(attempt and attempt["status"] in {"needs_user", "failed"}
                and not attempt.get("submit_clicked_at_utc")
                and (attempt.get("intervention") or {}).get("kind") not in NOT_RESUMABLE)


def status(search_id: int) -> dict:
    search = db.get_search(search_id)
    if search is None:
        raise db.NotFound("Recherche introuvable.")
    today = db.auto_applied_offer_count(search_id, _local_midnight_utc())
    return {"settings": search["auto_apply"], "today": today,
            "remaining_today": max(0, search["auto_apply"]["daily_limit"] - today),
            "recent": [_recent_item(raw) for raw in db.recent_auto_preparations(search_id)]}


def overview() -> dict:
    """État de l'auto-candidature de toutes les recherches, pour la page dédiée."""
    midnight = _local_midnight_utc()
    searches, recent = [], []
    for search in db.list_searches():
        today = db.auto_applied_offer_count(search["id"], midnight)
        searches.append({"id": search["id"], "name": search.get("name") or f"Recherche {search['id']}",
                         "settings": search["auto_apply"], "today": today,
                         "remaining_today": max(0, search["auto_apply"]["daily_limit"] - today)})
        for raw in db.recent_auto_preparations(search["id"], limit=200):
            recent.append(_recent_item(raw) | {"search_id": search["id"], "search_name": searches[-1]["name"]})
    recent.sort(key=lambda item: item["id"], reverse=True)
    latest, seen = [], set()  # une ligne par candidature : la préparation la plus récente
    for item in recent:
        if item["application_id"] not in seen:
            seen.add(item["application_id"])
            latest.append(item)
    return {"searches": searches, "recent": latest}


def retry_all() -> dict:
    """Relance chaque tentative arrêtée qui peut l'être ; les autres restent à finir à la main."""
    retried, seen = [], set()
    for item in overview()["recent"]:
        attempt = item["attempt"]
        if not item["resumable"] or attempt["id"] in seen:
            continue
        seen.add(attempt["id"])
        try:
            application_flow.retry(attempt["id"])
            retried.append(attempt["id"])
        except db.Conflict:
            pass
    return {"retried": len(retried)}


def schedule(search_id: int) -> list[int]:
    """Confie à la file les offres éligibles de la recherche, dans la limite du plafond du jour."""
    with _SCHEDULE_LOCK:
        search = db.get_search(search_id)
        if search is None or not search["auto_apply"]["enabled"]:
            return []
        remaining = search["auto_apply"]["daily_limit"] - db.auto_applied_offer_count(search_id,
                                                                                       _local_midnight_utc())
        created = []
        for offer in eligible_offers(search)[:max(0, remaining)]:
            if not db.mark_offer_auto_applied(offer["id"]):
                continue
            application = archive.on_created(db.create_application({"offer_id": offer["id"]}))
            try:
                job = db.create_preparation_job(application["id"], origin="auto", params={"offer_id": offer["id"]})
            except db.Conflict:
                continue
            created.append(job["id"])
    if created:
        LOGGER.info("Auto-candidature : %s offre(s) confiée(s) à la file (recherche %s)", len(created), search_id)
        wake()
    return created


def configure(search_id: int, *, enabled: bool, min_score: int, daily_limit: int) -> dict:
    db.set_auto_apply(search_id, enabled=enabled, min_score=min_score, daily_limit=daily_limit)
    scheduled = schedule(search_id) if enabled else []
    return status(search_id) | {"scheduled": len(scheduled)}


# ---------------------------------------------------------------- file


class PreparationQueue:
    """Un seul fil : une préparation à la fois (l'IA et le rendu sont coûteux)."""

    def __init__(self, handler: Handler | None = None) -> None:
        self._handler = handler or process
        self._wake = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._work, name="jobradar-preparations", daemon=True)

    def start(self) -> int:
        recovered = db.recover_preparation_jobs()
        self._thread.start()
        self.wake()
        return recovered

    def wake(self) -> None:
        if not self._stop.is_set():
            self._wake.put(object())

    def stop(self, timeout_s: float = 5) -> None:
        """N'attend pas la fin d'une préparation : elle repartira au prochain démarrage."""
        self._stop.set()
        self._wake.put(None)
        self._thread.join(timeout_s)

    def wait_idle(self, timeout_s: float = 10) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._wake.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._wake.unfinished_tasks

    def _work(self) -> None:
        while not self._stop.is_set():
            token = self._wake.get()
            try:
                if token is None or self._stop.is_set():
                    continue
                while not self._stop.is_set():
                    job = db.claim_next_preparation_job()
                    if job is None:
                        break
                    try:
                        self._handler(job)
                    except Exception:
                        LOGGER.exception("Fil des préparations : échec pour la tâche %s", job["id"])
            finally:
                self._wake.task_done()


_QUEUE: PreparationQueue | None = None


def start_queue(handler: Handler | None = None) -> PreparationQueue:
    global _QUEUE
    stop_queue()
    _QUEUE = PreparationQueue(handler)
    _QUEUE.start()
    return _QUEUE


def stop_queue() -> None:
    global _QUEUE
    if _QUEUE is not None:
        _QUEUE.stop()
        _QUEUE = None


def wake() -> None:
    if _QUEUE is not None:
        _QUEUE.wake()


def run_pending() -> int:
    """Pour ``cli refresh`` sans serveur : traite ici les préparations en attente."""
    count = 0
    while (job := db.claim_next_preparation_job()) is not None:
        process(job)
        count += 1
    return count
