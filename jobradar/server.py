"""Serveur web local (http://127.0.0.1:8765)."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, db, pipeline
from .config import load_config

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="JobRadar")
db.init()
load_config()


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


@app.get("/api/info")
def info():
    provider = load_config()["ai_provider"]
    return {"provider": provider, "label": pipeline.PROVIDER_LABEL.get(provider, provider)}


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
    status: str | None = None
    notes: str = ""


class ApplicationPatch(BaseModel):
    status: str | None = None
    notes: str | None = None


@app.get("/api/applications")
def applications():
    return db.list_applications()


@app.post("/api/applications")
def create_application(body: ApplicationIn):
    if not body.offer_id and not body.title.strip():
        raise HTTPException(400, "Intitulé requis")
    try:
        return db.create_application(body.model_dump())
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.patch("/api/applications/{aid}")
def patch_application(aid: int, body: ApplicationPatch):
    res = db.update_application(aid, body.model_dump(exclude_none=True))
    if not res:
        raise HTTPException(404)
    return res


@app.delete("/api/applications/{aid}")
def delete_application(aid: int):
    db.delete_application(aid)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
