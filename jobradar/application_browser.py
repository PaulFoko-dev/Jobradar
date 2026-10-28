"""Exécution Playwright des adaptateurs de candidature, avec arrêt sûr avant envoi."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from urllib.request import urlopen

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
HUMAN_CHECK_WORDS = ("verify you are human", "verifiez que vous etes humain", "je ne suis pas un robot",
                     "i m not a robot")
BLOCK_SELECTORS = ('input[type="password"]', 'iframe[src*="captcha" i]', 'iframe[src*="challenges.cloudflare" i]',
                   '[data-sitekey]', '[class*="captcha" i]', '[id*="captcha" i]')
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
RESUME_METHOD_WORDS = ("from device", "upload resume", "upload cv", "with resume", "depuis mon appareil")
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


def _unfreeze_tabs(port: int) -> list[str]:
    """Un seul onglet figé (script bloqué) suffit à bloquer la connexion au navigateur : il est rechargé.

    Chaque onglet doit répondre à une évaluation triviale en 3 s ; sinon le navigateur le recharge, ce qui
    marche même quand la page est figée. Rien n'est fermé ni envoyé.
    """
    try:
        from websockets.sync.client import connect  # fourni par uvicorn[standard]
    except ImportError:
        return []
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
            targets = [item for item in json.load(response)
                       if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
    except (OSError, ValueError):
        return []
    reloaded = []
    for target in targets:
        try:
            with connect(target["webSocketDebuggerUrl"], open_timeout=3, max_size=None) as socket_:
                socket_.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1"}}))
                try:
                    socket_.recv(timeout=3)
                except TimeoutError:
                    socket_.send(json.dumps({"id": 2, "method": "Page.reload", "params": {}}))
                    reloaded.append(target["url"])
                    LOGGER.warning("Onglet figé rechargé avant connexion : %s", target["url"][:120])
        except Exception:  # noqa: BLE001 - onglet fermé entre-temps, protocole refusé : on continue
            continue
    return reloaded


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
    _unfreeze_tabs(port)
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


def _retag(root, plan: dict) -> None:
    """Repose les repères des champs sans nom, effacés si le site a reconstruit le formulaire."""
    if not any("data-jobradar-field" in item["selector"] for item in plan["values"]):
        return
    try:
        root.evaluate(TAG_CONTROLS_JS)
    except PlaywrightError:
        pass


PHONE_ERROR_WORDS = ("phone", "telephone", "mobile", "portable")


def _retry_phone_national(form, page, plan: dict, errors: list[str]) -> bool:
    """Le site refuse « +33 6 … » : le numéro est ressaisi au format national (0xxxxxxxxx), une seule fois."""
    if not any(word in application_adapters._fold(error) for error in errors for word in PHONE_ERROR_WORDS):
        return False
    changed = False
    for item in plan["values"]:
        national = application_adapters.national_phone(item["value"]) if item["kind"] in {"tel", "text"} else None
        if (str(item.get("source", "")).startswith("profil:phone") and national
                and str(item["value"]).strip().startswith(("+", "00"))):
            item["value"] = national
            changed = True
    if changed:
        _fill(form, {"values": [item for item in plan["values"] if str(item.get("source", "")).startswith("profil:phone")]})
        page.wait_for_timeout(500)
    return changed


PASTE_RESUME_TERMS = ("paste resume", "paste your resume", "copy and paste resume", "resume text", "cv text",
                      "coller votre cv", "collez votre cv", "copier coller", "texte du cv")


def _paste_resume(plan: dict, files: dict) -> dict:
    """« Copy and paste resume » : le texte exact du CV envoyé, lu dans son PDF."""
    wanted = [item for item in plan["interventions"] if item.get("input_kind") == "textarea"
              and any(term in application_adapters._fold(item["label"]) for term in PASTE_RESUME_TERMS)]
    if not wanted or not files.get("cv"):
        return plan
    try:
        path = Path(files["cv"])
        text = "\n".join(line.strip() for line in profile.extract_text(path.name, path.read_bytes()).splitlines()
                         if line.strip())
    except (OSError, ValueError):
        return plan
    if not text:
        return plan
    values = [{"selector": item["selector"], "value": text[: item.get("maxlength") or len(text)], "kind": "textarea",
               "confidence": 1.0, "source": "document:cv_texte", "label": item["label"]} for item in wanted]
    selectors = {item["selector"] for item in wanted}
    interventions = [item for item in plan["interventions"] if item["selector"] not in selectors]
    return {"values": [*plan["values"], *values], "interventions": interventions, "ready": not interventions}


NEXT_LABELS = re.compile(r"^\s*(next|suivant|continuer|continue|save and continue|étape suivante)\s*$", re.I)


def _settle(page) -> None:
    """Attend la fin des échanges réseau de la page (au plus 15 s), sans jamais échouer."""
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightError:
        pass
    page.wait_for_timeout(1_000)


FILL_ORDER = {"select": 0, "listbox": 0, "radio": 1}  # une liste (pays…) peut reconstruire ou vider les champs suivants


def _fill(root, plan: dict) -> None:
    _retag(root, plan)
    for item in sorted(plan["values"], key=lambda item: FILL_ORDER.get(item["kind"], 2)):
        locator = root.locator(item["selector"]).first
        if item["kind"] == "file":
            locator.set_input_files(item["value"])
        elif item["kind"] == "checkbox":
            try:
                locator.set_checked(bool(item["value"]), timeout=5_000)
            except PlaywrightError:
                # Case stylée (input caché sous un dessin) : l'événement est remis à la case elle-même.
                if locator.is_enabled() and locator.is_checked() != bool(item["value"]):
                    locator.dispatch_event("click")
        elif item["kind"] == "select":
            locator.select_option(label=item["value"])
        elif item["kind"] == "listbox":
            locator.click(timeout=5_000)
            option = root.page.get_by_role("option", name=str(item["value"]), exact=True)
            option.first.click(timeout=5_000)
            root.page.wait_for_timeout(300)
        elif item["kind"] == "radio":
            # Le plan contient le libellé visible validé par l'utilisateur,
            # jamais une valeur interne opaque propre au site.
            root.get_by_label(item["value"], exact=True).check()
        else:
            locator.fill(item["value"])
            # Quitter le champ comme une personne : le site revalide et retire son ancien message d'erreur.
            locator.evaluate("el => el.blur()")


def _verify_filled(root, plan: dict) -> list[dict]:
    """Relit le DOM après remplissage ; un écart interdit tout clic d'envoi."""
    _retag(root, plan)
    issues = []
    for item in plan["values"]:
        locator = root.locator(item["selector"]).first
        try:
            if item["kind"] == "file":
                names = locator.evaluate("el => Array.from(el.files || []).map(file => file.name)")
                # Workday envoie le fichier aussitôt, vide le champ et affiche son nom dans le formulaire.
                actual = Path(item["value"]).name in names or Path(item["value"]).name in root.inner_text()
            elif item["kind"] == "checkbox":
                actual = locator.is_checked()
            elif item["kind"] == "listbox":
                shown = " ".join(locator.inner_text().split())
                actual = str(item["value"]) if shown == str(item["value"]).strip() else shown
            elif item["kind"] == "select":
                # Lu dans le DOM : une liste masquée sous un widget de recherche n'a pas de texte « visible ».
                chosen = locator.evaluate("el => [...el.selectedOptions].map(option => option.text.trim())")
                actual = str(item["value"]) if str(item["value"]).strip() in chosen else ", ".join(chosen)
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


SUCCESS_WORDS = ("successfully", "uploaded", "avec succes", "telecharge", "televerse", "enregistre",
                 "saved")


def _validation_errors(page) -> list[str]:
    """Relève uniquement les messages de validation visibles et non vides, dans le formulaire de candidature.

    Hors du formulaire, un bandeau ``role="alert"`` peut afficher le titre de l'offre (Flatchr) : ce n'est
    pas une erreur de saisie.
    """
    messages = []
    marked = page.locator("[data-jobradar-form]")
    root = marked.first if marked.count() else page
    for selector in VALIDATION_SELECTORS:
        locator = root.locator(selector)
        for index in range(min(locator.count(), 20)):
            item = locator.nth(index)
            try:
                text = " ".join(item.inner_text(timeout=500).split())
                # « CV successfully uploaded » : une annonce de réussite (role="alert") n'est pas une erreur.
                if any(word in application_adapters._fold(text) for word in SUCCESS_WORDS):
                    continue
                if item.is_visible() and text and text not in messages:
                    messages.append(text[:300])
            except PlaywrightError:
                continue
    return messages


def _confirmed(page, body_text: str) -> bool:
    url = page.url.casefold()
    return any(word in body_text for word in CONFIRMATION_WORDS) or any(word in url for word in CONFIRMATION_URL_WORDS)


CAPTCHA_JS = r"""(selectors) => [...document.querySelectorAll(selectors)].some(el => {
  // Le badge reCAPTCHA « invisible » (Thales, Phenom…) n'attend aucune action de ta part.
  if (el.closest('.grecaptcha-badge') || /size=invisible/.test(el.src || '')
      || el.getAttribute('data-size') === 'invisible') return false;
  const box = el.getBoundingClientRect(), style = getComputedStyle(el);
  return box.width > 30 && box.height > 30 && style.visibility !== 'hidden' && style.display !== 'none';
})"""


def _captcha_visible(page) -> bool:
    try:
        return bool(page.evaluate(CAPTCHA_JS, ", ".join(BLOCK_SELECTORS[1:])))
    except PlaywrightError:
        return False


def _blocked(page, body_text: str) -> bool:
    """Détecte un vrai mur interactif, sans confondre un lien de pied de page."""
    if page.locator(BLOCK_SELECTORS[0]).count() or _captcha_visible(page):
        return True
    # Un écran de choix peut proposer « Log in » à côté de « From Device » : la connexion est alors
    # facultative et ne doit pas masquer la voie de candidature directe.
    if any(word in application_adapters._fold(body_text) for word in RESUME_METHOD_WORDS):
        return False
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


HUMAN_STEP_KINDS = {"login", "account_creation", "captcha"}
HUMAN_STEP_MESSAGES = {
    "login": "connecte-toi dans la fenêtre du navigateur",
    "account_creation": "crée ton compte dans la fenêtre du navigateur (et valide l'e-mail de confirmation si besoin)",
    "captcha": "résous la vérification dans la fenêtre du navigateur",
}


SIGNUP_WORDS = ("creer un compte", "creer mon compte", "create account", "create an account", "sign up",
                "inscription", "s inscrire", "register")


def _human_step_kind(page, visible: str) -> str:
    """Type d'étape humaine reconnu par les règles, pour un message précis à l'utilisateur."""
    folded = application_adapters._fold(visible)
    # « Protected by reCAPTCHA » en pied de page ne compte pas : il faut un défi affiché.
    if _captcha_visible(page) or any(words in folded for words in HUMAN_CHECK_WORDS):
        return "captcha"
    return "account_creation" if any(word in folded for word in SIGNUP_WORDS) else "login"


def _base_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return ".".join(host.split(".")[-2:])


def _wait_for_user(page, attempt: dict, provider: str, supervision, kind: str):
    """Étape humaine (connexion, compte, captcha, code e-mail) : JobRadar surveille et reprend seul.

    Renvoie (issue, page) : « ready » (la candidature est de nouveau accessible, éventuellement dans
    un nouvel onglet), « confirmed » (tu as terminé l'envoi toi-même), « closed » (onglet fermé) ou
    « timeout » (rien de concluant dans le délai : la candidature est mise de côté, onglet ouvert).
    """
    minutes = config.browser_settings()["human_wait_minutes"]
    deadline = time.monotonic() + minutes * 60
    filled = _prefill_known(page, supervision, login=kind == "login") if kind in {"login", "account_creation"} else []
    message = (f"{HUMAN_STEP_MESSAGES.get(kind, HUMAN_STEP_MESSAGES['login'])[:1].upper()}"
               f"{HUMAN_STEP_MESSAGES.get(kind, HUMAN_STEP_MESSAGES['login'])[1:]} : JobRadar reprend tout seul "
               f"dès que c'est fait (attente jusqu'à {minutes} min)."
               + (f" Déjà rempli pour toi : {', '.join(filled)}." if filled else ""))
    _claim_tab(page, attempt)
    notifications.application_intervention(attempt, {"kind": "waiting_user", "message": message,
                                                     "action": "open_browser"})
    supervision.journal.waiting(message, time.time() + minutes * 60)
    context = page.context
    domains = {_base_domain(page.url), _base_domain(attempt["target_url"])}
    known = {tab for tab in context.pages if tab is not page}  # onglets à toi (messagerie…) : ignorés
    seen: dict = {}  # onglet -> [empreinte de départ, dernière empreinte, relevés stables]
    try:
        while time.monotonic() < deadline:
            tabs = [tab for tab in context.pages if not tab.is_closed() and tab not in known]
            # Nouvel onglet du même site (lien de validation, connexion unique…) : suivi aussi.
            tabs = [tab for tab in tabs if tab is page or _base_domain(tab.url) in domains]
            if not tabs:
                return "closed", page
            tabs[-1].wait_for_timeout(2_000)
            for tab in reversed(tabs):
                if tab.is_closed():
                    continue
                try:
                    ready = _ready_tab(tab, provider)
                    _claim_tab(ready, attempt)
                    return "ready", ready
                except (application_adapters.NotApplicationForm, ValueError, PlaywrightError):
                    pass
                try:
                    current = application_supervisor.snapshot(tab)["hash"]
                    visible = tab.locator("body").inner_text(timeout=5000).casefold()
                except PlaywrightError:
                    continue
                state = seen.setdefault(tab, [current, current, 0])
                state[2] = state[2] + 1 if current == state[1] else 0
                state[1] = current
                # Page changée puis stable 4 s, sans mur de connexion visible : on regarde où on en est.
                if current == state[0] or state[2] < 2 or _blocked(tab, visible):
                    continue
                verdict = _classify_after_wait(tab, supervision, kind)
                if verdict in HUMAN_STEP_KINDS:
                    state[0] = current  # étape suivante de l'inscription : on continue d'attendre
                    _prefill_known(tab, supervision)
                    continue
                _claim_tab(tab, attempt)
                return verdict, tab
        return "timeout", page
    finally:
        supervision.journal.waiting(None)


PREFILL_KINDS = {"text", "email", "tel", "url"}
# Identifiant, mot de passe, code reçu : c'est à toi de les choisir ou de les saisir.
PREFILL_SKIP_WORDS = ("user", "login", "identifiant", "pseudo", "password", "mot de passe", "code", "otp", "pin")


def _prefill_known(page, supervision, *, login: bool = False) -> list[str]:
    """Avant de te passer la main (création de compte, connexion), remplit ce que JobRadar connaît déjà.

    Seuls les champs vides et visibles reçoivent une valeur exacte du profil (nom, e-mail, téléphone…) ;
    mot de passe, identifiant, cases à cocher et questions restent pour toi. Rien n'est validé.
    """
    try:
        page.locator("body").evaluate(TAG_CONTROLS_JS)
        soup = BeautifulSoup(page.content(), "html.parser")
    except PlaywrightError:
        return []
    secret = soup.select_one('input[type="password"]')
    root = (secret.find_parent("form") if secret is not None else None) or soup.body
    try:
        fields = application_adapters.adapter_for("generic")._fields(root) if root is not None else []
    except ValueError:
        return []
    def is_email(field) -> bool:  # l'e-mail sert souvent d'identifiant : il reste à pré-remplir
        return field.kind == "email" or any(word in application_adapters._fold(field.label)
                                            for word in ("e mail", "email", "courriel"))

    fields = [field for field in fields if field.kind in PREFILL_KINDS and not field.sensitive
              and (is_email(field) or not any(word in application_adapters._fold(f"{field.name} {field.label}").split()
                                              or word in application_adapters._fold(field.name)
                                              for word in PREFILL_SKIP_WORDS))]
    if login:  # une page de connexion ne demande que l'identifiant : l'e-mail, rien d'autre
        fields = [field for field in fields if is_email(field)]
    plan = application_adapters.fill_plan(fields, supervision.candidate, {})
    filled = []
    for item in plan["values"]:
        if not str(item["source"]).startswith("profil:"):
            continue
        locator = page.locator(item["selector"]).first
        try:
            if locator.is_visible() and locator.is_editable() and not locator.input_value():
                locator.fill(str(item["value"]))
                filled.append(" ".join(str(item["label"]).split())[:40])
        except PlaywrightError:
            continue
    if filled:
        supervision.journal.note("Champs connus pré-remplis avant de te passer la main : " + ", ".join(filled))
    return filled


def _ready_tab(tab, provider: str):
    match = _live_form(tab, provider)
    if any(field.kind == "password" for field in match.fields):
        raise ValueError("Page de compte : l'étape humaine n'est pas terminée.")
    return tab


def _classify_after_wait(tab, supervision, kind: str) -> str:
    """« ready », « confirmed » ou un type d'étape humaine encore en cours."""
    snap, decision = supervision.decide(tab, hints={"attente_utilisateur_terminee": kind})
    if decision is None:
        return "ready"  # sans IA, la disparition du mur de connexion suffit
    if decision["page_kind"] == "confirmation":
        return "confirmed"
    return decision["page_kind"] if decision["page_kind"] in HUMAN_STEP_KINDS else "ready"


def _after_human_step(attempt: dict, outcome: str, page, decision: dict | None = None) -> None:
    """Pose l'intervention correspondant à une attente qui n'a pas abouti à la reprise."""
    if outcome == "confirmed":
        _ai_intervention(attempt, "already_confirmed", decision,
                         "La page affiche une confirmation : tu as sans doute terminé l'envoi toi-même. "
                         "Marque la candidature « envoyée » dans le suivi.", action="verify")
        return
    resume_url = None if page.is_closed() else page.url
    message = ("L'onglet a été fermé avant la fin de l'étape. Une fois ton compte prêt, clique « Reprendre »."
               if outcome == "closed" else
               "Étape pas encore terminée (connexion, création de compte ou vérification). L'onglet reste ouvert : "
               "finis-la puis clique « Reprendre », JobRadar repartira de là.")
    _ai_intervention(attempt, "waiting_user", decision, message, action="resume", resume_url=resume_url)


TAB_KEY = "jobradar-candidature"


def _claim_tab(page, attempt: dict) -> None:
    """Marque l'onglet de la candidature et note son adresse : une reprise le retrouve au lieu d'en rouvrir un."""
    try:
        page.evaluate("([key, value]) => sessionStorage.setItem(key, value)",
                      [TAB_KEY, str(attempt["application_id"])])
        profile.atomic_write(_proof_directory(attempt) / "onglet.json",
                             json.dumps({"url": page.url}, ensure_ascii=False).encode())
    except (PlaywrightError, OSError, db.NotFound):
        pass


def _last_tab_url(attempt: dict) -> str | None:
    try:
        return json.loads((_proof_directory(attempt) / "onglet.json").read_text(encoding="utf-8")).get("url")
    except (OSError, ValueError, db.NotFound):
        return None


def _offer_tokens(url: str) -> set[str]:
    """Identifiants propres à une offre dans son adresse (« TGPTGWGLOBALR0343013… », « 1446216533 »…)."""
    parts = urlsplit(url)
    pieces = [*parts.path.split("/"), *(value for _, value in parse_qsl(parts.query))]
    tokens = set()
    for piece in pieces:
        for token in re.split(r"[-_.~]", unquote(piece)) if len(piece) > 40 else [unquote(piece)]:
            # Au moins 4 chiffres : « 257095 », « xlv0d9a66oppwelk » oui ; « etape-2 », « step-1 » non.
            if len(token) >= 6 and sum(char.isdigit() for char in token) >= 4:
                tokens.add(token.casefold())
    return tokens


APPLICATION_STEP_WORDS = ("apply", "application", "candidat", "postuler", "form", "step=")


def _resume_page(context, attempt: dict):
    """Onglet déjà ouvert pour cette candidature (attente, erreur, reprise) : on y continue sans doublon.

    Par ordre de priorité : l'onglet marqué par JobRadar pour cette candidature (à la dernière adresse
    connue d'abord), puis un onglet non marqué du même site dont l'adresse contient l'identifiant de
    l'offre (le plus avancé dans la candidature d'abord). Un onglet marqué pour une autre offre n'est
    jamais pris.
    """
    last = _last_tab_url(attempt)
    urls = [url for url in ((attempt.get("intervention") or {}).get("resume_url"), last,
                            attempt.get("target_url")) if url]
    if not urls:
        return None
    domains = {_base_domain(url) for url in urls}
    paths = {urlsplit(url)[:3] for url in urls}
    tokens = set().union(*(_offer_tokens(url) for url in urls))
    best = None
    for order, tab in enumerate(context.pages):
        try:
            if tab.is_closed() or _base_domain(tab.url) not in domains:
                continue
            owner = tab.evaluate("key => sessionStorage.getItem(key)", TAB_KEY)
        except PlaywrightError:
            continue
        address = unquote(tab.url).casefold()
        further = any(word in address for word in APPLICATION_STEP_WORDS)
        if owner == str(attempt["application_id"]):
            score = 4 if last and urlsplit(tab.url)[:3] == urlsplit(last)[:3] else 3
        elif owner is None and (any(token in address for token in tokens)
                                or (not tokens and urlsplit(tab.url)[:3] in paths)):
            # L'identifiant de l'offre fait foi : chez Thales, toutes les offres partagent « /global/en/apply ».
            score = 2 if further else 1
        else:
            continue
        if best is None or (score, order) > best[:2]:
            best = (score, order, tab)
    if best is None:
        return None
    best[2].bring_to_front()
    return best[2]


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


TAG_CONTROLS_JS = r"""root => {
  const clip = text => (text || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const controls = [...root.querySelectorAll('input,select,textarea')];
  controls.forEach((control, position) => {
    // Sans name ni id, un champ reste joignable par un repère tiré de sa position dans le formulaire :
    // reposé à l'identique après un nouveau rendu de la page (React), il désigne toujours le même champ.
    if (!control.name && !control.id) control.setAttribute('data-jobradar-field', String(position + 1));
  });
  for (const control of controls) {
    if ((control.type || '').toLowerCase() === 'hidden') continue;
    const labelled = (control.labels && control.labels.length) || control.getAttribute('aria-label')
      || control.getAttribute('placeholder') || control.getAttribute('aria-labelledby');
    if (!labelled) {
      // Zone de dépôt « Déposez votre CV » : le texte voisin tient lieu de libellé.
      let box = control.parentElement;
      for (let depth = 0; box && depth < 3 && !clip(box.innerText); depth++) box = box.parentElement;
      if (box && clip(box.innerText)) control.setAttribute('data-jobradar-label', clip(box.innerText));
    }
    if ((control.type || '').toLowerCase() !== 'checkbox') continue;
    // Question commune d'une liste de cases : libellé sans champ, dans un bloc qui ne contient que des cases.
    let box = control.parentElement;
    for (let depth = 0; box && box !== root.parentElement && depth < 6; depth++, box = box.parentElement) {
      const others = [...box.querySelectorAll('input,select,textarea')]
        .filter(el => !['checkbox', 'hidden'].includes((el.type || '').toLowerCase()));
      if (others.length) break;
      const title = [...box.querySelectorAll('label,legend,h2,h3,h4,p,span')]
        .find(el => !el.querySelector('input') && !el.contains(control)
                   && !(el.parentElement && el.parentElement.closest('label'))
                   && clip(el.innerText) && el.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING);
      if (title) { control.setAttribute('data-jobradar-group', clip(title.innerText)); break; }
    }
  }
}"""


HIDDEN_SKIPPED_KINDS = {"text", "textarea", "email", "tel", "url", "number", "date"}


def _mark_provider_form(page, provider: str) -> bool:
    """Workday, Greenhouse… : le formulaire reconnu par les sélecteurs de l'adaptateur devient la cible.

    Une étape « My Information » n'a ni CV ni couple e-mail + nom, mais reste le formulaire du fournisseur.
    """
    if provider == "generic":
        return False
    for selector in application_adapters.adapter_for(provider).form_selectors:
        target = page.locator(selector)
        try:
            if target.count() and target.first.is_visible():
                page.evaluate("() => document.querySelectorAll('[data-jobradar-form]')"
                              ".forEach(el => el.removeAttribute('data-jobradar-form'))")
                target.first.evaluate("el => el.setAttribute('data-jobradar-form', '')")
                return True
        except PlaywrightError:
            continue
    return False


def _marked_match(page):
    """Champs lus dans le conteneur effectivement marqué : Python et la page visent le même bloc."""
    container = page.locator("[data-jobradar-form]")
    if not container.count():
        return None
    container.first.evaluate(TAG_CONTROLS_JS)
    tag = BeautifulSoup(container.first.evaluate("el => el.outerHTML"), "html.parser").find()
    try:
        fields = application_adapters.adapter_for("generic")._fields(tag) if tag else []
    except ValueError:
        fields = []  # que des listes maison : lues juste après
    # Le HTML peut décrire des champs absents de la page vivante (gabarits) : ils ne sont pas remplissables.
    # Un champ de texte caché est une variante non choisie (« coller le CV » à côté du dépôt de fichier) ;
    # fichiers, cases et listes restent, souvent masqués sous un habillage personnalisé.
    fields = [field for field in fields if container.first.locator(field.selector).count()
              and (field.kind not in HIDDEN_SKIPPED_KINDS or container.first.locator(field.selector).first.is_visible())]
    fields += _listbox_fields(page, container.first)
    return application_adapters.FormMatch(fields, "[data-jobradar-form]") if fields else None


LISTBOX_JS = r"""root => [...root.querySelectorAll('button[aria-haspopup="listbox"],[role="combobox"][aria-haspopup="listbox"]')]
  .filter(el => el.getClientRects().length > 0).map((el, index) => {
    el.setAttribute('data-jobradar-listbox', String(index + 1));
    const box = el.closest('[data-automation-id^="formField"],fieldset,.form-group') || el.parentElement;
    const label = box ? (box.querySelector('label,legend')?.innerText || '') : '';
    const required = /\*/.test(label) || el.getAttribute('aria-required') === 'true';
    return {index: index + 1, label: label.replace(/\s+/g, ' ').trim(), required, current: el.innerText.trim()};
  })"""


LISTBOX_CONTAINER_JS = r"""() => {
  const lists = [...document.querySelectorAll('button[aria-haspopup="listbox"]')].filter(el => el.getClientRects().length);
  if (!lists.length) return false;
  const forms = new Set(lists.map(el => el.closest('form')));
  let target = forms.size === 1 && !forms.has(null) ? [...forms][0] : lists[0].parentElement;
  while (target && !lists.every(el => target.contains(el))) target = target.parentElement;
  const action = 'button:not([aria-haspopup]),input[type=submit]';
  while (target && target !== document.body && !target.querySelector(action)) target = target.parentElement;
  if (!target) return false;
  document.querySelectorAll('[data-jobradar-form]').forEach(el => el.removeAttribute('data-jobradar-form'));
  target.setAttribute('data-jobradar-form', '');
  return true;
}"""


def _listbox_form(page):
    """Étape faite seulement de listes maison (questions Workday « Select One ») : le bloc qui les contient."""
    try:
        if not page.evaluate(LISTBOX_CONTAINER_JS):
            return None
        match = _marked_match(page)
    except (PlaywrightError, ValueError):
        return None
    return match if match and any(field.kind == "listbox" for field in match.fields) else None


def _listbox_fields(page, root) -> list:
    """Listes maison (Workday : bouton « Select One » qui ouvre une liste) : options lues en l'ouvrant un instant."""
    try:
        found = root.evaluate(LISTBOX_JS)
    except PlaywrightError:
        return []
    fields = []
    for item in found:
        if not item["label"]:
            continue
        selector = f'[data-jobradar-listbox="{item["index"]}"]'
        button = root.locator(selector).first
        try:
            button.click(timeout=3_000)
            page.wait_for_timeout(400)
            options = [text.strip() for text in page.locator('[role="option"]').all_inner_texts() if text.strip()]
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except PlaywrightError:
            continue
        label = item["label"]
        folded = application_adapters._fold(label)
        sensitive = any(term in folded for term in application_adapters.SENSITIVE_TERMS)
        # Déjà choisi sur le site (autre que l'invite « Select One ») : la liste compte comme remplie.
        answered = item["current"] and application_adapters._fold(item["current"]) not in {"select one", "choisir",
                                                                                           "selectionner", ""}
        fields.append(application_adapters.Field(
            selector, f"listbox-{item['index']}", label, "listbox", bool(item["required"]) and not answered,
            tuple(options), sensitive, None,
            application_adapters.classify_text_field("select", selector, label, sensitive)))
    return fields


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


def _choose_resume_method(page, files: dict) -> bool:
    """Franchit un écran de choix de méthode (ex. L'Oréal) en important le CV.

    Ces pages ne contiennent pas encore de formulaire et leurs actions sont souvent des boutons sans
    ``href``. On ne choisit jamais la connexion, Indeed ou une candidature sans CV. Si le sélecteur de
    fichier ou la navigation ne réagit pas, le parcours normal reprend et s'arrête de façon contrôlée.
    """
    cv = files.get("cv")
    if not cv:
        return False
    candidates = page.locator("button, a, [role=button]")
    for index in range(min(candidates.count(), 100)):
        item = candidates.nth(index)
        try:
            text = application_adapters._fold(" ".join(item.inner_text(timeout=250).split()))
            if not item.is_visible() or not any(word in text for word in RESUME_METHOD_WORDS):
                continue
            existing = page.locator('input[type="file"]').first
            if existing.count():
                existing.set_input_files(cv)
                page.wait_for_timeout(500)
                return True
            try:
                with page.expect_file_chooser(timeout=3_000) as chooser:
                    item.click(timeout=5_000)
                chooser.value.set_files(cv)
            except PlaywrightError:
                # Certains sites révèlent un input caché au lieu d'ouvrir le sélecteur natif.
                upload = page.locator('input[type="file"]').first
                if upload.count():
                    upload.set_input_files(cv)
                else:
                    return True  # le clic a pu naviguer vers l'étape suivante
            page.wait_for_timeout(500)
            return True
        except PlaywrightError:
            continue
    return False


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
        self.seen_frames: set[str] = set()  # cadres intégrés déjà ouverts directement : jamais deux fois
        self.candidate = candidate
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


COUNT_CONTROLS_JS = r"""() => [...document.querySelectorAll('input,select,textarea')].filter(el =>
  !['hidden', 'submit', 'button'].includes((el.type || '').toLowerCase())
  && (el.getClientRects().length > 0 || (el.type || '').toLowerCase() === 'file')).length"""


def _promote_form_frame(page, seen: set[str]) -> str | None:
    """Formulaire affiché dans un cadre intégré (iCIMS…) : l'onglet ouvre directement l'adresse du cadre.

    Le formulaire devient alors la page elle-même, et tout le parcours (instantané de l'IA, remplissage,
    relecture, envoi protégé) s'applique sans changement. Les cadres de captcha ne sont jamais suivis.
    Renvoie « promoted », « login » (cadre déjà ouvert une fois qui demande un mot de passe : le site se
    ré-encadre, c'est une connexion à faire par toi) ou ``None``.
    """
    try:
        if page.evaluate(COUNT_CONTROLS_JS) >= 2:
            return None
        for frame in page.frames[1:]:
            url = frame.url or ""
            if not url.startswith("http") or "captcha" in url.casefold() or "challenges.cloudflare" in url:
                continue
            if frame.evaluate(COUNT_CONTROLS_JS) < 3 and not frame.locator('input[type="password"]').count():
                continue
            key = url.split("#")[0]
            if key in seen:
                if frame.locator('input[type="password"]').count():
                    return "login"
                continue
            seen.add(key)
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return "promoted"
    except PlaywrightError:
        return None
    return None


def _navigation_click(page, target) -> None:
    """Clic de navigation (ouvrir, fermer, cookies, suivant), jamais l'envoi final.

    Une fenêtre surgissante qui intercepte les clics (Welcome to the Jungle) est d'abord fermée par
    Échap ; en dernier recours, l'événement est remis directement à l'élément visé.
    """
    try:
        target.click(timeout=5_000)
        return
    except PlaywrightError:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    try:
        target.click(timeout=3_000)
    except PlaywrightError:
        target.dispatch_event("click", timeout=3_000)


def _clickable(page, button) -> bool:
    """Essai à blanc du bouton d'envoi : une fenêtre qui l'intercepte est fermée par Échap, sinon on s'arrête."""
    for attempt in range(2):
        try:
            button.click(trial=True, timeout=5_000)
            return True
        except PlaywrightError:
            if not attempt:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
    return False


FORM_KINDS = {"application_form", "multi_step_form"}
AI_CONTAINER_JS = r"""(ids) => {
  const fields = 'input:not([type=hidden]),select,textarea';
  const controls = ids.map(id => document.querySelector(`[data-jobradar-id="${id}"]`)).filter(Boolean);
  if (!controls.length) return false;
  // Le dépôt du CV est souvent un champ caché que l'IA ne désigne pas : le bloc l'englobe aussi.
  controls.push(...document.querySelectorAll('input[type=file]'));
  const forms = new Set(controls.map(el => el.closest('form')));
  let target = forms.size === 1 && !forms.has(null) ? [...forms][0] : controls[0].parentElement;
  while (target && !controls.every(el => target.contains(el))) target = target.parentElement;
  const action = 'button,[role=button],input[type=submit],input[type=button]';
  while (target && target !== document.body && !target.querySelector(action)) target = target.parentElement;
  if (!target || !target.querySelector(fields) || !target.getClientRects().length) return false;
  document.querySelectorAll('[data-jobradar-form]').forEach(el => el.removeAttribute('data-jobradar-form'));
  target.setAttribute('data-jobradar-form', '');
  return true;
}"""


def _ai_form(page, decision: dict | None, snap: dict | None = None):
    """Formulaire que l'IA reconnaît mais pas les règles (libellés atypiques, composants maison).

    Le bloc retenu englobe les champs et le bouton désignés par l'IA dans l'instantané qu'elle vient de
    lire ; la suite (plan de remplissage, relecture, envoi protégé) est exactement celle des règles.
    """
    if not decision or decision["page_kind"] not in FORM_KINDS:
        return None
    actions = decision["actions"]
    if any(item["type"] == "click" and item["purpose"] == "open_application" for item in actions):
        return None  # la candidature est encore à ouvrir (fenêtre fermée, bouton « Postuler »)
    ids = [item["id"] for item in actions if item["type"] in {"fill", "upload", "select", "check"}]
    if not ids and snap:
        # Rien que l'IA puisse remplir (question sensible seule, « Gender ») : les champs obligatoires visibles
        # forment le bloc, pour que la question te soit posée au lieu d'un arrêt « pas un formulaire ».
        ids = [item["id"] for item in snap["elements"] if item.get("required")
               and item["tag"] in {"input", "select", "textarea"} and item.get("type") != "file"]
    if not ids:
        return None
    ids += [item["id"] for item in actions if item["type"] == "click" and item["purpose"] in {"submit", "next_step"}]
    try:
        if not page.evaluate(AI_CONTAINER_JS, ids):
            return None
        return _marked_match(page)
    except (PlaywrightError, ValueError):
        return None


def _ai_navigate(page, attempt: dict, provider: str, supervision: _Supervision, hints: dict):
    """L'IA pilote l'ouverture de la candidature jusqu'à un formulaire reconnu.

    Renvoie (fournisseur, formulaire, page), ``None`` si une intervention a été posée, ou ``UNRESOLVED``
    si l'IA est indisponible.
    """
    from . import application_flow  # évite une dépendance circulaire au chargement
    login_attempted = False
    stalled = loading_waited = False
    for _ in range(application_supervisor.MAX_DECISIONS):
        try:
            return provider, _live_form(page, provider), page
        except (application_adapters.NotApplicationForm, ValueError):
            pass
        listboxes = _listbox_form(page)
        if listboxes is not None:
            supervision.journal.note("Questions à choix (listes déroulantes du site) : remplissage.")
            return provider, listboxes, page
        framed = _promote_form_frame(page, supervision.seen_frames)
        if framed == "promoted":
            supervision.journal.note(f"Formulaire affiché dans un cadre intégré : ouverture directe de {page.url}")
            _claim_tab(page, attempt)
            _wait_for_application_ui(page, provider, timeout_ms=8_000)
            continue
        if framed == "login":
            supervision.journal.note("Connexion demandée dans un cadre intégré.")
            outcome, page = _wait_for_user(page, attempt, provider, supervision, "login")
            if outcome == "ready":
                supervision.journal.note("Étape terminée de ton côté : reprise automatique.")
                supervision.last_hash = None
                continue
            _after_human_step(attempt, outcome, page)
            return None
        snap, decision = supervision.decide(page, hints=hints)
        if decision is None:
            return UNRESOLVED
        if len(snap["elements"]) < 5 and not loading_waited:
            # Page encore en construction (application Workday après rechargement) : on la relit une fois chargée.
            loading_waited = True
            supervision.journal.note("Page presque vide : attente de la fin du chargement.")
            _settle(page)
            page.wait_for_timeout(3_000)
            continue
        if snap["hash"] == supervision.last_hash and not stalled:
            # Clic parti avant que la page soit prête, ou masqué par un bandeau : une seconde chance.
            stalled = True
            cookie = _dismiss_cookies(page)
            supervision.journal.note("Le clic n'a rien changé : " + (f"bandeau « {cookie} » fermé, " if cookie else "")
                                     + "nouvel essai après une courte attente.")
            page.wait_for_timeout(3_000)
            hints = hints | {"clic_precedent_sans_effet": True}
            supervision.last_hash = None
            continue
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
            outcome, page = _wait_for_user(page, attempt, provider, supervision, kind)
            if outcome == "ready":
                supervision.journal.note("Étape terminée de ton côté : reprise automatique.")
                supervision.last_hash = None
                continue
            _after_human_step(attempt, outcome, page, decision)
            return None
        clicks = [item for item in decision["actions"]
                  if item["type"] == "click" and item["purpose"] in application_supervisor.NAVIGATION_PURPOSES]
        match = _ai_form(page, decision, snap) if kind in FORM_KINDS else None
        if match is not None:
            supervision.journal.decision(page, snap, decision, ["formulaire reconnu par l'IA : remplissage"])
            supervision.last_hash = None
            return provider, match, page
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
                _navigation_click(page, target)
            except PlaywrightError as exc:
                executed.append(f"clic n°{item['id']} ({item['purpose']}) impossible : {exc.message[:120]}")
                continue
            executed.append(f"clic n°{item['id']} ({item['purpose']}) : {item['reason']}")
            page.wait_for_timeout(500)
            opened = set(page.context.pages) - before
            if opened:
                page = opened.pop()
                page.wait_for_load_state("domcontentloaded", timeout=45_000)
                _claim_tab(page, attempt)
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
        if _promote_form_frame(page, supervision.seen_frames) == "promoted":
            supervision.journal.note(f"Étape {step + 1} dans un cadre intégré : ouverture directe de {page.url}")
            _wait_for_application_ui(page, provider, timeout_ms=8_000)
            try:
                return provider, _live_form(page, provider), page
            except (application_adapters.NotApplicationForm, ValueError):
                pass
        listboxes = _listbox_form(page)
        if listboxes is not None:
            supervision.journal.note(f"Étape {step + 1} : questions à choix (listes déroulantes du site).")
            return provider, listboxes, page
        snap, decision = supervision.decide(page, hints={"etape_du_formulaire": step + 1})
        match = _ai_form(page, decision, snap)
        if match is not None:
            supervision.journal.decision(page, snap, decision, [f"étape {step + 1} reconnue par l'IA"])
            return provider, match, page
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
        elif action["type"] == "fill" and action["source"] == "lettre":
            value = application_adapters.full_letter(cache.get("letter_paragraphs"), field.get("maxlength"))
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
    page = _resume_page(context, attempt)
    resumed = page is not None
    if (resumed and (attempt.get("intervention") or {}).get("kind") != "resume"
            and "myworkdayjobs" not in page.url):
        # Reprise après une erreur : le formulaire repart propre, sans les saisies d'une tentative précédente.
        # Après une étape faite par toi (connexion, compte), ou sur Workday dont le parcours en plusieurs
        # étapes se perdrait, l'onglet est gardé tel quel.
        try:
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            _settle(page)
        except PlaywrightError:
            pass
    if not resumed:
        page = context.new_page()
        page.goto(attempt["target_url"], wait_until="domcontentloaded", timeout=45_000)
    _claim_tab(page, attempt)
    supervision = _Supervision(attempt, candidate, files)
    supervision.journal.note(f"{'Reprise dans l’onglet déjà ouvert' if resumed else 'Ouverture'} de {page.url} "
                             f"(supervision IA : {supervision.mode})")
    collected: dict[str, dict] = {}
    final_submit = None
    login_attempted = False
    # Une page générique peut mener, via « Postuler », à un formulaire Workday en plusieurs étapes.
    max_steps = WORKDAY_MAX_STEPS if provider in {"workday", "generic"} else 1
    for step in range(max_steps):
        if step == 0 and not resumed:
            if _choose_resume_method(page, files):
                supervision.journal.note("Méthode de candidature avec CV sélectionnée automatiquement.")
                _wait_for_application_ui(page, provider, timeout_ms=8_000)
        visible = page.locator("body").inner_text(timeout=5000).casefold()
        if _blocked(page, visible):
            logged_in = False
            if not login_attempted:
                login_attempted = True
                logged_in = _try_login(page)
            if not logged_in:
                outcome, page = _wait_for_user(page, attempt, provider, supervision, _human_step_kind(page, visible))
                if outcome != "ready":
                    _after_human_step(attempt, outcome, page)
                    return None
                supervision.journal.note("Étape terminée de ton côté : reprise automatique.")
        located = _locate(page, attempt, provider, step, supervision)
        if located is None:
            return None
        provider, match, page = located
        _claim_tab(page, attempt)
        if any(field.kind == "password" for field in match.fields):
            # Workday et d'autres demandent un compte au milieu du parcours : étape humaine, puis reprise.
            visible = page.locator("body").inner_text(timeout=5000).casefold()
            outcome, page = _wait_for_user(page, attempt, provider, supervision, _human_step_kind(page, visible))
            if outcome != "ready":
                _after_human_step(attempt, outcome, page)
                return None
            supervision.journal.note("Étape terminée de ton côté : reprise automatique.")
            continue
        # Remplissage, relecture et envoi restent confinés au formulaire de candidature reconnu :
        # un formulaire de recherche ou de newsletter de la même page n'est jamais touché.
        # Un bloc déjà marqué qui contient tous les champs prévus reste la cible : chez Thales, le dépôt
        # du CV est hors de la balise <form> que les règles marqueraient à sa place.
        marked = page.locator("[data-jobradar-form]")
        keep = bool(marked.count()) and all(marked.first.locator(field.selector).count() for field in match.fields)
        if not keep and not _mark_application_container(page) and not _mark_provider_form(page, provider) \
                and not marked.count():
            _intervention(attempt["id"], "not_application_form",
                          "Le formulaire détecté a disparu avant le remplissage. Rien n'a été envoyé.")
            return None
        form = page.locator(match.selector).first
        plan = application_adapters.fill_plan(match.fields, candidate, files, ai_metadata=True)
        plan = _paste_resume(plan, files)
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
        if any(item["kind"] == "file" for item in plan["values"]):
            _settle(page)  # le site analyse souvent le CV déposé avant d'accepter la suite (Phenom, Workday)
        verification_issues = _verify_filled(form, plan)
        if verification_issues:
            # Champ vidé par un nouveau rendu de la page : une seule nouvelle saisie des champs en écart.
            labels = {issue["label"] for issue in verification_issues}
            page.wait_for_timeout(1_000)
            _fill(form, {"values": [item for item in plan["values"] if item["label"] in labels]})
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
        if validation_errors and _retry_phone_national(form, page, plan, validation_errors):
            supervision.journal.note("Téléphone refusé au format international : ressaisi au format 0xxxxxxxxx.")
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
                before = application_supervisor.snapshot(page)["hash"]
                designated[1].click(timeout=10_000)
                page.wait_for_timeout(1500)
                if application_supervisor.snapshot(page)["hash"] == before:
                    # Le site finissait peut-être un traitement (analyse du CV) : un second essai, plus tard, sur
                    # le bouton « Next / Suivant » visible de la page (l'IA a pu désigner un bouton voisin).
                    _settle(page)
                    page.wait_for_timeout(3_000)
                    named = page.get_by_role("button", name=NEXT_LABELS)
                    retry = next((named.nth(index) for index in range(named.count()) if named.nth(index).is_visible()),
                                 designated[1] if designated[1].count() else None)
                    if retry is not None:
                        retry.click(timeout=10_000)
                        page.wait_for_timeout(2_000)
                if application_supervisor.snapshot(page)["hash"] == before:
                    errors = _validation_errors(page)
                    _intervention(attempt["id"], "page_validation_failed",
                                  "« Suivant » n'a pas fait avancer le formulaire. Aucun envoi n'a été effectué.",
                                  fields=[{"label": message, "reason": "erreur affichée par le site"}
                                          for message in errors] or
                                         [{"label": "Aucun message d'erreur visible", "reason": "page inchangée"}],
                                  preview=_preview(combined), action="verify")
                    return None
                supervision.journal.note(f"Étape {step + 1} remplie, passage à l'étape suivante.")
                continue
            if designated and designated[0] == "submit":
                final_submit = designated[1]
            plan = combined
            break
        navigation, is_final = _workday_navigation(page)
        if navigation is None and designated:
            # Bouton hors des repères Workday habituels : celui que l'IA a désigné dans le formulaire.
            navigation, is_final = designated[1], designated[0] == "submit"
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
    if not _clickable(page, submit):
        _intervention(attempt["id"], "submit_blocked",
                      "Une fenêtre recouvre le bouton d'envoi. Rien n'a été envoyé : ferme-la puis clique « Reprendre ».",
                      preview=_preview(plan), action="resume")
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
