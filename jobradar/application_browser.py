"""Exécution Playwright des adaptateurs de candidature, avec arrêt sûr avant envoi."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import ai, application_adapters, archive, capture, config, db, notifications, profile, tailoring

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


LOGGER = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    pass


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


def process(attempt: dict) -> None:
    """Remplit, vérifie et soumet ; toute ambiguïté devient ``needs_user``."""
    provider = attempt["provider"]
    if provider not in application_adapters.SUPPORTED:
        _intervention(attempt["id"], "unsupported_form", "Ce formulaire nécessite le mode manuel.",
                      action="manual_fallback")
        return
    if not _headed_available():
        raise BrowserUnavailable("Aucune session graphique n'est disponible pour le navigateur de candidature.")
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
    browser_lock = capture._profile_lock(config.browser_profile_dir())
    if not browser_lock.acquire(timeout=5):
        raise BrowserUnavailable("Le navigateur JobRadar est occupé ; la candidature sera reprise plus tard.")
    outcome = None
    try:
        config.browser_profile_dir().mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                config.browser_profile_dir(), headless=False, accept_downloads=False,
                permissions=[], service_workers="block", viewport={"width": 1365, "height": 900},
            )
            # Le contexte est fermé AVANT la sortie du bloc : après elle, Playwright est arrêté et
            # la fermeture échouerait (« Event loop is closed »), en masquant un envoi réussi.
            try:
                outcome = _run_session(context, attempt, provider, candidate, files, approved)
            finally:
                _close_quietly(context)
    except (PlaywrightError, OSError, ValueError) as exc:
        if submit_was_clicked(attempt["id"]):
            submission_unconfirmed(attempt["id"])
            return
        raise BrowserUnavailable(str(exc)) from exc
    finally:
        browser_lock.release()
    if outcome == "submitted":
        from . import application_flow  # évite une dépendance circulaire au chargement
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


def _application_form(page, attempt: dict, provider: str, *, follow_link: bool):
    """Renvoie (fournisseur, formulaire) ; sur une page d'offre, suit une fois le lien « Postuler ».

    Si aucun formulaire de candidature n'est reconnu, rien n'est rempli ni envoyé : une intervention
    propose le mode manuel et la fonction renvoie ``None``.
    """
    try:
        return provider, application_adapters.adapter_for(provider).locate(page.content())
    except application_adapters.NotApplicationForm:
        link = application_adapters.find_apply_link(page.content()) if follow_link else None
    if link:
        from . import application_flow  # évite une dépendance circulaire au chargement
        page.goto(urljoin(page.url, link), wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1000)
        destination = application_flow.provider_for(page.url)
        if destination in application_flow.MANUAL_ONLY_HOSTS:
            _intervention(attempt["id"], "manual_only_destination",
                          "Le lien « Postuler » mène à LinkedIn ou Indeed, disponibles uniquement en mode manuel. "
                          "Rien n'a été rempli ni envoyé.", action="manual_fallback")
            return None
        return _application_form(page, attempt, destination, follow_link=False)
    _intervention(attempt["id"], "not_application_form",
                  "Cette page présente l'offre mais ne contient pas de formulaire de candidature reconnu "
                  "(champ CV, ou e-mail et nom). Rien n'a été rempli ni envoyé : passe en mode manuel.",
                  action="manual_fallback")
    return None


def _run_session(context, attempt: dict, provider: str, candidate: dict, files: dict, approved: bool) -> str | None:
    """Parcourt le formulaire ; renvoie « submitted » si l'envoi est confirmé, sinon une intervention est posée."""
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(attempt["target_url"], wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(1000)
    collected: dict[str, dict] = {}
    final_submit = None
    # Une page générique peut mener, via « Postuler », à un formulaire Workday en plusieurs étapes.
    max_steps = WORKDAY_MAX_STEPS if provider in {"workday", "generic"} else 1
    for step in range(max_steps):
        visible = page.locator("body").inner_text(timeout=5000).casefold()
        if _blocked(page, visible):
            if not _wait_for_human(page, attempt):
                _intervention(attempt["id"], "authentication_or_captcha",
                              "Le délai d'intervention est dépassé. Reprends la session quand tu es disponible.",
                              action="open_browser")
                return None
        located = _application_form(page, attempt, provider, follow_link=step == 0)
        if located is None:
            return None
        provider, match = located
        # Remplissage, relecture et envoi restent confinés au formulaire de candidature reconnu :
        # un formulaire de recherche ou de newsletter de la même page n'est jamais touché.
        form = page.locator("form").nth(match.index)
        plan = application_adapters.fill_plan(match.fields, candidate, files)
        if provider == "generic":
            try:
                plan = application_adapters.generic_ai_plan(plan, candidate)
            except ai.AIError:
                # Le quota ou l'assistant indisponible ne doit jamais bloquer le
                # repli : les champs restent proposés directement à l'utilisateur.
                pass
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
        submission_unconfirmed(attempt["id"])
        return None
    try:
        page.screenshot(path=str(proof / "confirmation.png"), full_page=True)
        profile.atomic_write(proof / "session.json", json.dumps(
            {"attempt_id": attempt["id"], "provider": provider, "confirmation_url": page.url},
            ensure_ascii=False, indent=2,
        ).encode())
    except (PlaywrightError, OSError):
        LOGGER.warning("Preuve de confirmation non enregistrée pour la session %s", attempt["id"], exc_info=True)
    return "submitted"
