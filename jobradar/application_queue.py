"""File locale, durable et strictement séquentielle des candidatures assistées."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

from . import db

LOGGER = logging.getLogger(__name__)
Handler = Callable[[dict], None]
BROWSER_RETRY_DELAYS_S = (60, 180, 300)


def _may_retry_browser(attempt: dict) -> bool:
    previous = attempt.get("intervention") or {}
    return not attempt.get("submit_clicked_at_utc") and previous.get("browser_retry_count", 0) < len(BROWSER_RETRY_DELAYS_S)


def _retry_browser_later(attempt_id: int, retry_count: int) -> None:
    try:
        db.transition_application_attempt(attempt_id, expected={"needs_user"}, status="queued",
                                          intervention={"kind": "browser_retry_started",
                                                        "browser_retry_count": retry_count})
    except (db.Conflict, db.NotFound):
        return
    wake()


def _process(attempt: dict) -> None:
    from . import application_browser, notifications
    try:
        application_browser.process(attempt)
    except application_browser.BrowserUnavailable as exc:
        current = db.get_application_attempt(attempt["id"])
        if _may_retry_browser(current):
            retry_count = int((current.get("intervention") or {}).get("browser_retry_count", 0)) + 1
            db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                              intervention={"kind": "browser_retry_scheduled",
                                                            "browser_retry_count": retry_count,
                                                            "message": f"{exc} Reprise {retry_count}/3 programmée."})
            timer = threading.Timer(BROWSER_RETRY_DELAYS_S[retry_count - 1], _retry_browser_later,
                                    [attempt["id"], retry_count])
            timer.daemon = True
            timer.start()
            return
        intervention = {"kind": "browser_unavailable", "message": str(exc), "action": "manual_fallback"}
        updated = db.transition_application_attempt(
            attempt["id"], expected={"running"}, status="needs_user",
            intervention=intervention,
        )
        notifications.application_intervention(updated, intervention)


class ApplicationQueue:
    """Un seul fil de travail : aucune candidature parallèle, même si plusieurs sont ajoutées."""

    def __init__(self, handler: Handler | None = None) -> None:
        self._handler = handler or _process
        self._wake = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._work, name="jobradar-applications", daemon=True)

    def start(self) -> int:
        recovered = db.recover_application_attempts()
        self._thread.start()
        self.wake()
        return recovered

    def wake(self) -> None:
        if not self._stop.is_set():
            self._wake.put(object())

    def stop(self, timeout_s: float = 5) -> None:
        self._stop.set()
        self._wake.put(None)
        self._thread.join(timeout_s)

    def wait_idle(self, timeout_s: float = 10) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._wake.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._wake.unfinished_tasks

    def _work(self) -> None:
        try:
            while not self._stop.is_set():
                token = self._wake.get()
                try:
                    if token is None or self._stop.is_set():
                        continue
                    while not self._stop.is_set():
                        attempt = db.claim_next_application_attempt()
                        if attempt is None:
                            break
                        try:
                            self._handler(attempt)
                        except Exception as exc:
                            self._handle_failure(attempt, exc)
                finally:
                    self._wake.task_done()
        finally:
            from . import application_browser
            application_browser.close_persistent_browser()

    @staticmethod
    def _handle_failure(attempt: dict, exc: Exception) -> None:
        LOGGER.exception("Candidature assistée %s : échec", attempt["id"])
        try:
            from . import application_browser
            if application_browser.submit_was_clicked(attempt["id"]):
                # Le clic d'envoi a eu lieu : un « échec » inviterait à renvoyer un doublon.
                application_browser.submission_unconfirmed(attempt["id"])
                return
            updated = db.transition_application_attempt(
                attempt["id"], expected={"running"}, status="failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
            from . import notifications
            notifications.application_intervention(updated, {
                "kind": "unexpected_error",
                "message": "Le formulaire a changé ou une erreur imprévue est survenue.",
                "action": "manual_fallback",
            })
        except (db.Conflict, db.NotFound):
            pass


_QUEUE: ApplicationQueue | None = None


def start_queue(handler: Handler | None = None) -> ApplicationQueue:
    global _QUEUE
    stop_queue()
    _QUEUE = ApplicationQueue(handler)
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
