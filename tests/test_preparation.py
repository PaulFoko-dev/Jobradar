"""Préparations en arrière-plan et auto-candidature : file durable, éligibilité, chaîne automatique."""
from datetime import timedelta

import pytest
from conftest import FIXED_NOW

from jobradar import application_flow, db, notifications, pipeline, preparation, tailoring


def _score(offer_id: int, score: int, **fields: object) -> None:
    assignments = ", ".join(["score=?", *(f"{name}=?" for name in fields)])
    with db.connect() as c:
        c.execute(f"UPDATE offers SET {assignments} WHERE id=?", (score, *fields.values(), offer_id))


def _enable(search_id: int, min_score: int = 80, daily_limit: int = 10) -> None:
    db.set_auto_apply(search_id, enabled=True, min_score=min_score, daily_limit=daily_limit)


def _application(make_offer) -> dict:
    return db.create_application({"offer_id": make_offer()})


@pytest.fixture
def quiet_queue(monkeypatch):
    """Les tâches créées restent en attente : chaque test les traite explicitement."""
    monkeypatch.setattr(preparation, "wake", lambda: None)


@pytest.fixture
def chain(monkeypatch):
    """Chaîne automatique simulée : préparation, rendu (ATS réglable), prêt, options et départ de l'envoi."""
    calls = {"prepare": [], "ready": [], "start": [], "ats": 100.0, "assisted": True}

    def prepare(application_id, **params):
        calls["prepare"].append((application_id, params))
        return {"session": "00000000-0000-4000-8000-000000000001", "composition": {"cv": True},
                "letter": {"lettre": True}, "questions": {"skills": ["Splunk"]}, "suggested_bases": [], "notes": []}

    def render_documents(application_id, composition, letter, template=None):
        ats = {"ready": calls["ats"] >= 95, "readability": calls["ats"]}
        return {"documents": [{"id": 11, "kind": "cv", "ats": ats}, {"id": 12, "kind": "lettre", "ats": ats}],
                "ats": ats}

    def options(application_id):
        allowed = calls["assisted"]
        return {"assisted": {"allowed": allowed, "implemented": True,
                             "reason": None if allowed else "LinkedIn et Indeed sont disponibles uniquement en mode manuel."}}

    def start(application_id, **kwargs):
        calls["start"].append((application_id, kwargs))
        return {"attempt": {"id": 77}}

    def record_attempt(job_id, **fields):
        if fields.get("attempt_id") == 77:  # tentative simulée : aucune ligne réelle à référencer
            fields["attempt_id"] = None
            calls["attempt_recorded"] = True
        return update(job_id, **fields)

    update = db.update_preparation_job

    monkeypatch.setattr(tailoring, "prepare", prepare)
    monkeypatch.setattr(tailoring, "render_documents", render_documents)
    monkeypatch.setattr(tailoring, "mark_ready", lambda document_id: calls["ready"].append(document_id))
    monkeypatch.setattr(application_flow, "options", options)
    monkeypatch.setattr(application_flow, "start", start)
    monkeypatch.setattr(db, "update_preparation_job", record_attempt)
    return calls


# ---------------------------------------------------------------- préparation manuelle


def test_la_preparation_continue_sans_page_et_son_resultat_reste_recuperable(make_offer, monkeypatch, quiet_queue):
    application = _application(make_offer)
    monkeypatch.setattr(tailoring, "check_preparation", lambda *_a, **_k: None)
    monkeypatch.setattr(tailoring, "prepare", lambda application_id, **params: {
        "session": "00000000-0000-4000-8000-000000000002", "composition": {"v": 1}, "letter": None,
        "questions": {}, "suggested_bases": [], "notes": ["note"]})
    started = preparation.start_manual(application["id"], {"kinds": ["cv"]})
    assert started["status"] == "queued" and started["result"] is None

    # Aucun client n'attend : la file traite la tâche seule et le résultat reste en base.
    assert preparation.run_pending() == 1
    job = preparation.latest(application["id"])
    assert (job["status"], job["step"]) == ("done", "prepared")
    assert job["result"]["composition"] == {"v": 1} and job["result"]["notes"] == ["note"]


def test_le_resultat_relit_la_session_pour_refleter_les_reponses(make_offer, monkeypatch, quiet_queue):
    application = _application(make_offer)
    job = db.create_preparation_job(application["id"], origin="manual", params={"kinds": ["cv"]})
    db.update_preparation_job(job["id"], status="done", session_id="00000000-0000-4000-8000-000000000003",
                              result={"session": "s", "composition": {"v": 1}, "letter": None, "questions": {"a": 1},
                                      "suggested_bases": [], "notes": []})
    monkeypatch.setattr(tailoring, "load_session", lambda *_a: {"composition": {"v": 2}, "letter": None,
                                                                "questions": {}})
    result = preparation.latest(application["id"])["result"]
    assert result["composition"] == {"v": 2} and result["questions"] == {}


def test_double_lancement_refuse_et_reprise_apres_arret(make_offer, monkeypatch, quiet_queue):
    application = _application(make_offer)
    monkeypatch.setattr(tailoring, "check_preparation", lambda *_a, **_k: None)
    preparation.start_manual(application["id"], {"kinds": ["cv"]})
    with pytest.raises(db.Conflict):
        preparation.start_manual(application["id"], {"kinds": ["cv"]})

    running = db.claim_next_preparation_job()
    assert running["status"] == "running"
    assert db.recover_preparation_jobs() == 1  # arrêt brutal de JobRadar pendant la préparation
    assert db.get_preparation_job(running["id"])["status"] == "queued"


def test_un_echec_est_enregistre_sans_nouvel_essai(make_offer, monkeypatch, quiet_queue):
    application = _application(make_offer)
    monkeypatch.setattr(tailoring, "check_preparation", lambda *_a, **_k: None)
    monkeypatch.setattr(tailoring, "prepare", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("Quota IA atteint")))
    preparation.start_manual(application["id"], {"kinds": ["cv"]})
    assert preparation.run_pending() == 1
    job = preparation.latest(application["id"])
    assert job["status"] == "failed" and "Quota" in job["error"]
    assert preparation.run_pending() == 0


def test_routes_de_suivi_et_de_reprise(client, make_offer, monkeypatch):
    application = _application(make_offer)
    monkeypatch.setattr(tailoring, "check_preparation", lambda *_a, **_k: None)
    monkeypatch.setattr(tailoring, "prepare", lambda *_a, **_k: {
        "session": "00000000-0000-4000-8000-000000000004", "composition": None, "letter": {"v": 1},
        "questions": {}, "suggested_bases": [], "notes": []})
    monkeypatch.setattr(tailoring, "load_session", lambda *_a: (_ for _ in ()).throw(FileNotFoundError()))
    assert client.post(f"/api/applications/{application['id']}/documents/prepare",
                       json={"kinds": ["lettre"]}).json()["status"] == "queued"
    assert preparation._QUEUE.wait_idle(10)
    job = client.get(f"/api/applications/{application['id']}/documents/preparation").json()["job"]
    assert job["status"] == "done" and job["consumed_at_utc"] is None and job["result"]["letter"] == {"v": 1}

    assert client.post(f"/api/applications/{application['id']}/documents/preparation/consume").status_code == 200
    job = client.get(f"/api/applications/{application['id']}/documents/preparation").json()["job"]
    assert job["consumed_at_utc"] is not None


# ---------------------------------------------------------------- éligibilité


def test_eligibilite_seuil_suivi_masque_exclu_et_mode_desactive(search_id, make_offer, quiet_queue, chain):
    good, low, hidden, followed = (make_offer() for _ in range(4))
    excluded = make_offer(company="Évitée SA")
    for offer_id in (good, hidden, followed, excluded):
        _score(offer_id, 90)
    _score(low, 60)
    _score(hidden, 90, status="ignored")
    db.create_application({"offer_id": followed})
    db.set_company_rule("Évitée SA", "excluded")

    assert preparation.schedule(search_id) == []  # mode désactivé
    _enable(search_id, min_score=80)
    assert [offer["id"] for offer in preparation.eligible_offers(db.get_search(search_id))] == [good]
    assert len(preparation.schedule(search_id)) == 1
    assert db.get_application(db.create_application({"offer_id": good})["id"])["offer_id"] == good
    # Une offre confiée une fois ne repart jamais, même si sa candidature est supprimée.
    db.delete_application(db.create_application({"offer_id": good})["id"])
    assert preparation.schedule(search_id) == []


def test_eligibilite_exclut_le_score_egal_au_seuil(search_id, make_offer):
    equal, above = make_offer(), make_offer()
    _score(equal, 90)
    _score(above, 91)
    _enable(search_id, min_score=90)

    assert [offer["id"] for offer in preparation.eligible_offers(db.get_search(search_id))] == [above]


def test_eligibilite_exclut_une_exigence_de_nationalite(search_id, make_offer):
    allowed, restricted = make_offer(), make_offer()
    _score(allowed, 91)
    _score(restricted, 92, description="Ce poste exige la nationalité française.")
    _enable(search_id, min_score=80)
    assert [offer["id"] for offer in preparation.eligible_offers(db.get_search(search_id))] == [allowed]


def test_plafond_quotidien_dans_le_fuseau_de_l_utilisateur(search_id, make_offer, quiet_queue, chain, clock):
    offers = [make_offer() for _ in range(4)]
    for index, offer_id in enumerate(offers):
        _score(offer_id, 95 - index)
    _enable(search_id, min_score=80, daily_limit=2)
    assert len(preparation.schedule(search_id)) == 2
    assert preparation.schedule(search_id) == []
    assert preparation.status(search_id)["remaining_today"] == 0

    clock(FIXED_NOW + timedelta(days=1))
    assert len(preparation.schedule(search_id)) == 2


def test_fin_de_recherche_declenche_l_auto_candidature(search_id, make_offer, quiet_queue, chain, monkeypatch):
    _score(make_offer(), 92)
    _enable(search_id)
    monkeypatch.setattr(pipeline, "_collect", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "_score", lambda *_a, **_k: [])
    pipeline.run_search(search_id, send_email=False)
    assert any("Auto-candidature : 1 offre" in line for line in pipeline.STATE["log"])


# ---------------------------------------------------------------- chaîne automatique


def _auto_job(search_id: int, make_offer) -> dict:
    _score(make_offer(), 90)
    _enable(search_id)
    (job_id,) = preparation.schedule(search_id)
    assert preparation.run_pending() == 1
    return db.get_preparation_job(job_id)


def test_chaine_complete_jusqu_a_l_envoi_assiste_sans_relecture(search_id, make_offer, quiet_queue, chain):
    application_flow.save_preferences(mode="manual", include_letter=True, review_policy="always")
    job = _auto_job(search_id, make_offer)
    assert (job["status"], job["step"], chain.get("attempt_recorded")) == ("done", "queued_for_submission", True)
    assert chain["prepare"][0][1] == {"kinds": ["cv", "lettre"]}
    assert chain["ready"] == [11, 12]
    ((application_id, kwargs),) = chain["start"]
    assert kwargs == {"mode": "assisted", "cv_document_id": 11, "letter_document_id": 12, "review_policy": "never"}
    # La préférence de relecture de l'utilisateur n'est pas modifiée par l'auto-candidature.
    assert application_flow._preferences()["review_policy"] == "always"


def test_arret_sous_95_pourcent_ats_avec_notification(search_id, make_offer, quiet_queue, chain):
    chain["ats"] = 88.0
    job = _auto_job(search_id, make_offer)
    assert job["status"] == "failed" and "ATS" in job["error"]
    assert chain["ready"] == [] and chain["start"] == []
    assert [n["kind"] for n in notifications.list_notifications()] == ["auto_apply_failed"]


def test_arret_sur_erreur_ia_avec_notification(search_id, make_offer, quiet_queue, chain, monkeypatch):
    monkeypatch.setattr(tailoring, "prepare", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("Quota IA")))
    job = _auto_job(search_id, make_offer)
    assert job["status"] == "failed" and job["error"] == "Quota IA" and chain["start"] == []
    assert notifications.list_notifications()[0]["payload"]["message"] == "Quota IA"


def test_fournisseur_manuel_documents_prets_sans_envoi(search_id, make_offer, quiet_queue, chain):
    chain["assisted"] = False
    job = _auto_job(search_id, make_offer)
    assert (job["status"], job["step"]) == ("done", "manual")
    assert chain["ready"] == [11, 12] and chain["start"] == []
    (notification,) = notifications.list_notifications()
    assert notification["kind"] == "auto_apply_manual" and "Indeed" in notification["payload"]["message"]


def test_reglages_valides_par_l_api(client, search_id, make_offer, chain, monkeypatch):
    monkeypatch.setattr(preparation, "wake", lambda: None)
    _score(make_offer(), 90)
    assert client.put(f"/api/searches/{search_id}/auto-apply",
                      json={"enabled": True, "min_score": 120, "daily_limit": 5}).status_code == 400
    assert client.put(f"/api/searches/{search_id}/auto-apply",
                      json={"enabled": "oui", "min_score": 80, "daily_limit": 5}).status_code == 400
    body = client.put(f"/api/searches/{search_id}/auto-apply",
                      json={"enabled": True, "min_score": 80, "daily_limit": 5}).json()
    assert body["settings"] == {"enabled": True, "min_score": 80, "daily_limit": 5}
    assert body["scheduled"] == 1 and body["today"] == 1 and body["remaining_today"] == 4
    assert client.get("/api/searches/999/auto-apply").status_code == 404


def test_page_dediee_liste_toutes_les_recherches_et_reprend_les_arrets(client, search_id, make_offer, monkeypatch):
    from jobradar import application_queue
    monkeypatch.setattr(application_queue, "wake", lambda: None)

    def stopped(kind: str, *, clicked: bool = False) -> int:
        application = _application(make_offer)
        job = db.create_preparation_job(application["id"], origin="auto", params={})
        document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                      {"readability": 100, "ready": True}, [])
        db.set_document_status(document["id"], "pret")
        attempt = db.create_application_attempt(application["id"], mode="assisted", status="running",
                                                cv_document_id=document["id"], letter_document_id=None,
                                                target_url="https://exemple.test/offre", provider="generic")
        if clicked:
            db.mark_submit_clicked(attempt["id"])
        db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                          intervention={"kind": kind, "message": kind})
        db.update_preparation_job(job["id"], status="done", step="queued_for_submission", attempt_id=attempt["id"])
        return attempt["id"]

    resumable = stopped("no_progress")
    closed = stopped("offer_removed")
    clicked = stopped("missing_fields", clicked=True)
    response = client.get("/api/auto-apply")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["id"] for s in body["searches"]] == [search_id]
    by_attempt = {item["attempt"]["id"]: item for item in body["recent"]}
    assert by_attempt[resumable]["resumable"] and by_attempt[resumable]["search_id"] == search_id
    assert not by_attempt[closed]["resumable"] and not by_attempt[clicked]["resumable"]

    assert client.post("/api/auto-apply/retry").json() == {"retried": 1}
    assert db.get_application_attempt(resumable)["status"] == "queued"
    assert db.get_application_attempt(closed)["status"] == "needs_user"
    assert db.get_application_attempt(clicked)["status"] == "needs_user"
    assert client.post("/api/auto-apply/retry").json() == {"retried": 0}


def test_candidature_envoyee_a_la_main_n_est_jamais_relancee(client, search_id, make_offer, monkeypatch):
    from jobradar import application_queue
    monkeypatch.setattr(application_queue, "wake", lambda: None)
    application = _application(make_offer)
    job = db.create_preparation_job(application["id"], origin="auto", params={})
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                  {"readability": 100, "ready": True}, [])
    db.set_document_status(document["id"], "pret")
    attempt = db.create_application_attempt(application["id"], mode="assisted", status="running",
                                            cv_document_id=document["id"], letter_document_id=None,
                                            target_url="https://exemple.test/offre", provider="generic")
    db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                      intervention={"kind": "not_application_form", "message": "Formulaire"})
    db.update_preparation_job(job["id"], status="done", step="queued_for_submission", attempt_id=attempt["id"])
    db.update_application(application["id"], {"status": "envoyee"})

    (item,) = client.get("/api/auto-apply").json()["recent"]
    assert item["sent_by_hand"] and not item["resumable"]
    assert client.post("/api/auto-apply/retry").json() == {"retried": 0}
    assert client.post(f"/api/application-attempts/{attempt['id']}/retry").status_code == 409
    assert db.get_application_attempt(attempt["id"])["status"] == "needs_user"
