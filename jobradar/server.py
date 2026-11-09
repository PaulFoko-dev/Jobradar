"""Serveur web local (http://127.0.0.1:8765)."""
import base64
import binascii
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from . import (
    ai,
    analytics,
    application_flow,
    application_queue,
    archive,
    auth,
    config,
    contacts,
    db,
    export,
    inbox,
    interview_prep,
    interview_simulation,
    login_credentials,
    mailer,
    notifications,
    pipeline,
    preparation,
    private_io,
    profile,
    profile_coach,
    tailoring,
    today,
    vault,
    workflow,
)
from .config import load_config, settings

STATIC = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Migration et configuration au démarrage effectif du serveur, jamais à l'import :
    # un simple `import jobradar.server` (outil, test, vérification) ne doit pas toucher aux données.
    db.init()
    load_config()
    try:
        merged = application_flow.merge_duplicate_applications()
        if merged:
            LOGGER.info("Candidatures en double fusionnées : %s", merged)
    except Exception:
        LOGGER.warning("Fusion des candidatures en double reportée (coffre fermé ?)", exc_info=True)
    try:
        archive.start_queue()  # file de capture, rattrapage des archives manquantes, reprise des captures
    except Exception:
        LOGGER.exception("Archives des candidatures indisponibles ; le reste de JobRadar fonctionne.")
    try:
        application_queue.start_queue()
    except Exception:
        LOGGER.exception("File des candidatures assistées indisponible ; le mode manuel reste utilisable.")
    try:
        preparation.start_queue()  # reprend les préparations interrompues par un arrêt
    except Exception:
        LOGGER.exception("File des préparations de documents indisponible.")
    yield
    preparation.stop_queue()
    application_queue.stop_queue()
    archive.stop_queue()


app = FastAPI(title="JobRadar", lifespan=lifespan)

@app.exception_handler(vault.VaultError)
async def vault_locked(_request: Request, exc: vault.VaultError):
    """Coffre fermé ou clé introuvable : message clair (423) au lieu d'une erreur serveur."""
    return JSONResponse({"detail": f"Coffre verrouillé : {exc} Saisis ta clé de récupération pour accéder à tes "
                                   "données (rechargement de la page)."}, status_code=423)


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def local_requests_only(request: Request, call_next):
    """Une requête qui modifie des données doit viser JobRadar en local et venir de sa propre page.

    Bloque une page web malveillante (Origin étrangère) et le DNS rebinding (Host étranger).
    """
    if request.method not in SAFE_METHODS:
        host = urlsplit(f"//{request.headers.get('host') or ''}").hostname or ""
        origin = request.headers.get("origin")
        origin_host = urlsplit(origin).hostname if origin and origin != "null" else None
        if host not in LOCAL_HOSTS or (origin is not None and origin_host not in LOCAL_HOSTS):
            return JSONResponse({"detail": "Requête refusée : elle ne vient pas de JobRadar en local."},
                                status_code=403)
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/") and auth.account_exists() \
            and auth.session_user(request.cookies.get(auth.COOKIE)) is None:
        return JSONResponse({"detail": "Connexion requise."}, status_code=401)
    return await call_next(request)


class AuthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(max_length=256)
    new: str = Field(min_length=1, max_length=256)


def _with_session(token: str | None) -> JSONResponse:
    response = JSONResponse({"account": True, "authenticated": token is not None, "username": auth.username()})
    if token is None:
        response.delete_cookie(auth.COOKIE, path="/")
    else:
        response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", path="/")
    return response


@app.get("/api/auth/status")
def auth_status(request: Request):
    user = auth.session_user(request.cookies.get(auth.COOKIE))
    return {"account": auth.account_exists(), "authenticated": user is not None, "username": user}


@app.post("/api/auth/setup")
def auth_setup(body: AuthIn):
    try:
        return _with_session(auth.create_account(body.username.strip(), body.password))
    except auth.AuthError as exc:
        raise HTTPException(409 if auth.account_exists() else 400, str(exc))


@app.post("/api/auth/login")
def auth_login(body: AuthIn):
    try:
        return _with_session(auth.login(body.username.strip(), body.password))
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc))


@app.post("/api/auth/logout")
def auth_logout():
    return _with_session(None)


@app.put("/api/auth/password")
def auth_password(body: PasswordIn, request: Request):
    if auth.session_user(request.cookies.get(auth.COOKIE)) is None:
        raise HTTPException(401, "Connexion requise.")
    try:
        return _with_session(auth.change_password(body.current, body.new))
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc))


def _private_file(path: Path, media_type: str, *, inline: bool) -> Response:
    """Fichier privé (éventuellement chiffré au repos) servi déchiffré, sous son vrai nom."""
    disposition = "inline" if inline else "attachment"
    return Response(private_io.read_bytes(path), media_type=media_type, headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}",
        "X-Content-Type-Options": "nosniff"})


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: StrictBool


class RecoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_key: str = Field(min_length=20, max_length=200)


@app.get("/api/profile/coach")
def profile_coach_review():
    return profile_coach.review(profile.load_profile())


@app.post("/api/profile/coach/{item_id}/enrich")
def profile_coach_enrich(item_id: str, body: dict):
    try:
        return profile_coach.enrich(item_id, body.get("answers") if isinstance(body, dict) else None)
    except (profile_coach.CoachError, tailoring.CompositionError, profile.ProfileError) as exc:
        raise HTTPException(400, str(exc))
    except ai.AIError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/profile/coach/merge")
def profile_coach_merge(body: dict):
    try:
        return profile_coach.merge(str(body.get("keep") or ""), str(body.get("drop") or ""))
    except (profile_coach.CoachError, profile.ProfileError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/profile/coach/link-skills")
def profile_coach_link_skills():
    with profile.LOCK:
        data = profile.load_profile()
        linked = profile_coach.link_skills(data)
        return profile.save_profile(data) | {"linked": linked}


@app.post("/api/profile/answers/{key}/confirm")
def confirm_profile_answer(key: str):
    try:
        return profile.confirm_answer(key)
    except profile.ProfileError as exc:
        raise HTTPException(404, str(exc))


class InboxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    imap_host: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9.-]*$")
    days: StrictInt = Field(default=30, ge=1, le=120)


@app.get("/api/inbox")
def inbox_settings():
    return inbox.settings()


@app.put("/api/inbox")
def put_inbox_settings(body: InboxIn):
    config.save_config_section("inbox", body.model_dump())
    return inbox.settings()


@app.get("/api/today")
def today_summary():
    return today.summary()


@app.get("/api/analytics/robot")
def robot_analytics():
    return analytics.robot_statistics()


@app.post("/api/inbox/scan")
def inbox_scan():
    """Lecture locale de la boîte de réception (réponses, accusés) ; rien n'est envoyé à l'IA."""
    try:
        return inbox.scan()
    except (RuntimeError, OSError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/vault")
def vault_status():
    return vault.status()


@app.post("/api/vault/init")
def vault_init():
    try:
        return {"recovery_key": vault.init()} | vault.status()
    except vault.VaultError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/vault/confirm-recovery")
def vault_confirm_recovery():
    return vault.confirm_recovery()


@app.post("/api/vault/unlock")
def vault_unlock(body: RecoveryIn):
    try:
        vault.unlock_with_recovery(body.recovery_key)
    except vault.VaultError as exc:
        raise HTTPException(400, str(exc))
    return vault.status()


@app.post("/api/vault/migrate")
def vault_migrate():
    """Chiffre tout l'existant (colonnes privées, profil, candidatures) ; reprenable, sans effet s'il est fait."""
    if not vault.status()["unlocked"]:
        raise HTTPException(409, "Coffre fermé : déverrouille-le avant de chiffrer tes données.")
    if not vault.status()["recovery_confirmed"]:
        raise HTTPException(409, "Confirme d'abord avoir sauvegardé ta clé de récupération.")
    values = db.encrypt_existing()
    files = private_io.seal_tree()
    return {"values": values, "files": files} | vault.status()


@app.get("/api/vault/plaintext-backups")
def vault_plaintext_backups():
    return export.plaintext_backups(data_dir=config.DATA_DIR)


@app.delete("/api/vault/plaintext-backups")
def vault_delete_plaintext_backups():
    return {"deleted": export.delete_plaintext_backups(data_dir=config.DATA_DIR)}


@app.post("/api/vault/lock")
def vault_lock():
    vault.lock()
    return vault.status()


@app.put("/api/vault/consent")
def vault_consent(body: ConsentIn):
    return vault.set_cv_consent(body.consent)

ApplicationStatus = Literal[
    "consultee", "envoyee", "relance", "entretien_rh", "entretien_tech", "retenu", "refuse", "abandon"
]


_VALIDATION_MESSAGES = {
    "missing": "champ obligatoire",
    "literal_error": "valeur non autorisée (attendu : {expected})",
    "int_parsing": "nombre entier attendu",
    "int_type": "nombre entier attendu",
    "int_from_float": "nombre entier attendu",
    "string_type": "texte attendu",
    "bool_parsing": "booléen attendu",
    "greater_than_equal": "doit être supérieur ou égal à {ge}",
    "less_than_equal": "doit être inférieur ou égal à {le}",
    "string_too_short": "doit contenir au moins {min_length} caractère(s)",
    "string_too_long": "ne doit pas dépasser {max_length} caractères",
    "datetime_parsing": "date-heure ISO 8601 attendue",
    "datetime_from_date_parsing": "date-heure ISO 8601 attendue",
    "datetime_type": "date-heure ISO 8601 attendue",
    "json_invalid": "JSON invalide",
    "model_attributes_type": "objet JSON attendu",
    "dict_type": "objet JSON attendu",
    "bool_type": "booléen attendu",
    "list_type": "liste attendue",
    "extra_forbidden": "champ inconnu",
}


def _validation_message(err: dict) -> str:
    template = _VALIDATION_MESSAGES.get(err.get("type", ""))
    if template is None:
        return str(err.get("msg", "valeur invalide")).removeprefix("Value error, ")
    ctx = dict(err.get("ctx") or {})
    if "expected" in ctx:
        ctx["expected"] = str(ctx["expected"]).replace(" or ", " ou ")
    try:
        return template.format(**ctx)
    except (KeyError, IndexError):
        return template.split(" (")[0]


@app.exception_handler(RequestValidationError)
def invalid_request(_request: Request, exc: RequestValidationError):
    """Erreurs de validation en 400 avec un message en français (le front affiche ``detail`` tel quel)."""
    messages = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()) if part != "body") or "requête"
        messages.append(f"{field} : {_validation_message(err)}")
    return JSONResponse({"detail": "Requête invalide — " + " ; ".join(messages)}, status_code=400)


@app.exception_handler(db.NotFound)
def not_found(_request: Request, exc: db.NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(db.Conflict)
def conflict(_request: Request, exc: db.Conflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(archive.ExplorerUnavailable)
def not_implemented(_request: Request, exc: archive.ExplorerUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=501)


@app.exception_handler(tailoring.RenderFailure)
def render_failed(_request: Request, exc: tailoring.RenderFailure):
    return JSONResponse({"detail": f"Rendu du document impossible : {exc}"}, status_code=500)


@app.exception_handler(interview_prep.PreparationError)
def preparation_failed(_request: Request, exc: interview_prep.PreparationError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ValueError)
def bad_value(_request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


class AnalyzeIn(BaseModel):
    form: dict = {}
    prompt: str = ""


class SearchIn(BaseModel):
    name: str
    form: dict = {}
    prompt: str = ""
    criteria: dict
    frequency: str = "daily"


class OfferPatch(BaseModel):
    status: str | None = None
    viewed: bool | None = None


class AIProviderIn(BaseModel):
    """Choix explicite de l'assistant utilisé pour toutes les tâches IA."""

    model_config = ConfigDict(extra="forbid")
    provider: Literal["claude", "codex"]


@app.get("/api/info")
def info():
    cfg = load_config()
    st = settings(cfg)
    provider = cfg["ai_provider"]
    return {
        "provider": provider,
        "label": pipeline.PROVIDER_LABEL.get(provider, provider),
        "providers": ai.provider_availability(cfg),
        "timezone": st.timezone,
        "follow_up_delay_days": st.follow_up_delay_days,
        "alert_min_score": st.alert_min_score,
    }


@app.put("/api/settings/ai-provider")
def set_ai_provider(body: AIProviderIn):
    """Persiste le moteur choisi ; une commande absente est refusée immédiatement."""
    cfg = load_config()
    availability = ai.provider_availability(cfg)
    if not availability[body.provider]:
        command = cfg[f"{body.provider}_bin"]
        raise HTTPException(409, f"Commande `{command}` introuvable. Installe-la et connecte-toi d'abord.")
    config.save_config_section("ai_provider", body.provider)
    return {
        "provider": body.provider,
        "label": pipeline.PROVIDER_LABEL[body.provider],
        "providers": availability,
    }


@app.post("/api/analyze")
def analyze(body: AnalyzeIn):
    if not body.prompt.strip() and not any(body.form.values()):
        raise HTTPException(400, "Renseigne le formulaire ou décris ta recherche.")
    try:
        return ai.analyze_criteria(body.form, body.prompt)
    except ai.AIError as e:
        raise HTTPException(502, str(e))


@app.get("/api/searches")
def searches():
    return db.list_searches()


@app.post("/api/searches")
def create_search(body: SearchIn):
    return {"id": db.save_search(body.model_dump())}


@app.put("/api/searches/{sid}")
def update_search(sid: int, body: SearchIn):
    if not db.get_search(sid):
        raise HTTPException(404)
    db.save_search(body.model_dump(), sid)
    return {"id": sid}


@app.delete("/api/searches/{sid}")
def delete_search(sid: int):
    db.delete_search(sid)
    return {"ok": True}


@app.post("/api/searches/{sid}/run")
def run(sid: int, score_only: bool = False):
    if not db.get_search(sid):
        raise HTTPException(404)
    try:
        pipeline.run_in_background(sid, score_only)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


class AutoApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    min_score: StrictInt = Field(ge=0, le=100)
    daily_limit: StrictInt = Field(ge=1, le=50)


@app.get("/api/auto-apply")
def auto_apply_overview():
    return preparation.overview()


@app.post("/api/auto-apply/retry")
def auto_apply_retry_all():
    return preparation.retry_all()


@app.get("/api/auto-apply/jobs/{job_id}/activity")
def auto_apply_activity(job_id: int):
    return preparation.activity(job_id)


@app.post("/api/auto-apply/jobs/{job_id}/retry")
def retry_auto_apply_preparation(job_id: int):
    return preparation.retry_preparation(job_id)


@app.get("/api/searches/{sid}/auto-apply")
def auto_apply_status(sid: int):
    return preparation.status(sid)


@app.put("/api/searches/{sid}/auto-apply")
def configure_auto_apply(sid: int, body: AutoApplyIn):
    return preparation.configure(sid, **body.model_dump())


@app.post("/api/offers/{oid}/auto-apply")
def queue_offer_for_auto_apply(oid: int):
    """Choix explicite : ajoute une seule offre sûre à la file, indépendamment du seuil de score."""
    return preparation.schedule_offer(oid)


@app.get("/api/status")
def status():
    return pipeline.STATE


@app.get("/api/searches/{sid}/offers")
def offers(sid: int):
    return db.list_offers(sid)


@app.patch("/api/offers/{oid}")
def patch_offer(oid: int, body: OfferPatch):
    db.update_offer(oid, body.model_dump(exclude_none=True))
    return {"ok": True}


class ApplicationIn(BaseModel):
    offer_id: int | None = None
    title: str = ""
    company: str | None = None
    location: str | None = None
    url: str | None = None
    status: ApplicationStatus | None = None
    notes: str = ""


class ApplicationPatch(BaseModel):
    status: ApplicationStatus | None = None
    notes: str | None = None


class CompanyRuleIn(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    disposition: Literal["favorite", "excluded", "neutral"]


class SnoozeIn(BaseModel):
    days: int = Field(ge=1, le=30)


class InterviewPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    starts_at: datetime | None = None
    duration_minutes: int | None = Field(None, ge=15, le=480)
    timezone: str | None = Field(None, max_length=64)
    location: str | None = Field(None, max_length=300)
    meeting_url: str | None = Field(None, max_length=2000)
    notes: str | None = Field(None, max_length=5000)
    reminder_minutes: int | None = Field(None, ge=0, le=20160)


class InterviewIn(InterviewPatch):
    starts_at: datetime
    duration_minutes: int = Field(60, ge=15, le=480)
    reminder_minutes: int = Field(1440, ge=0, le=20160)


@app.get("/api/applications")
def applications():
    return db.list_applications()


@app.get("/api/analytics/skills")
def skill_analytics(limit: int = 30):
    if not 1 <= limit <= 100:
        raise HTTPException(400, "La limite doit être comprise entre 1 et 100.")
    return analytics.demanded_skills(limit)


@app.get("/api/analytics/applications")
def application_analytics():
    return analytics.application_statistics()


@app.post("/api/applications")
def create_application(body: ApplicationIn):
    if not body.offer_id and not body.title.strip():
        raise HTTPException(400, "Intitulé requis")
    return archive.on_created(db.create_application(body.model_dump()))


def _archived(aid: int, result: dict) -> dict:
    """Après une modification : LISEZMOI.md, candidature.json, .ics et INDEX.md régénérés (sans jamais échouer)."""
    archive.refresh(aid)
    return result


@app.patch("/api/applications/{aid}")
def patch_application(aid: int, body: ApplicationPatch):
    res = db.update_application(aid, body.model_dump(exclude_none=True))
    if not res:
        raise HTTPException(404, "Candidature introuvable.")
    return _archived(aid, res)


@app.delete("/api/applications/{aid}")
def delete_application(aid: int):
    try:
        archive_dir = db.get_application(aid)["archive_dir"]
    except db.NotFound:
        archive_dir = None
    db.delete_application(aid)
    archive.retire(archive_dir)  # le dossier part dans _retirees/, jamais supprimé
    return {"ok": True}


@app.get("/api/applications/{aid}/events")
def application_events(aid: int):
    return db.list_events(aid)


# ---------------------------------------------------------------- règles par entreprise

@app.get("/api/company-rules")
def company_rules():
    return db.list_company_rules()


@app.put("/api/company-rules")
def put_company_rule(body: CompanyRuleIn):
    return db.set_company_rule(body.company, body.disposition)


# ---------------------------------------------------------------- relances

@app.get("/api/follow-ups")
def follow_ups():
    return db.follow_ups()


@app.post("/api/applications/{aid}/follow-up/draft")
def follow_up_draft(aid: int):
    context = db.follow_up_context(aid)
    try:
        draft = ai.draft_follow_up(context)
    except ai.AIError as e:  # QuotaError comprise
        raise HTTPException(502, str(e))
    db.record_follow_up_draft(aid, draft)
    return draft


@app.post("/api/applications/{aid}/follow-up/sent")
def follow_up_sent(aid: int):
    return _archived(aid, db.mark_follow_up_sent(aid))


@app.post("/api/applications/{aid}/follow-up/snooze")
def follow_up_snooze(aid: int, body: SnoozeIn):
    return _archived(aid, db.snooze_follow_up(aid, body.days))


@app.post("/api/applications/{aid}/follow-up/disable")
def follow_up_disable(aid: int):
    return _archived(aid, db.set_follow_up_disabled(aid, True))


@app.post("/api/applications/{aid}/follow-up/enable")
def follow_up_enable(aid: int):
    return _archived(aid, db.set_follow_up_disabled(aid, False))


# ---------------------------------------------------------------- entretiens

class PreparationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interview_id: StrictInt | None = None


class SimulationStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preparation_id: StrictInt | None = None


class SimulationAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_index: StrictInt = Field(ge=0)
    answer: str = Field(min_length=1, max_length=6_000)


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    role: str | None = Field(None, max_length=120)
    company: str | None = Field(None, max_length=120)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=60)
    profile_url: str | None = Field(None, max_length=2_000)
    context: str = Field("", max_length=1_000)
    notes: str = Field("", max_length=5_000)


class ContactPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=120)
    role: str | None = Field(None, max_length=120)
    company: str | None = Field(None, max_length=120)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=60)
    profile_url: str | None = Field(None, max_length=2_000)
    context: str | None = Field(None, max_length=1_000)
    notes: str | None = Field(None, max_length=5_000)


class ContactDraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["prise_contact", "remerciement", "relance"] = "prise_contact"

@app.get("/api/applications/{aid}/interviews")
def interviews(aid: int):
    return db.list_interviews(aid)


@app.post("/api/applications/{aid}/interviews")
def create_interview(aid: int, body: InterviewIn):
    return _archived(aid, db.create_interview(aid, body.model_dump()))


@app.get("/api/interviews/{iid}.ics")
def interview_ics(iid: int):
    filename, content = db.interview_calendar(iid)
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(content, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": disposition})


@app.patch("/api/interviews/{iid}")
def patch_interview(iid: int, body: InterviewPatch):
    interview = db.update_interview(iid, body.model_dump(exclude_unset=True))
    return _archived(interview["application_id"], interview)


@app.delete("/api/interviews/{iid}")
def cancel_interview(iid: int):
    interview = db.cancel_interview(iid)
    return _archived(interview["application_id"], interview)


@app.get("/api/applications/{aid}/interview-preparations")
def interview_preparations(aid: int):
    return db.list_interview_preparations(aid)


@app.post("/api/applications/{aid}/interview-preparations")
def create_interview_preparation(aid: int, body: PreparationIn):
    try:
        return interview_prep.generate(aid, body.interview_id)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/interview-preparations/{pid}")
def interview_preparation(pid: int):
    return db.get_interview_preparation(pid)


@app.get("/api/applications/{aid}/interview-simulations")
def interview_simulations(aid: int):
    return db.list_interview_simulations(aid)


@app.post("/api/applications/{aid}/interview-simulations")
def start_interview_simulation(aid: int, body: SimulationStartIn):
    return interview_simulation.start(aid, body.preparation_id)


@app.get("/api/interview-simulations/{sid}")
def interview_simulation_detail(sid: int):
    return db.get_interview_simulation(sid)


@app.post("/api/interview-simulations/{sid}/answers")
def answer_interview_simulation(sid: int, body: SimulationAnswerIn):
    try:
        return interview_simulation.answer(sid, body.question_index, body.answer)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/applications/{aid}/contacts")
def application_contacts(aid: int):
    return db.list_application_contacts(aid)


@app.post("/api/applications/{aid}/contacts")
def create_application_contact(aid: int, body: ContactIn):
    return db.create_application_contact(aid, contacts.validate(body.model_dump()))


@app.patch("/api/contacts/{cid}")
def patch_application_contact(cid: int, body: ContactPatchIn):
    return db.update_application_contact(cid, contacts.validate(body.model_dump(exclude_unset=True), partial=True))


@app.delete("/api/contacts/{cid}", status_code=204)
def delete_application_contact(cid: int):
    db.delete_application_contact(cid)
    return Response(status_code=204)


@app.post("/api/contacts/{cid}/draft")
def contact_draft(cid: int, body: ContactDraftIn):
    try:
        return contacts.draft(cid, body.purpose)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc)) from exc


# ---------------------------------------------------------------- archives des candidatures

class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    # 20 Mo une fois décodés, soit environ 26,7 millions de caractères en base64.
    content_base64: str = Field(min_length=1, max_length=28_000_000)


class ProfileSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["cv", "lettre"]
    name: str = Field(min_length=1, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=30)
    filename: str = Field(min_length=1, max_length=255)
    # 10 Mo décodés représentent moins de 14 millions de caractères base64.
    content_base64: str = Field(min_length=1, max_length=14_000_000)


class ConflictResolutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1, max_length=100)
    value: str


class DocumentsPrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: list[Literal["cv", "lettre"]] = Field(min_length=1, max_length=2)
    base_cv: str | None = None
    base_letter: str | None = None
    language: Literal["fr", "en"] | None = None
    template: dict | None = None


class DocumentsRenderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    composition: dict | None = None
    letter: dict | None = None
    template: dict | None = None


class DocumentsRefineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str = Field(min_length=36, max_length=36)
    instruction: str = Field(min_length=1, max_length=2_000)
    composition: dict | None = None
    letter: dict | None = None


class SkillAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    status: Literal["maitrise", "maîtrise", "je_maitrise", "notion", "inconnu", "ne_connait_pas",
                    "je_ne_connais_pas"]
    level: Literal["notion", "intermediaire", "avance"] | None = None
    category: str | None = Field(None, max_length=80)
    evidence: list[str] | str = Field(default_factory=list)


class ItemAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=100)
    action: Literal["retirer", "garder_une_ligne", "remplacer", "ajouter_projet"]
    replacement_id: str | None = Field(None, min_length=1, max_length=100)


class MissingAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=2000)
    remember: StrictBool = True


class DocumentAnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str = Field(min_length=36, max_length=36)
    skills: list[SkillAnswerIn] = Field(default_factory=list, max_length=100)
    items: list[ItemAnswerIn] = Field(default_factory=list, max_length=100)
    missing: list[MissingAnswerIn] = Field(default_factory=list, max_length=20)
    composition: dict | None = None
    letter: dict | None = None


class DocumentPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    composition: dict
    template: dict | None = None


class DocumentImproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)


class ApplicationStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual", "assisted"]
    cv_document_id: int | None = Field(None, ge=1)
    letter_document_id: int | None = Field(None, ge=1)
    remember: StrictBool = False
    review_policy: Literal["first_five", "always", "never"] = "first_five"


class ApplicationFieldAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=500)
    value: str | StrictBool


class ApplicationAnswersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[ApplicationFieldAnswerIn] = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------- profil et documents adaptés

@app.get("/api/profile")
def get_profile():
    return profile.load_profile()


@app.put("/api/profile")
def put_profile(body: dict):
    return profile.update_profile(body)


@app.get("/api/profile/versions")
def profile_versions():
    return profile.list_versions()


@app.get("/api/profile/versions/{version}")
def profile_version(version: int):
    try:
        return profile.load_version(version)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/profile/restore/{version}")
def restore_profile(version: int):
    try:
        return profile.restore_version(version)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/profile/sources")
def add_profile_source(body: ProfileSourceIn):
    if len(body.content_base64) > (profile.MAX_SOURCE_BYTES + 2) // 3 * 4 + 4:
        raise HTTPException(400, "Le document source dépasse 10 Mo.")
    try:
        content = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Contenu base64 invalide.") from None
    try:
        return profile.import_source(kind=body.kind, name=body.name, tags=body.tags,
                                     filename=body.filename, content=content)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))


@app.delete("/api/profile/sources/{source_id}")
def remove_profile_source(source_id: str):
    try:
        return profile.delete_source(source_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/api/profile/conflicts")
def profile_conflicts():
    return profile.list_conflicts()


@app.post("/api/profile/conflicts/resolve")
def resolve_profile_conflict(body: ConflictResolutionIn):
    try:
        return profile.resolve_conflict(body.conflict_id, body.value)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/applications/{aid}/documents/prepare")
def prepare_documents(aid: int, body: DocumentsPrepareIn):
    """Lance la préparation en arrière-plan : fermer la page ne l'interrompt pas."""
    return preparation.start_manual(aid, body.model_dump())


@app.get("/api/applications/{aid}/documents/preparation")
def document_preparation(aid: int):
    return {"job": preparation.latest(aid)}


@app.post("/api/applications/{aid}/documents/preparation/consume")
def consume_document_preparation(aid: int):
    db.consume_preparation_jobs(aid)
    return {"ok": True}


@app.get("/api/applications/{aid}/documents/bases")
def document_bases(aid: int, kind: Literal["cv", "lettre"] | None = None,
                   language: Literal["fr", "en"] | None = None):
    return tailoring.suggest_bases(aid, kind=kind, language=language)


@app.post("/api/applications/{aid}/documents/answers")
def answer_document_questions(aid: int, body: DocumentAnswersIn):
    try:
        return tailoring.answer(aid, body.model_dump(exclude_none=True))
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/applications/{aid}/documents/refine")
def refine_application_documents(aid: int, body: DocumentsRefineIn):
    try:
        return tailoring.refine(aid, body.model_dump())
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/profile/projects/assist")
def assist_profile_project(body: dict):
    try:
        return tailoring.assist_project(body)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/applications/{aid}/documents/render")
def render_application_documents(aid: int, body: DocumentsRenderIn):
    result = tailoring.render_documents(aid, body.composition, body.letter, body.template)
    db.consume_preparation_jobs(aid)
    return result


@app.get("/api/applications/{aid}/documents")
def application_documents(aid: int):
    documents = db.list_documents(aid)
    for document in documents:
        if document["status"] in {"brouillon", "pret"}:
            try:
                document["current"] = tailoring.current_check(document["id"])
            except Exception as exc:  # un contrôle impossible ne doit pas masquer la liste
                document["current"] = {"ok": False, "message": f"Contrôle du document impossible : {exc}"}
    return documents


@app.patch("/api/documents/{document_id}")
def patch_document(document_id: int, body: DocumentPatchIn):
    return tailoring.rerender_document(document_id, body.composition, body.template)


@app.post("/api/documents/{document_id}/refine")
def refine_rendered_document(document_id: int, body: DocumentImproveIn):
    try:
        return tailoring.refine_document(document_id, body.instruction)
    except ai.AIError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/documents/{document_id}/ready")
def ready_document(document_id: int):
    return tailoring.mark_ready(document_id)


@app.post("/api/documents/{document_id}/sent")
def sent_document(document_id: int):
    return tailoring.mark_sent(document_id)


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: int, name: str):
    path = tailoring.document_file(document_id, name)
    return _private_file(path, archive.mime_type(path), inline=path.suffix.lower() == ".pdf")


@app.get("/api/templates")
def document_templates():
    return tailoring.list_templates()


@app.get("/api/applications/{aid}/apply/options")
def application_apply_options(aid: int):
    return application_flow.options(aid)


@app.post("/api/applications/{aid}/apply/start")
def start_application(aid: int, body: ApplicationStartIn):
    return application_flow.start(aid, **body.model_dump())


@app.get("/api/applications/{aid}/apply/attempts")
def application_attempts(aid: int):
    return db.list_application_attempts(aid)


@app.post("/api/application-attempts/{attempt_id}/manual-sent")
def confirm_manual_application(attempt_id: int):
    return application_flow.confirm_manual_sent(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/confirm-sent")
def confirm_assisted_application(attempt_id: int):
    """L'utilisateur a vérifié sur le site qu'une candidature assistée non confirmée est bien partie."""
    return application_flow.confirm_assisted_sent(attempt_id)


@app.get("/api/application-attempts/{attempt_id}")
def application_attempt(attempt_id: int):
    return db.get_application_attempt(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/cancel")
def cancel_application_attempt(attempt_id: int):
    return application_flow.cancel(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/retry")
def retry_application_attempt(attempt_id: int):
    return application_flow.retry(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/manual")
def manual_application_attempt(attempt_id: int):
    return application_flow.switch_to_manual(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/approve")
def approve_application_attempt(attempt_id: int):
    return application_flow.approve(attempt_id)


@app.post("/api/application-attempts/{attempt_id}/answers")
def answer_application_fields(attempt_id: int, body: ApplicationAnswersIn):
    return application_flow.answer_fields(attempt_id, [item.model_dump() for item in body.answers])


@app.get("/api/applications/{aid}/archive")
def application_archive(aid: int):
    return archive.archive_view(aid)


@app.get("/api/applications/{aid}/archive/file")
def application_archive_file(aid: int, path: str):
    target = archive.resolve_file(archive.directory_of(aid), path)
    # Le journal d'envoi se lit directement : texte brut, jamais interprété comme du HTML.
    journal = target.name == "journal.md" and target.parent.name == "envoi"
    inline = journal or target.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}  # aperçu dans l'onglet « Offre »
    media_type = "text/plain; charset=utf-8" if journal else archive.mime_type(target)
    return _private_file(target, media_type, inline=inline)


@app.get("/api/applications/{aid}/offer-snapshot")
def offer_snapshot(aid: int):
    row = db.archive_row(aid)
    if not row["offer_snapshot_json"]:
        raise HTTPException(404, "Aucun instantané de l'offre pour cette candidature.")
    return json.loads(row["offer_snapshot_json"])


@app.post("/api/applications/{aid}/archive/recapture")
def recapture(aid: int):
    archive.recapture(aid)
    return archive.archive_view(aid)


@app.post("/api/applications/{aid}/archive/attach")
def attach_capture(aid: int, body: AttachmentIn):
    return archive.attach(aid, body.filename, body.content_base64)


@app.post("/api/applications/{aid}/archive/open")
def open_archive_folder(aid: int):
    archive.open_folder(aid)
    return {"ok": True}


@app.post("/api/archives/reindex")
def reindex_archives():
    return archive.reindex()


@app.get("/api/archives/info")
def archives_info():
    return archive.info()


# ---------------------------------------------------------------- export et sauvegarde

@app.get("/api/export/applications.csv")
def export_applications():
    zone = workflow.validate_timezone(settings().timezone)
    filename = f"JobRadar-candidatures-{db.utcnow().astimezone(zone):%Y%m%d}.csv"
    return Response(export.applications_csv(db.list_applications(), zone), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/backup")
def create_backup():
    return export.backup()


@app.get("/api/backups")
def backups():
    return export.list_backups(data_dir=config.DATA_DIR)


@app.get("/api/backups/{name}")
def download_backup(name: str):
    try:
        path = export.resolve_backup(data_dir=config.DATA_DIR, name=name)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "Sauvegarde introuvable.") from None
    return FileResponse(path, media_type="application/zip", filename=path.name)


# ---------------------------------------------------------------- notifications

@app.get("/api/notifications")
def list_notifications():
    notifications.refresh_notifications()
    return notifications.list_notifications()


@app.post("/api/notifications/dismiss-all")
def dismiss_all_notifications():
    return {"dismissed": notifications.dismiss_all()}


@app.post("/api/notifications/{nid}/dismiss")
def dismiss_notification(nid: int):
    return notifications.dismiss(nid)


# ---------------------------------------------------------------- réglages des alertes par e-mail

EmailKind = Literal["high_score_offer", "follow_up_due", "interview_reminder"]


class EmailSettingsIn(BaseModel):
    """Mise à jour partielle : un champ absent ou null reste inchangé (sauf ``password``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: StrictBool | None = None
    to: str | None = Field(None, max_length=254)
    from_: str | None = Field(None, alias="from", max_length=254)
    smtp_host: str | None = Field(None, max_length=253)
    smtp_port: StrictInt | None = Field(None, ge=1, le=65535)
    security: Literal["starttls", "ssl"] | None = None
    username: str | None = Field(None, max_length=320)
    kinds: list[EmailKind] | None = None
    # Chaîne vide = inchangé ; null explicite (ou clear_password) = suppression du mot de passe enregistré.
    password: str | None = Field(None, max_length=1024)
    clear_password: StrictBool = False


@app.get("/api/settings/email")
def get_email_settings():
    return mailer.settings_view()


@app.put("/api/settings/email")
def put_email_settings(body: EmailSettingsIn):
    fields = {k: v for k, v in body.model_dump(by_alias=True, exclude={"password", "clear_password"}).items()
              if v is not None}
    clear = body.clear_password or ("password" in body.model_fields_set and body.password is None)
    return mailer.update_settings(fields, password=body.password or None, clear=clear)


@app.post("/api/settings/email/test")
def test_email():
    try:
        mailer.send_test(db.utcnow())
    except mailer.RateLimited as e:
        raise HTTPException(429, str(e))
    except mailer.SendError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


class BrowserSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["chromium", "system"]
    executable: str = Field("", max_length=1000)
    cdp_port: StrictInt = Field(9333, ge=1024, le=65535)
    keep_open: StrictBool | None = None
    ai_supervision: Literal["always", "fallback", "off"] | None = None
    human_wait_minutes: StrictInt | None = Field(None, ge=1, le=60)
    auto_accounts: StrictBool | None = None
    google_sign_in: StrictBool | None = None


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(max_length=253)
    username: str = Field(max_length=320)
    password: str = Field(max_length=2048)


@app.get("/api/settings/browser")
def get_browser_settings():
    value = config.browser_settings()
    return value | {"system_browser": config.find_system_browser(value["executable"]),
                    "default_browser": config.default_browser_name(),
                    "accounts": login_credentials.list_accounts()}


@app.put("/api/settings/browser")
def put_browser_settings(body: BrowserSettingsIn):
    return config.save_browser_settings(body.model_dump(exclude_none=True))


@app.post("/api/settings/browser/open")
def open_browser():
    from . import application_browser
    try:
        browser = application_browser.open_system_browser()
    except application_browser.BrowserUnavailable as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "name": browser["name"]}


@app.post("/api/settings/browser/google")
def open_browser_on_google():
    """Ouvre le navigateur de JobRadar sur la connexion Google : la session servira aux « Continuer avec Google »."""
    from . import application_browser
    try:
        browser = application_browser.open_system_browser(url=application_browser.GOOGLE_SIGN_IN)
    except application_browser.BrowserUnavailable as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "name": browser["name"]}


class SourceToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@app.get("/api/sources/jobteaser")
def jobteaser_source():
    return {"enabled": bool(load_config().get("sources", {}).get("jobteaser"))}


@app.put("/api/sources/jobteaser")
def put_jobteaser_source(body: SourceToggleIn):
    sources = dict(load_config().get("sources") or {})
    sources["jobteaser"] = body.enabled
    config.save_config_section("sources", sources)
    return {"enabled": body.enabled}


@app.post("/api/settings/browser/jobteaser")
def open_browser_on_jobteaser():
    """Ouvre le navigateur de JobRadar sur JobTeaser : connecte-toi une fois par ton école, la session est gardée."""
    from . import application_browser
    from .sources import jobteaser
    try:
        browser = application_browser.open_system_browser(url=jobteaser.LOGIN)
    except application_browser.BrowserUnavailable as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "name": browser["name"]}


@app.post("/api/settings/logins")
def put_login(body: LoginIn):
    return login_credentials.store(body.domain, body.username, body.password)


@app.delete("/api/settings/logins/{domain}")
def delete_login(domain: str):
    login_credentials.delete(domain)
    return {"ok": True}


# « no-cache » : le navigateur revalide (ETag) à chaque chargement, donc une mise à jour de
# l'interface est visible dès le redémarrage, sans rechargement forcé.
NO_CACHE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


app.mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static")
