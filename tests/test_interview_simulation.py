"""Étape 4 : simulation guidée, évaluée une seule fois par question."""
from jobradar import ai, db

QUESTIONS = [
    {"question": "Présente ton projet SOC.", "category": "parcours", "why": "CV"},
    {"question": "Comment qualifies-tu une alerte ?", "category": "technique", "why": "Offre"},
]


def app(client) -> dict:
    return client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme"}).json()


def preparation(application_id: int) -> dict:
    return db.create_interview_preparation(application_id, None, {
        "likely_questions": QUESTIONS, "cv_points": [],
        "questions_to_ask": ["Question 1", "Question 2", "Question 3"], "checklist": [],
    }, None)


def feedback(**changes) -> dict:
    return {"clarity": 4, "structure": 3, "relevance": 5,
            "star": {"situation": True, "task": True, "action": True, "result": False},
            "strengths": ["Réponse précise"], "improvements": ["Ajouter le résultat"],
            "better_structure": "Contexte, action, résultat."} | changes


def test_simulation_requires_preparation(client) -> None:
    application = app(client)
    response = client.post(f"/api/applications/{application['id']}/interview-simulations", json={})
    assert response.status_code == 400
    assert "Crée d'abord" in response.json()["detail"]


def test_simulation_advances_one_question_at_a_time(client, monkeypatch) -> None:
    application = app(client)
    prep = preparation(application["id"])
    simulation = client.post(f"/api/applications/{application['id']}/interview-simulations",
                             json={"preparation_id": prep["id"]}).json()
    assert simulation["current_question"] == 0 and simulation["status"] == "active"
    monkeypatch.setattr(ai, "ask_ai", lambda prompt, schema, task: feedback())

    first = client.post(f"/api/interview-simulations/{simulation['id']}/answers", json={
        "question_index": 0, "answer": "J'ai construit un laboratoire et analysé les alertes.",
    })
    assert first.status_code == 200
    assert first.json()["current_question"] == 1
    assert first.json()["turns"][0]["feedback"]["clarity"] == 4

    duplicate = client.post(f"/api/interview-simulations/{simulation['id']}/answers", json={
        "question_index": 0, "answer": "Deuxième réponse concurrente",
    })
    assert duplicate.status_code == 409

    completed = client.post(f"/api/interview-simulations/{simulation['id']}/answers", json={
        "question_index": 1, "answer": "Je vérifie le contexte puis les indicateurs.",
    }).json()
    assert completed["status"] == "completed"
    assert completed["current_question"] == 2
    assert len(completed["turns"]) == 2


def test_invalid_ai_score_is_never_saved(client, monkeypatch) -> None:
    application = app(client)
    prep = preparation(application["id"])
    simulation = client.post(f"/api/applications/{application['id']}/interview-simulations",
                             json={"preparation_id": prep["id"]}).json()
    monkeypatch.setattr(ai, "ask_ai", lambda *_args: feedback(clarity=9))
    response = client.post(f"/api/interview-simulations/{simulation['id']}/answers", json={
        "question_index": 0, "answer": "Une réponse",
    })
    assert response.status_code == 400
    assert db.get_interview_simulation(simulation["id"])["turns"] == []


def test_preparation_of_another_application_is_rejected(client) -> None:
    first, second = app(client), app(client)
    prep = preparation(second["id"])
    response = client.post(f"/api/applications/{first['id']}/interview-simulations",
                           json={"preparation_id": prep["id"]})
    assert response.status_code == 400
    assert "ne correspond pas" in response.json()["detail"]
