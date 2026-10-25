"""Premiers lots de l'étape 5 : choix sûrs, mode manuel et file assistée."""

import pytest

from jobradar import application_flow, application_queue, db, profile, tailoring
from jobradar.migrations import LATEST_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def _documents_a_jour(monkeypatch) -> None:
    """Ces documents de test n'ont pas de PDF : leur actualité est couverte par test_document_currency."""
    monkeypatch.setattr(tailoring, "current_check", lambda _document_id: {"ok": True, "message": None})


def _application(client, url="https://boards.greenhouse.io/acme/jobs/42") -> dict:
    return client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme", "url": url}).json()


def _ready_document(application_id: int, kind: str = "cv", version: int = 1) -> dict:
    document = db.create_document(
        application_id, kind, version,
        {"kind": kind, "template": {"kind": "jobradar", "id": "sobre-ats"}},
        {"kind": "jobradar", "id": "sobre-ats"},
        {"readability": 100, "ready": True, "keyword_coverage": {}, "issues": []},
        [],
    )
    return db.set_document_status(document["id"], "pret")


def test_migration_13_creates_application_attempts_and_review_policy(data_dir) -> None:
    with db.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION == 15
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(application_attempts)")}
    assert {"application_id", "mode", "status", "cv_document_id", "letter_document_id",
            "target_url", "provider", "answers_json", "intervention_json", "submitted_at_utc",
            "review_policy"} <= columns


def test_options_only_offer_ready_ats_documents_and_safe_defaults(client) -> None:
    application = _application(client)
    ready = _ready_document(application["id"])
    db.create_document(application["id"], "lettre", 2, {"kind": "lettre"}, {},
                       {"readability": 100, "ready": True}, [])

    response = client.get(f"/api/applications/{application['id']}/apply/options")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "greenhouse"
    assert body["assisted"] == {"allowed": True, "implemented": True, "reason": None}
    assert [item["id"] for item in body["documents"]] == [ready["id"]]
    assert body["defaults"]["mode"] == "manual"
    assert body["defaults"]["review_policy"] == "first_five"
    assert body["defaults"]["cv_document_id"] == ready["id"]
    assert body["attempts"] == []


def test_review_policy_is_frozen_on_attempt_and_can_be_remembered(client) -> None:
    application = _application(client)
    cv = _ready_document(application["id"])

    response = client.post(f"/api/applications/{application['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": cv["id"], "review_policy": "always", "remember": True,
    })

    assert response.status_code == 200
    assert response.json()["attempt"]["review_policy"] == "always"
    assert client.get(f"/api/applications/{application['id']}/apply/options").json()["defaults"][
        "review_policy"] == "always"


def test_review_policy_rejects_unknown_values(client) -> None:
    application = _application(client)
    cv = _ready_document(application["id"])
    response = client.post(f"/api/applications/{application['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": cv["id"], "review_policy": "sometimes",
    })
    assert response.status_code == 400
    assert "review_policy" in response.json()["detail"]


def test_linkedin_and_indeed_are_manual_only(client) -> None:
    for url, provider in (("https://www.linkedin.com/jobs/view/42", "linkedin"),
                          ("https://fr.indeed.com/viewjob?jk=42", "indeed")):
        application = _application(client, url)
        document = _ready_document(application["id"])
        options = client.get(f"/api/applications/{application['id']}/apply/options").json()
        assert options["provider"] == provider and options["assisted"]["allowed"] is False
        response = client.post(f"/api/applications/{application['id']}/apply/start", json={
            "mode": "assisted", "cv_document_id": document["id"],
        })
        assert response.status_code == 409 and "uniquement en mode manuel" in response.json()["detail"]


def test_manual_start_rejects_foreign_or_unready_documents(client) -> None:
    first, second = _application(client), _application(client, "https://jobs.example.test/2")
    foreign = _ready_document(second["id"])
    unready = db.create_document(first["id"], "cv", 1, {"kind": "cv"}, {},
                                 {"readability": 100, "ready": True}, [])

    foreign_response = client.post(f"/api/applications/{first['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": foreign["id"],
    })
    unready_response = client.post(f"/api/applications/{first['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": unready["id"],
    })

    assert foreign_response.status_code == 400 and "cette candidature" in foreign_response.json()["detail"]
    assert unready_response.status_code == 409 and "prêt à envoyer" in unready_response.json()["detail"]


def test_manual_flow_exposes_local_help_and_confirms_exact_documents(client) -> None:
    candidate = profile.empty_profile()
    candidate["identity"]["email"] = "alice@example.test"
    candidate["standard_answers"]["disponibilite"] = {"question": "Disponibilité", "answer": "Septembre"}
    profile.save_profile(candidate)
    application = _application(client)
    cv = _ready_document(application["id"], "cv", 1)
    letter = _ready_document(application["id"], "lettre", 1)

    started = client.post(f"/api/applications/{application['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": cv["id"], "letter_document_id": letter["id"], "remember": True,
    })

    assert started.status_code == 200, started.text
    body = started.json()
    assert body["identity"]["email"] == "alice@example.test"
    assert body["standard_answers"]["disponibilite"]["answer"] == "Septembre"
    assert {item["id"] for item in body["documents"]} == {cv["id"], letter["id"]}
    attempt_id = body["attempt"]["id"]
    assert client.get(f"/api/applications/{application['id']}/apply/attempts").json()[0]["id"] == attempt_id

    sent = client.post(f"/api/application-attempts/{attempt_id}/manual-sent")

    assert sent.status_code == 200 and sent.json()["status"] == "submitted"
    assert db.get_application(application["id"])["status"] == "envoyee"
    assert db.get_document(cv["id"])["status"] == "envoye"
    assert db.get_document(letter["id"])["status"] == "envoye"


def test_start_refuses_parallel_or_duplicate_submission(data_dir) -> None:
    application = db.create_application({"title": "Stage SOC", "company": "Acme",
                                         "url": "https://boards.greenhouse.io/acme/jobs/42"})
    cv = _ready_document(application["id"])
    first = application_flow.start(application["id"], mode="manual", cv_document_id=cv["id"],
                                   letter_document_id=None)
    with pytest.raises(db.Conflict, match="déjà ouverte"):
        application_flow.start(application["id"], mode="manual", cv_document_id=cv["id"],
                               letter_document_id=None)
    db.submit_application_attempt(first["attempt"]["id"])
    with pytest.raises(db.Conflict, match="déjà été envoyée"):
        application_flow.start(application["id"], mode="manual", cv_document_id=cv["id"],
                               letter_document_id=None)


def test_assisted_mode_is_queued_then_requests_manual_fallback(client) -> None:
    queue = application_queue.start_queue()
    application = _application(client)
    cv = _ready_document(application["id"])
    response = client.post(f"/api/applications/{application['id']}/apply/start", json={
        "mode": "assisted", "cv_document_id": cv["id"],
    })
    assert response.status_code == 200 and response.json()["attempt"]["status"] == "queued"
    attempt_id = response.json()["attempt"]["id"]
    try:
        assert queue.wait_idle(2)
        attempt = client.get(f"/api/application-attempts/{attempt_id}").json()
        assert attempt["status"] == "needs_user"
        assert attempt["intervention"]["action"] == "manual_fallback"
        retried = client.post(f"/api/application-attempts/{attempt_id}/retry")
        assert retried.status_code == 200 and retried.json()["status"] == "queued"
    finally:
        application_queue.stop_queue()


def test_deleting_application_cascades_attempt_and_selected_documents(client) -> None:
    application = _application(client)
    cv = _ready_document(application["id"])
    started = client.post(f"/api/applications/{application['id']}/apply/start", json={
        "mode": "manual", "cv_document_id": cv["id"],
    }).json()

    assert client.delete(f"/api/applications/{application['id']}").status_code == 200
    assert client.post(f"/api/application-attempts/{started['attempt']['id']}/manual-sent").status_code == 404


def test_provider_classification_rejects_non_http_urls() -> None:
    assert application_flow.provider_for("https://jobs.lever.co/acme/42") == "lever"
    assert application_flow.provider_for("https://jobs.smartrecruiters.com/Acme/42") == "smartrecruiters"
    assert application_flow.provider_for("https://acme.wd3.myworkdayjobs.com/jobs/job/42") == "workday"


def test_queue_processes_attempts_strictly_in_order(data_dir) -> None:
    attempts = []
    for number in range(3):
        application = db.create_application({"title": f"Stage {number}", "url": f"https://example.test/{number}"})
        document = _ready_document(application["id"])
        attempts.append(db.create_application_attempt(
            application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
            letter_document_id=None, target_url=application["url"], provider="generic",
        ))
    seen = []

    def handler(attempt):
        seen.append(attempt["id"])
        db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                          intervention={"kind": "test"})

    queue = application_queue.ApplicationQueue(handler)
    queue.start()
    try:
        assert queue.wait_idle(2)
    finally:
        queue.stop()
    assert seen == [attempt["id"] for attempt in attempts]
    assert all(db.get_application_attempt(attempt["id"])["status"] == "needs_user" for attempt in attempts)


def test_queue_recovers_interrupted_attempt_and_records_worker_failure(data_dir) -> None:
    application = db.create_application({"title": "Stage", "url": "https://example.test/job"})
    document = _ready_document(application["id"])
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider="generic",
    )
    assert db.claim_next_application_attempt()["status"] == "running"

    def broken(_attempt):
        raise RuntimeError("échec simulé")

    queue = application_queue.ApplicationQueue(broken)
    assert queue.start() == 1
    try:
        assert queue.wait_idle(2)
    finally:
        queue.stop()
    failed = db.get_application_attempt(attempt["id"])
    assert failed["status"] == "failed" and "échec simulé" in failed["error"]


def test_review_approval_is_strict_and_requeues_with_marker(client) -> None:
    application = _application(client)
    document = _ready_document(application["id"])
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider="greenhouse",
    )
    db.claim_next_application_attempt()
    db.transition_application_attempt(
        attempt["id"], expected={"running"}, status="needs_user",
        intervention={"kind": "review_required", "message": "Vérifier"},
    )

    approved = client.post(f"/api/application-attempts/{attempt['id']}/approve")

    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"
    assert approved.json()["intervention"] == {"kind": "review_approved"}


def test_assisted_failure_can_switch_to_manual_with_same_documents(client) -> None:
    application = _application(client)
    document = _ready_document(application["id"])
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider="greenhouse",
    )
    db.claim_next_application_attempt()
    db.transition_application_attempt(
        attempt["id"], expected={"running"}, status="needs_user",
        intervention={"kind": "browser_unavailable", "message": "Indisponible", "action": "manual_fallback"},
    )

    response = client.post(f"/api/application-attempts/{attempt['id']}/manual")

    assert response.status_code == 200
    assert db.get_application_attempt(attempt["id"])["status"] == "cancelled"
    assert response.json()["attempt"]["mode"] == "manual"
    assert response.json()["attempt"]["cv_document_id"] == document["id"]


def test_user_can_answer_only_the_detected_fields_and_resume(data_dir, monkeypatch) -> None:
    application = db.create_application({"title": "Stage", "url": "https://boards.greenhouse.io/acme/jobs/1"})
    document = _ready_document(application["id"])
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider="greenhouse",
    )
    db.claim_next_application_attempt()
    field = {"selector": '[name="availability"]', "label": "Disponibilité", "kind": "missing_required",
             "input_kind": "text", "options": []}
    db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                      intervention={"kind": "missing_fields", "fields": [field]})
    woke = []
    monkeypatch.setattr(application_queue, "wake", lambda: woke.append(True))

    resumed = application_flow.answer_fields(attempt["id"], [
        {"selector": field["selector"], "value": "Septembre 2027"},
    ])

    assert resumed["status"] == "queued" and woke == [True]
    assert resumed["answers"] == [{"selector": field["selector"], "label": "Disponibilité", "kind": "text",
                                    "value": "Septembre 2027", "sensitive": False,
                                    "source": "utilisateur"}]
    remembered = profile.load_profile()["standard_answers"]
    assert any(item["question"] == "Disponibilité" and item["answer"] == "Septembre 2027"
               for item in remembered.values())

    # Un formulaire multi-écran repart de son URL initiale : les réponses des
    # écrans précédents doivent donc rester disponibles pendant toute la reprise.
    db.claim_next_application_attempt()
    second = {"selector": '[name="authorization"]', "label": "Autorisation de travail",
              "kind": "missing_required", "input_kind": "select", "options": ["Oui", "Non"]}
    db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                      intervention={"kind": "missing_fields", "fields": [second]})
    resumed = application_flow.answer_fields(attempt["id"], [
        {"selector": second["selector"], "value": "Oui"},
    ])
    assert [item["selector"] for item in resumed["answers"]] == [field["selector"], second["selector"]]


def test_answering_an_undetected_field_is_rejected(data_dir) -> None:
    application = db.create_application({"title": "Stage", "url": "https://jobs.lever.co/acme/1"})
    document = _ready_document(application["id"])
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url=application["url"], provider="lever",
    )
    db.claim_next_application_attempt()
    db.transition_application_attempt(attempt["id"], expected={"running"}, status="needs_user",
                                      intervention={"kind": "missing_fields", "fields": [{
                                          "selector": '[name="legal"]', "label": "Question", "kind": "sensitive",
                                          "input_kind": "select", "options": ["Oui", "Non"],
                                      }]})
    with pytest.raises(ValueError, match="champ demandé"):
        application_flow.answer_fields(attempt["id"], [{"selector": '[name="other"]', "value": "Oui"}])
