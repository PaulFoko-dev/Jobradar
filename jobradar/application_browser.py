"""Exécution Playwright des adaptateurs de candidature, avec arrêt sûr avant envoi."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import (
    ai,
    application_adapters,
    application_supervisor,
    archive,
    capture,
    config,
    db,
    login_credentials,
    notifications,
    profile,
    tailoring,
)

CONFIRMATION_WORDS = ("thank you", "application received", "candidature reçue", "merci pour votre candidature")
CONFIRMATION_URL_WORDS = ("thank-you", "thank_you", "confirmation", "application-submitted", "success")
BLOCK_WORDS = ("captcha", "verify you are human", "connexion", "sign in", "log in", "verification code")
BLOCK_SELECTORS = ('input[type="password"]', 'iframe[src*="captcha" i]', '[data-sitekey]',
                   '[class*="captcha" i]', '[id*="captcha" i]')
VALIDATION_SELECTORS = ('[aria-invalid="true"]', '[role="alert"]', '.field-error', '.validation-error')
SUBMIT_SELECTORS = {
    "greenhouse": ("#submit_app", "button[type=submit]", "input[type=submit]"),
    "lever": ("button[type=submit]", ".postings-btn-submit"),
    "smartrecruiters": ("button[type=submit]", "button[data-test=submit-application]"),
    "workday": ("button[data-automation-id=bottom-navigation-next-button]", "button[type=submit]"),
    "generic": ("button[type=submit]", "input[type=submit]"),
}
WORKDAY_MAX_STEPS = 8
WORKDAY_FINAL_WORDS = ("submit", "send application", "envoyer", "soumettre", "postuler")
RENDER_WAIT_MS = 15_000
UNRESOLVED = "unresolved"
APPLICATION_LOCK_TIMEOUT_S = 600


LOGGER = logging.getLogger(__name__)
_PERSISTENT_MANAGER = None
_PERSISTENT_CONTEXT = None


class BrowserUnavailable(RuntimeError):
    pass


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.25):
            return True
    except OSError:
        return False


def system_browser(playwright=None) -> dict:
    """Navigateur du système à piloter ; à défaut, le Chromium fourni avec Playwright."""
    found = config.find_system_browser(config.browser_settings()["executable"])
    if found:
        return found
    try:
        if playwright is not None:
            path = playwright.chromium.executable_path
        else:
            with sync_playwright() as started:
                path = started.chromium.executable_path
    except PlaywrightError:
        path = ""
    if path and Path(path).is_file():
        return {"key": "chromium-integre", "name": "Chromium intégré", "path": path, "is_default": False}
    raise BrowserUnavailable("Aucun navigateur compatible (Chrome, Edge, Brave…) trouvé ; "
                             "indique son exécutable dans les Réglages.")


def _start_browser(browser: dict, port: int) -> None:
    profile = config.system_profile_dir(browser["key"])
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([browser["path"], f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                      "--no-first-run", "--no-default-browser-check"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _port_open(port):
            return
        time.sleep(.2)
    raise BrowserUnavailable(f"{browser['name']} n'a pas ouvert son port de connexion dans le délai imparti.")


def open_system_browser(playwright=None) -> dict:
    """Ouvre le navigateur dédié s'il ne tourne pas déjà ; renvoie le navigateur et son port."""
    port = config.browser_settings()["cdp_port"]
    browser = system_browser(playwright)
    if not _port_open(port):
        _start_browser(browser, port)
    return browser | {"port": port}


@contextmanager
def _browser_context(playwright):
    """Isole le moteur choisi ; le navigateur du système et son profil persistent après la candidature."""
    settings = config.browser_settings()
    if settings["engine"] == "chromium":
        context = playwright.chromium.launch_persistent_context(
            config.browser_profile_dir(), headless=False, accept_downloads=False,
            permissions=[], service_workers="block", viewport={"width": 1365, "height": 900})
        try:
            yield context
        finally:
            _close_quietly(context)
        return
    port = open_system_browser(playwright)["port"]
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    yield context


def _long_lived_context():
    """Contexte Chromium conservé dans le fil unique de la file Playwright."""
    global _PERSISTENT_MANAGER, _PERSISTENT_CONTEXT
    if _PERSISTENT_CONTEXT is not None:
        try:
            _ = _PERSISTENT_CONTEXT.pages
            return _PERSISTENT_CONTEXT
        except PlaywrightError:
            close_persistent_browser()
    manager = sync_playwright()
    playwright = manager.start()
    try:
        context = playwright.chromium.launch_persistent_context(
            config.browser_profile_dir(), headless=False, accept_downloads=False,
            permissions=[], service_workers="block", viewport={"width": 1365, "height": 900})
    except Exception:
        manager.stop()
        raise
    _PERSISTENT_MANAGER, _PERSISTENT_CONTEXT = manager, context
    return context


def close_persistent_browser() -> None:
    """À appeler depuis le fil de la file, auquel Playwright sync est attaché."""
    global _PERSISTENT_MANAGER, _PERSISTENT_CONTEXT
    if _PERSISTENT_CONTEXT is not None:
        _close_quietly(_PERSISTENT_CONTEXT)
    if _PERSISTENT_MANAGER is not None:
        try:
            _PERSISTENT_MANAGER.stop()
        except Exception:
            LOGGER.warning("Arrêt de Playwright impossible", exc_info=True)
    _PERSISTENT_MANAGER = _PERSISTENT_CONTEXT = None


def _headed_available() -> bool:
    return os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _selected_files(attempt: dict) -> dict[str, str]:
    selected = {}
    for document_id in (attempt["cv_document_id"], attempt["letter_document_id"]):
        if not document_id:
            continue
        document = db.get_document(document_id)
        pdf = next((item for item in document["files"] if item["name"].lower().endswith(".pdf")), None)
        item = pdf or (document["files"][0] if document["files"] else None)
        if item:
            selected[document["kind"]] = str(tailoring.document_file(document_id, item["name"]))
    return selected


def _fill(root, plan: dict) -> None:
    for item in plan["values"]:
        locator = root.locator(item["selector"]).first
        if item["kind"] == "file":
            locator.set_input_files(item["value"])
        elif item["kind"] == "checkbox":
            locator.set_checked(bool(item["value"]))
        elif item["kind"] == "select":
            locator.select_option(label=item["value"])
        elif item["kind"] == "radio":
            # Le plan contient le libellé visible validé par l'utilisateur,
            # jamais une valeur interne opaque propre au site.
            root.get_by_label(item["value"], exact=True).check()
        else:
            locator.fill(item["value"])


def _verify_filled(root, plan: dict) -> list[dict]:
    """Relit le DOM après remplissage ; un écart interdit tout clic d'envoi."""
    issues = []
    for item in plan["values"]:
        locator = root.locator(item["selector"]).first
        try:
            if item["kind"] == "file":
                names = locator.evaluate("el => Array.from(el.files || []).map(file => file.name)")
                actual = Path(item["value"]).name in names
            elif item["kind"] == "checkbox":
                actual = locator.is_checked()
            elif item["kind"] == "select":
                actual = locator.locator("option:checked").inner_text().strip()
            elif item["kind"] == "radio":
                chosen = root.locator(f'{item["selector"]}:checked')
                actual = root.get_by_label(str(item["value"]), exact=True).is_checked() if chosen.count() else False
            else:
                actual = locator.input_value()
        except PlaywrightError:
            issues.append({"label": item["label"], "reason": "champ devenu inaccessible"})
            continue
        expected = bool(item["value"]) if item["kind"] in {"file", "checkbox", "radio"} else str(item["value"])
        if actual != expected:
            issues.append({"label": item["label"], "reason": "valeur relue différente"})
    return issues


def _validation_errors(page) -> list[str]:
    """Relève uniquement les messages de validation visibles et non vides."""
    messages = []
    for selector in VALIDATION_SELECTORS:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 20)):
            item = locator.nth(index)
            try:
                text = " ".join(item.inner_text(timeout=500).split())
                if item.is_visible() and text and text not in messages:
                    messages.append(text[:300])
            except PlaywrightError:
                continue
    return messages


def _confirmed(page, body_text: str) -> bool:
    url = page.url.casefold()
    return any(word in body_text for word in CONFIRMATION_WORDS) or any(word in url for word in CONFIRMATION_URL_WORDS)


def _blocked(page, body_text: str) -> bool:
    """Détecte un vrai mur interactif, sans confondre un lien de pied de page."""
    if any(page.locator(selector).count() for selector in BLOCK_SELECTORS):
        return True
    compact = " ".join(body_text.split())
    return len(compact) < 2_000 and any(word in compact for word in BLOCK_WORDS)


def _try_login(page) -> bool:
    """Tente une seule soumission d'identifiants correspondant strictement au domaine courant."""
    if not page.locator('input[type="password"]').count():
        return False
    credential = login_credentials.for_host(urlsplit(page.url).hostname or "")
    if not credential:
        return False
    username, password = credential
    user = page.locator('input[type="email"], input[autocomplete="username"], input[name*="user" i]').first
    secret = page.locator('input[type="password"]').first
    if not user.count() or not secret.count():
        return False
    user.fill(username)
    secret.fill(password)
    button = page.locator('button[type="submit"], input[type="submit"]').first
    if not button.count():
        return False
    button.click()
    page.wait_for_timeout(1500)
    return not _blocked(page, page.locator("body").inner_text(timeout=5000).casefold())


def _wait_for_human(page, attempt: dict, timeout_ms: int = 300_000) -> bool:
    """Laisse la fenêtre ouverte pour connexion/captcha et reprend dès que le blocage disparaît."""
    intervention = {"kind": "authentication_or_captcha",
                    "message": "Interviens dans la fenêtre du navigateur ; JobRadar reprendra automatiquement.",
                    "action": "open_browser"}
    notifications.application_intervention(attempt, intervention)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
        visible = page.locator("body").inner_text(timeout=5000).casefold()
        if not _blocked(page, visible):
            return True
    return False


def _proof_directory(attempt: dict) -> Path:
    directory = archive.directory_of(attempt["application_id"]) / "envoi"
    directory.mkdir(exist_ok=True)
    return directory


def _write_answers(attempt: dict, plan: dict) -> None:
    lines = ["# Réponses du formulaire", "", f"Session : {attempt['id']}", ""]
    for item in plan["values"]:
        shown = "[fichier]" if item["kind"] == "file" else item["value"]
        lines.append(f"- **{item['label']}** : {shown} — source `{item['source']}`")
    profile.atomic_write(_proof_directory(attempt) / "reponses.md", ("\n".join(lines) + "\n").encode())
    motivation = next((str(item["value"]) for item in plan["values"]
                       if item.get("source") == "ia:motivation"), None)
    if motivation:
        profile.atomic_write(_proof_directory(attempt) / "motivation.md",
                             ("# Message de motivation\n\n" + motivation + "\n").encode())


def _merge_user_answers(plan: dict, answers: list[dict]) -> dict:
    """Réinjecte uniquement les réponses correspondant encore aux champs détectés sur la page."""
    pending = {item["selector"]: item for item in plan["interventions"]}
    values = list(plan["values"])
    for answer in answers or []:
        field = pending.pop(answer.get("selector"), None)
        if field is None:
            continue
        values.append({"selector": answer["selector"], "value": answer.get("value"),
                       "kind": field.get("input_kind") or answer.get("kind") or "text",
                       "confidence": 1.0, "source": "utilisateur", "label": field.get("label") or ""})
    return {"values": values, "interventions": list(pending.values()), "ready": not pending}


def _preview(plan: dict) -> list[dict]:
    """Résumé affichable : aucun chemin local de document ne quitte le moteur."""
    return [{"label": item.get("label") or "Champ", "value": "Fichier sélectionné" if item.get("kind") == "file"
             else item.get("value"), "source": item.get("source"), "kind": item.get("kind")}
            for item in plan["values"]]


def _review_required(attempt: dict) -> bool:
    """Applique la politique figée au démarrage de cette candidature."""
    policy = attempt.get("review_policy") or "first_five"
    if policy == "always":
        return True
    if policy == "never":
        return False
    return db.assisted_submitted_count() < 5


def _workday_navigation(page):
    """Renvoie le bouton de navigation et indique s'il s'agit explicitement de l'envoi final."""
    locator = page.locator("button[data-automation-id=bottom-navigation-next-button]")
    if not locator.count():
        return None, False
    button = locator.first
    text = " ".join(button.inner_text(timeout=3000).casefold().split())
    return button, any(word in text for word in WORKDAY_FINAL_WORDS)


def _intervention(attempt_id: int, kind: str, message: str, **extra) -> None:
    intervention = {"kind": kind, "message": message} | extra
    attempt = db.transition_application_attempt(
        attempt_id, expected={"running"}, status="needs_user",
        intervention=intervention,
    )
    notifications.application_intervention(attempt, intervention)


def _mark_application_container(page) -> bool:
    """Marque dans le DOM vivant le conteneur de la candidature visible.

    Premier ``<form>`` visible qui contient un CV, ou un e-mail et un nom ; à défaut (Ashby, modales
    React), le plus petit bloc englobant tous ces champs clés et au moins un bouton d'action.
    """
    return bool(page.evaluate(r"""() => {
      const folded = value => (value || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
      const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
      // Un champ fichier est souvent masqué derrière un bouton stylé : son bloc parent doit être visible.
      const usable = c => shown(c) || ((c.type || '').toLowerCase() === 'file' && c.parentElement && shown(c.parentElement));
      const isFile = c => (c.type || '').toLowerCase() === 'file';
      const isEmail = c => c.type === 'email' || folded(`${c.name} ${c.id}`).includes('email');
      const isName = c => /(^|\s)(name|nom|prenom|surname)(\s|$)/.test(folded(`${c.name} ${c.id} ${c.getAttribute('aria-label')}`));
      const keys = root => [...root.querySelectorAll('input,textarea,select')].filter(usable)
        .filter(c => isFile(c) || isEmail(c) || isName(c));
      const valid = list => list.some(isFile) || (list.some(isEmail) && list.some(isName));
      let target = [...document.querySelectorAll('form')].find(form => shown(form) && valid(keys(form)));
      if (!target) {
        const found = keys(document);
        if (!valid(found)) return false;
        target = found[0].parentElement;
        while (target && !found.every(c => target.contains(c))) target = target.parentElement;
        const action = 'button,[role=button],input[type=submit],input[type=button]';
        while (target && target !== document.body && !target.querySelector(action)) target = target.parentElement;
        if (!target) return false;
      }
      document.querySelectorAll('[data-jobradar-form]').forEach(el => el.removeAttribute('data-jobradar-form'));
      target.setAttribute('data-jobradar-form', '');
      return true;
    }"""))


def _marked_match(page):
    """Champs lus dans le conteneur effectivement marqué : Python et la page visent le même bloc."""
    container = page.locator("[data-jobradar-form]")
    if not container.count():
        return None
    tag = BeautifulSoup(container.first.evaluate("el => el.outerHTML"), "html.parser").find()
    fields = application_adapters.adapter_for("generic")._fields(tag) if tag else []
    return application_adapters.FormMatch(fields, "[data-jobradar-form]") if fields else None


def _live_form(page, provider: str):
    """Formulaire reconnu dans le HTML et, pour le générique, réellement visible dans la page.

    Une fenêtre modale encore fermée contient souvent le formulaire : le remplir échouerait sur des
    champs invisibles. Il n'est donc retenu qu'une fois affiché.
    """
    match = application_adapters.adapter_for(provider).locate(page.content())
    if provider != "generic":
        return match
    if not _mark_application_container(page):
        raise application_adapters.NotApplicationForm("Formulaire présent mais encore masqué.")
    return _marked_match(page) or match


def _action_button(page):
    rejected = application_adapters.APPLY_HELP_WORDS
    candidates = page.locator("button, [role=button]")
    best = None
    for index in range(min(candidates.count(), 100)):
        item = candidates.nth(index)
        try:
            text = application_adapters._fold(item.inner_text(timeout=250))
            if item.is_visible() and any(word in text for word in application_adapters.APPLY_WORDS) \
                    and not any(word in text for word in rejected):
                score = int(text in {"apply now", "apply", "postuler", "je postule", "apply for this job"})
                if best is None or score > best[0]:
                    best = (score, item, text)
        except PlaywrightError:
            continue
    return best[1:] if best else (None, None)


def _dismiss_cookies(page) -> str | None:
    for label in ("Tout accepter", "Accept all", "J'accepte", "Accepter tout"):
        button = page.get_by_role("button", name=label, exact=True)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=2_000)
                return label
        except PlaywrightError:
            continue
    return None


def _wait_for_application_ui(page, provider: str, timeout_ms: int = RENDER_WAIT_MS) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        html = page.content()
        try:
            application_adapters.adapter_for(provider).locate(html)
            if _mark_application_container(page):
                return "form"
        except (application_adapters.NotApplicationForm, ValueError):
            pass
        if application_adapters.find_apply_link(html):
            return "link"
        button, _ = _action_button(page)
        if button is not None:
            return "button"
        page.wait_for_timeout(250)
    return "timeout"


def process(attempt: dict) -> None:
    """Remplit, vérifie et soumet ; toute ambiguïté devient ``needs_user``."""
    provider = attempt["provider"]
    if provider not in application_adapters.SUPPORTED:
        _intervention(attempt["id"], "unsupported_form", "Ce formulaire nécessite le mode manuel.",
                      action="manual_fallback")
        return
    if not _headed_available():
        raise BrowserUnavailable("Aucune session graphique n'est disponible pour le navigateur de candidature.")
    from . import application_flow  # évite une dépendance circulaire au chargement
    if application_flow.already_sent(attempt["application_id"]):
        # Envoyée à la main entre-temps : relancer le robot risquerait un doublon.
        db.transition_application_attempt(attempt["id"], expected={"queued", "running"}, status="cancelled")
        return
    candidate, files = profile.load_profile(), _selected_files(attempt)
    if "cv" not in files:
        _intervention(attempt["id"], "missing_document", "Le fichier CV sélectionné est introuvable.")
        return
    application = db.get_application(attempt["application_id"])
    if application.get("offer_online") is False or capture.is_offer_online(attempt["target_url"]) is False:
        _intervention(attempt["id"], "offer_removed",
                      "L'offre n'est plus en ligne. Aucun envoi n'a été effectué.", action="manual_fallback")
        return
    approved = (attempt.get("intervention") or {}).get("kind") == "review_approved"
    lock_dir = (config.system_profile_root() if config.browser_settings()["engine"] == "system"
                else config.browser_profile_dir())
    browser_lock = capture._profile_lock(lock_dir)
    if not browser_lock.acquire(timeout=APPLICATION_LOCK_TIMEOUT_S):
        raise BrowserUnavailable(
            "Le navigateur JobRadar est resté occupé pendant 10 min ; la candidature sera reprise plus tard."
        )
    outcome = None
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        settings = config.browser_settings()

        def run(context):
            nonlocal outcome
            existing_pages = set(context.pages)
            try:
                outcome = _run_session(context, attempt, provider, candidate, files, approved)
            finally:
                for opened in set(context.pages) - existing_pages:
                    try:
                        # Un arrêt manuel reste exactement là où il a eu lieu. Une confirmation
                        # demeure visible quelques secondes avant fermeture.
                        if outcome == "submitted":
                            opened.wait_for_timeout(5_000)
                            opened.close()
                    except PlaywrightError:
                        pass

        if settings["engine"] == "chromium" and settings.get("keep_open", True):
            run(_long_lived_context())
        else:
            with sync_playwright() as playwright:
                with _browser_context(playwright) as context:
                    run(context)
    except (PlaywrightError, OSError, ValueError) as exc:
        if submit_was_clicked(attempt["id"]):
            submission_unconfirmed(attempt["id"])
            return
        raise BrowserUnavailable(str(exc)) from exc
    finally:
        browser_lock.release()
    if outcome == "submitted":
        application_flow.finalize_assisted_submission(attempt["id"])


def _close_quietly(context) -> None:
    try:
        context.close()
    except PlaywrightError:
        LOGGER.warning("Fermeture du navigateur de candidature impossible", exc_info=True)


def submit_was_clicked(attempt_id: int) -> bool:
    return bool(db.get_application_attempt(attempt_id).get("submit_clicked_at_utc"))


def submission_unconfirmed(attempt_id: int) -> None:
    """Après le clic d'envoi, tout doute devient une demande de vérification, jamais un échec relançable."""
    attempt = db.transition_application_attempt(attempt_id, expected={"running"}, status="needs_user",
                                                intervention=dict(db.UNCONFIRMED_SUBMISSION))
    notifications.application_intervention(attempt, db.UNCONFIRMED_SUBMISSION)


def _application_form(page, attempt: dict, provider: str, *, follow_link: bool, trace: str = "",
                      report: bool = True):
    """Renvoie (fournisseur, formulaire, page) ; sur une page d'offre, suit une fois le lien « Postuler ».

    Si aucun formulaire de candidature n'est reconnu, rien n'est rempli ni envoyé : une intervention
    propose le mode manuel et la fonction renvoie ``None``. Avec ``report=False``, elle renvoie
    ``UNRESOLVED`` sans intervention, pour laisser la main à la supervision IA.
    """
    waited = _wait_for_application_ui(page, provider)
    try:
        return provider, _live_form(page, provider), page
    except (application_adapters.NotApplicationForm, ValueError):
        link = application_adapters.find_apply_link(page.content()) if follow_link else None
    if link:
        from . import application_flow  # évite une dépendance circulaire au chargement
        page.goto(urljoin(page.url, link), wait_until="domcontentloaded", timeout=45_000)
        destination = application_flow.provider_for(page.url)
        if destination in application_flow.MANUAL_ONLY_HOSTS:
            _intervention(attempt["id"], "manual_only_destination",
                          "Le lien « Postuler » mène à LinkedIn ou Indeed, disponibles uniquement en mode manuel. "
                          "Rien n'a été rempli ni envoyé.", action="manual_fallback")
            return None
        return _application_form(page, attempt, destination, follow_link=False,
                                 trace=f"Lien « Postuler » suivi vers {page.url}; ", report=report)
    if follow_link:
        cookie = _dismiss_cookies(page)
        button, label = _action_button(page)
        if button is not None:
            pages = set(page.context.pages)
            old_url = page.url
            button.click(timeout=10_000)
            page.wait_for_timeout(250)
            opened = set(page.context.pages) - pages
            if opened:
                page = opened.pop()
                page.wait_for_load_state("domcontentloaded", timeout=45_000)
            _wait_for_application_ui(page, provider)
            try:
                return provider, _live_form(page, provider), page
            except (application_adapters.NotApplicationForm, ValueError):
                if not report:
                    return UNRESOLVED
                details = f"bouton « {label} » cliqué"
                if cookie:
                    details = f"bandeau « {cookie} » fermé, {details}"
                _intervention(attempt["id"], "not_application_form",
                              f"Après {details} et 15 s d'attente, aucun formulaire reconnu n'est apparu "
                              f"(navigation : {old_url != page.url}). Rien n'a été envoyé.",
                              action="manual_fallback")
                return None
    if not report:
        return UNRESOLVED
    _intervention(attempt["id"], "not_application_form",
                  f"{trace}Après {15 if waited == 'timeout' else 0} s d'attente, cette page ne contient pas de formulaire de candidature reconnu "
                  "(champ CV, ou e-mail et nom). Rien n'a été rempli ni envoyé : passe en mode manuel.",
                  action="manual_fallback")
    return None


class _Supervision:
    """État de la supervision IA d'une tentative : profil, journal, empreinte de la dernière page."""

    def __init__(self, attempt: dict, candidate: dict, files: dict | None = None) -> None:
        self.mode = application_supervisor.mode()
        self.attempt = attempt
        application = db.get_application(attempt["application_id"])
        self.offer = {"title": application.get("title"), "company": application.get("company")}
        self.journal = application_supervisor.Journal(_proof_directory(attempt), attempt["id"])
        self.available = self.mode != "off"
        self.last_hash = None
        facts = self._document_facts(files or {})
        # Le profil prime ; le CV et la lettre envoyés complètent ce qui lui manque (LinkedIn, adresse…),
        # y compris pour les règles automatiques qui lisent ``candidate``.
        application_supervisor.enrich_candidate(candidate, facts)
        self.values = application_supervisor.derived_values(facts | application_supervisor.profile_values(candidate))

    def _document_facts(self, files: dict) -> dict[str, str]:
        if not self.available or not files:
            return {}
        try:
            facts = application_supervisor.document_facts(files, self.journal.directory)
        except application_supervisor.SupervisionError as exc:
            self.journal.note(f"Lecture du CV et de la lettre impossible, profil seul : {exc}")
            return {}
        if facts:
            self.journal.note("Informations lues dans le CV et la lettre : " + ", ".join(sorted(facts)))
        return facts

    def decide(self, page, *, hints: dict | None = None, pending: list[dict] | None = None):
        """Décision filtrée, ou ``None`` si l'IA est indisponible (repli sur les heuristiques)."""
        if not self.available:
            return None, None
        try:
            snap = application_supervisor.snapshot(page)
            decision = application_supervisor.decide(snap, offer=self.offer, values=self.values,
                                                     hints=hints, pending=pending)
        except (application_supervisor.SupervisionError, PlaywrightError) as exc:
            self.available = False
            self.journal.note(f"IA indisponible, repli sur les règles automatiques : {exc}")
            return None, None
        return snap, decision


def _ai_intervention(attempt: dict, kind: str, decision: dict, message: str, **extra) -> None:
    understanding = decision.get("understanding") if decision else ""
    _intervention(attempt["id"], kind, f"{message} Analyse de l'IA : {understanding}" if understanding else message,
                  **extra)


def _ai_navigate(page, attempt: dict, provider: str, supervision: _Supervision, hints: dict):
    """L'IA pilote l'ouverture de la candidature jusqu'à un formulaire reconnu.

    Renvoie (fournisseur, formulaire, page), ``None`` si une intervention a été posée, ou ``UNRESOLVED``
    si l'IA est indisponible.
    """
    from . import application_flow  # évite une dépendance circulaire au chargement
    login_attempted = False
    for _ in range(application_supervisor.MAX_DECISIONS):
        try:
            return provider, _live_form(page, provider), page
        except (application_adapters.NotApplicationForm, ValueError):
            pass
        snap, decision = supervision.decide(page, hints=hints)
        if decision is None:
            return UNRESOLVED
        if snap["hash"] == supervision.last_hash:
            supervision.journal.decision(page, snap, decision, ["aucun progrès depuis l'étape précédente"])
            _ai_intervention(attempt, "no_progress", decision,
                             "La page n'a pas changé après l'action demandée. Rien n'a été envoyé.",
                             action="manual_fallback")
            return None
        supervision.last_hash = snap["hash"]
        kind = decision["page_kind"]
        if kind == "closed_offer":
            supervision.journal.decision(page, snap, decision, ["offre clôturée"])
            _ai_intervention(attempt, "offer_removed", decision, "L'offre semble clôturée. Rien n'a été envoyé.",
                             action="manual_fallback")
            return None
        if kind == "confirmation":
            supervision.journal.decision(page, snap, decision, ["page de confirmation avant tout envoi"])
            _ai_intervention(attempt, "already_confirmed", decision,
                             "La page affiche déjà une confirmation sans envoi de JobRadar : vérifie si tu as "
                             "déjà postulé.", action="verify")
            return None
        if kind in {"login", "account_creation", "captcha"}:
            supervision.journal.decision(page, snap, decision, ["connexion ou vérification requise"])
            if not login_attempted:
                login_attempted = True
                if _try_login(page):
                    continue
            if _wait_for_change(page, attempt, snap["hash"]):
                supervision.last_hash = None
                continue
            _ai_intervention(attempt, "authentication_or_captcha", decision,
                             "Le délai d'intervention est dépassé. Reprends la session quand tu es disponible.",
                             action="open_browser")
            return None
        clicks = [item for item in decision["actions"]
                  if item["type"] == "click" and item["purpose"] in application_supervisor.NAVIGATION_PURPOSES]
        if not clicks:
            stop = next((item["reason"] for item in decision["actions"] if item["type"] == "stop"), "")
            supervision.journal.decision(page, snap, decision, [f"arrêt : {stop}" if stop else "aucune action sûre"])
            _ai_intervention(attempt, "not_application_form", decision,
                             "Aucune action sûre pour ouvrir la candidature. Rien n'a été envoyé.",
                             action="manual_fallback")
            return None
        executed = []
        for item in clicks:
            before = set(page.context.pages)
            target = page.locator(f'[data-jobradar-id="{item["id"]}"]').first
            try:
                target.click(timeout=10_000)
            except PlaywrightError as exc:
                executed.append(f"clic n°{item['id']} ({item['purpose']}) impossible : {exc.message[:120]}")
                continue
            executed.append(f"clic n°{item['id']} ({item['purpose']}) : {item['reason']}")
            page.wait_for_timeout(500)
            opened = set(page.context.pages) - before
            if opened:
                page = opened.pop()
                page.wait_for_load_state("domcontentloaded", timeout=45_000)
            if item["purpose"] in {"open_application", "next_step"}:
                break  # la page a changé : les numéros suivants ne sont plus fiables
        supervision.journal.decision(page, snap, decision, executed)
        destination = application_flow.provider_for(page.url)
        if destination in application_flow.MANUAL_ONLY_HOSTS:
            _intervention(attempt["id"], "manual_only_destination",
                          "La candidature mène à LinkedIn ou Indeed, disponibles uniquement en mode manuel. "
                          "Rien n'a été rempli ni envoyé.", action="manual_fallback")
            return None
        if destination != provider and destination in application_adapters.SUPPORTED:
            provider = destination
        _wait_for_application_ui(page, provider, timeout_ms=8_000)
    _intervention(attempt["id"], "too_many_steps",
                  "L'IA n'a pas atteint de formulaire dans la limite de douze étapes. Rien n'a été envoyé.",
                  action="manual_fallback")
    return None


def _wait_for_change(page, attempt: dict, initial_hash: str, timeout_ms: int = 300_000) -> bool:
    """Connexion ou captcha : laisse la main à l'utilisateur et reprend dès que la page change."""
    notifications.application_intervention(attempt, {
        "kind": "authentication_or_captcha",
        "message": "Interviens dans la fenêtre du navigateur ; JobRadar reprendra automatiquement.",
        "action": "open_browser"})
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        page.wait_for_timeout(2_000)
        try:
            if application_supervisor.snapshot(page)["hash"] != initial_hash:
                page.wait_for_timeout(1_500)
                return True
        except PlaywrightError:
            continue
    return False


def _continued_form(page, provider: str):
    """Étape suivante d'un formulaire générique : le conteneur déjà marqué reste la seule cible."""
    match = _marked_match(page) if provider == "generic" else None
    return (provider, match, page) if match else None


def _locate(page, attempt: dict, provider: str, step: int, supervision: _Supervision):
    """Formulaire reconnu par les heuristiques, sinon par l'IA (ou l'inverse en mode « fallback »)."""
    if step > 0:
        page.wait_for_timeout(500)
        try:
            return provider, _live_form(page, provider), page
        except (application_adapters.NotApplicationForm, ValueError):
            pass
        continued = _continued_form(page, provider)
        if continued is not None:
            return continued
        located = _application_form(page, attempt, provider, follow_link=False, report=False)
        if located != UNRESOLVED:
            return located
        _intervention(attempt["id"], "not_application_form",
                      f"L'étape {step + 1} du formulaire n'a pas été reconnue. Rien n'a été envoyé.",
                      action="manual_fallback")
        return None
    if supervision.mode == "off":
        return _application_form(page, attempt, provider, follow_link=True)
    waited = _wait_for_application_ui(page, provider)
    if waited == "form":
        return _application_form(page, attempt, provider, follow_link=False)
    if supervision.mode == "fallback":
        located = _application_form(page, attempt, provider, follow_link=True, report=False)
        if located != UNRESOLVED:
            return located
        supervision.journal.note("Règles automatiques sans résultat : l'IA prend la main.")
    html = page.content()
    hints = {"lien_postuler_detecte": application_adapters.find_apply_link(html) or "",
             "bouton_postuler_detecte": (_action_button(page)[1] or "")}
    located = _ai_navigate(page, attempt, provider, supervision, hints)
    if located != UNRESOLVED:
        return located
    return _application_form(page, attempt, provider, follow_link=True)


def _supervise_form(form, page, plan: dict, supervision: _Supervision, files: dict, cache: dict,
                    candidate: dict, application: dict, letter_text: str, lang: str):
    """Résout avec l'IA les champs obligatoires restés sans réponse ; renvoie (plan, bouton désigné)."""
    pending = [item for item in plan["interventions"] if item.get("kind") != "sensitive"]
    needs_button = not any(form.locator(selector).count() for selector in SUBMIT_SELECTORS["generic"])
    if supervision.mode == "off" or (supervision.mode == "fallback" and not pending and not needs_button):
        return plan, None
    snap, decision = supervision.decide(page, pending=[
        {"label": item["label"], "type": item.get("input_kind"), "options": item.get("options") or []}
        for item in pending])
    if decision is None:
        return plan, None
    # Les numéros n'existent qu'après l'instantané : on les relit maintenant pour chaque champ en attente.
    ids = {}
    for item in pending:
        try:
            ids[item["selector"]] = form.locator(item["selector"]).first.get_attribute("data-jobradar-id")
        except PlaywrightError:
            ids[item["selector"]] = None
    by_id = {int(value): selector for selector, value in ids.items() if value and value.isdigit()}
    pending_by_selector = {item["selector"]: item for item in pending}
    accepted, executed = {}, []
    for action in decision["actions"]:
        selector = by_id.get(action.get("id"))
        field = pending_by_selector.get(selector)
        if field is None:
            continue
        value = None
        if action["type"] == "fill" and action["source"] in supervision.values:
            value = supervision.values[action["source"]]
            if field.get("maxlength") and len(value) > field["maxlength"]:
                value = None
        elif action["type"] == "fill" and action["source"] == "motivation":
            try:
                if not cache.get("message"):
                    cache["message"] = (
                        application_adapters.letter_excerpt(cache.get("letter_paragraphs"), field.get("maxlength"))
                        or application_adapters.motivation_message(candidate, application, letter_text, lang,
                                                                   field.get("maxlength")))
                value = cache["message"]
            except (ai.AIError, ValueError):
                value = None
        elif action["type"] == "upload" and files.get(action["source"]):
            value = files[action["source"]]
        elif action["type"] == "select" and action["option"] in (field.get("options") or []):
            value = action["option"]
        elif action["type"] == "check" and field.get("input_kind") == "checkbox":
            value = True
        if value is None:
            continue
        accepted[selector] = {"selector": selector, "value": value, "kind": field.get("input_kind") or "text",
                              "confidence": decision["confidence"], "source": f"ia-superviseur:{action['source'] or action['type']}",
                              "label": field["label"]}
        executed.append(f"{field['label'][:60]} ← {action['source'] or action['option'] or action['type']}")
    button = None
    designated = next((item for item in decision["actions"]
                       if item["type"] == "click" and item["purpose"] in {"submit", "next_step"}), None)
    if designated is not None:
        locator = form.locator(f'[data-jobradar-id="{designated["id"]}"]')
        if locator.count():
            button = (designated["purpose"], locator.first)
            executed.append(f"bouton n°{designated['id']} désigné ({designated['purpose']})")
        else:
            executed.append(f"bouton n°{designated['id']} ignoré : hors du formulaire")
    supervision.journal.decision(page, snap, decision, executed)
    interventions = [item for item in plan["interventions"] if item["selector"] not in accepted]
    return {"values": [*plan["values"], *accepted.values()], "interventions": interventions,
            "ready": not interventions}, button


def _final_review(plan: dict, supervision: _Supervision) -> list[str]:
    """Contrôle de cohérence de l'IA ; rien de sensible ni aucun chemin local n'est transmis."""
    if supervision.mode == "off" or not supervision.available:
        return []
    fields = [{"label": item.get("label") or "Champ",
               "valeur": Path(str(item["value"])).name if item.get("kind") == "file" else item.get("value")}
              for item in plan["values"]]
    try:
        issues = application_supervisor.review(fields, supervision.offer)
    except application_supervisor.SupervisionError as exc:
        supervision.journal.note(f"Relecture IA indisponible : {exc}")
        return []
    supervision.journal.note("Relecture IA avant envoi : " + ("rien à signaler" if not issues else "; ".join(issues)))
    return issues


def _run_session(context, attempt: dict, provider: str, candidate: dict, files: dict, approved: bool) -> str | None:
    """Parcourt le formulaire ; renvoie « submitted » si l'envoi est confirmé, sinon une intervention est posée."""
    page = context.new_page()
    page.goto(attempt["target_url"], wait_until="domcontentloaded", timeout=45_000)
    supervision = _Supervision(attempt, candidate, files)
    supervision.journal.note(f"Ouverture de {attempt['target_url']} (supervision IA : {supervision.mode})")
    collected: dict[str, dict] = {}
    final_submit = None
    login_attempted = False
    # Une page générique peut mener, via « Postuler », à un formulaire Workday en plusieurs étapes.
    max_steps = WORKDAY_MAX_STEPS if provider in {"workday", "generic"} else 1
    for step in range(max_steps):
        visible = page.locator("body").inner_text(timeout=5000).casefold()
        if _blocked(page, visible):
            logged_in = False
            if not login_attempted:
                login_attempted = True
                logged_in = _try_login(page)
            if not logged_in and not _wait_for_human(page, attempt):
                _intervention(attempt["id"], "authentication_or_captcha",
                              "Le délai d'intervention est dépassé. Reprends la session quand tu es disponible.",
                              action="open_browser")
                return None
        located = _locate(page, attempt, provider, step, supervision)
        if located is None:
            return None
        provider, match, page = located
        # Remplissage, relecture et envoi restent confinés au formulaire de candidature reconnu :
        # un formulaire de recherche ou de newsletter de la même page n'est jamais touché.
        if not _mark_application_container(page) and not page.locator("[data-jobradar-form]").count():
            _intervention(attempt["id"], "not_application_form",
                          "Le formulaire détecté a disparu avant le remplissage. Rien n'a été envoyé.")
            return None
        form = page.locator(match.selector).first
        plan = application_adapters.fill_plan(match.fields, candidate, files, ai_metadata=True)
        application = db.get_application(attempt["application_id"])
        lang, letter_text, cache = application.get("language") or "fr", "", {}
        try:
            lang = page.locator("html").get_attribute("lang") or lang
            if files.get("lettre"):
                letter_path = Path(files["lettre"])
                letter_text = profile.extract_text(letter_path.name, letter_path.read_bytes())
            if attempt.get("letter_document_id"):
                cache["letter_paragraphs"] = db.get_document(attempt["letter_document_id"])["composition"].get(
                    "paragraphs") or []
            motivation_path = _proof_directory(attempt) / "motivation.md"
            if motivation_path.exists():
                cache["message"] = motivation_path.read_text(encoding="utf-8").split("\n\n", 1)[-1].strip()
            plan = application_adapters.ai_plan(plan, candidate, application, letter_text, lang, cache)
            if cache.get("message") and not motivation_path.exists():
                profile.atomic_write(motivation_path,
                                     ("# Message de motivation\n\n" + cache["message"] + "\n").encode())
        except (ai.AIError, ValueError, OSError):
            # Le quota ou l'assistant indisponible ne doit jamais bloquer le
            # repli : les champs restent proposés directement à l'utilisateur.
            pass
        plan, designated = _supervise_form(form, page, plan, supervision, files, cache, candidate,
                                           application, letter_text, lang)
        plan = _merge_user_answers(plan, attempt.get("answers") or [])
        _fill(form, plan)
        verification_issues = _verify_filled(form, plan)
        collected.update({item["selector"]: item for item in plan["values"]})
        combined = {"values": list(collected.values()), "interventions": plan["interventions"],
                    "ready": plan["ready"]}
        if verification_issues:
            _intervention(attempt["id"], "verification_failed",
                          "Certains champs diffèrent après leur remplissage. Aucun envoi n'a été effectué.",
                          fields=verification_issues, preview=_preview(combined), action="verify")
            return None
        validation_errors = _validation_errors(page)
        if validation_errors:
            _intervention(attempt["id"], "page_validation_failed",
                          "Le formulaire signale des erreurs. Aucun envoi n'a été effectué.",
                          fields=[{"label": message, "reason": "erreur affichée par le site"}
                                  for message in validation_errors],
                          preview=_preview(combined), action="verify")
            return None
        if not plan["ready"]:
            _intervention(attempt["id"], "missing_fields", "Des champs obligatoires nécessitent ta réponse.",
                          fields=plan["interventions"], preview=_preview(combined), step=step + 1)
            return None
        if provider != "workday":
            if designated and designated[0] == "next_step":
                # Formulaire en plusieurs étapes : « Suivant » n'envoie rien, on passe à l'étape suivante.
                designated[1].click(timeout=10_000)
                supervision.journal.note(f"Étape {step + 1} remplie, passage à l'étape suivante.")
                page.wait_for_timeout(1000)
                continue
            if designated and designated[0] == "submit":
                final_submit = designated[1]
            plan = combined
            break
        navigation, is_final = _workday_navigation(page)
        if navigation is None:
            _intervention(attempt["id"], "navigation_missing",
                          "Le bouton permettant de poursuivre sur Workday est introuvable.",
                          preview=_preview(combined), action="verify")
            return None
        if is_final:
            final_submit, plan = navigation, combined
            break
        navigation.click(timeout=10_000)
        page.wait_for_timeout(1000)
    else:
        _intervention(attempt["id"], "too_many_steps",
                      "Le formulaire Workday dépasse la limite de sécurité de huit étapes.", action="manual_fallback")
        return None
    proof = _proof_directory(attempt)
    page.screenshot(path=str(proof / "avant-envoi.png"), full_page=True)
    _write_answers(attempt, plan)
    issues = _final_review(plan, supervision)
    if issues:
        _intervention(attempt["id"], "verification_failed",
                      "La relecture de l'IA signale une incohérence. Aucun envoi n'a été effectué.",
                      fields=[{"label": issue, "reason": "relecture IA"} for issue in issues],
                      preview=_preview(plan), action="verify")
        return None
    if _review_required(attempt) and not approved:
        _intervention(attempt["id"], "review_required",
                      "Vérifie les réponses avant l'envoi de cette candidature.", action="approve",
                      preview=_preview(plan))
        return None
    submit = (final_submit if final_submit is not None else
              next((form.locator(selector).first for selector in SUBMIT_SELECTORS[provider]
                    if form.locator(selector).count()), None))
    if submit is None:
        _intervention(attempt["id"], "submit_missing", "Bouton d'envoi introuvable.")
        return None
    # À partir d'ici la candidature peut être partie : le fait est enregistré avant le clic,
    # pour qu'aucune reprise (Réessayer, redémarrage) ne puisse la renvoyer.
    db.mark_submit_clicked(attempt["id"])
    submit.click(timeout=10_000)
    page.wait_for_timeout(1500)
    text = page.locator("body").inner_text(timeout=5000).casefold()
    if not _confirmed(page, text):
        # Après le clic seulement, l'IA peut reconnaître une confirmation formulée autrement.
        page.wait_for_timeout(2000)
        _, decision = supervision.decide(page)
        if not decision or decision["page_kind"] != "confirmation":
            supervision.journal.note("Envoi cliqué, confirmation non reconnue : vérification demandée.")
            submission_unconfirmed(attempt["id"])
            return None
        supervision.journal.note(f"Confirmation reconnue par l'IA : {decision['understanding']}")
    supervision.journal.note("Candidature envoyée.")
    try:
        page.screenshot(path=str(proof / "confirmation.png"), full_page=True)
        profile.atomic_write(proof / "session.json", json.dumps(
            {"attempt_id": attempt["id"], "provider": provider, "confirmation_url": page.url},
            ensure_ascii=False, indent=2,
        ).encode())
    except (PlaywrightError, OSError):
        LOGGER.warning("Preuve de confirmation non enregistrée pour la session %s", attempt["id"], exc_info=True)
    return "submitted"
