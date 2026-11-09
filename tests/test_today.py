from jobradar import db, today


def test_today_groups_what_needs_attention(client) -> None:
    application = client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme",
                                                         "url": "https://example.com/o"}).json()
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {}, {"readability": 100, "ready": True}, [])
    db.set_document_status(document["id"], "pret")
    data = today.summary()
    assert [item["id"] for item in data["ready_to_send"]] == [application["id"]]
    with db.connect() as conn:
        conn.execute("""INSERT INTO application_attempts(application_id, mode, status, cv_document_id, target_url,
                        provider, intervention_json, created_at_utc, updated_at_utc)
                        VALUES (?, 'assisted', 'needs_user', ?, 'https://example.com', 'generic',
                        '{"kind": "missing_fields"}', 'x', 'x')""", (application["id"], document["id"]))
    data = today.summary()
    assert data["interventions"][0]["label"] == "Répondre à des questions du formulaire"
    assert data["interventions"][0]["items"][0]["title"] == "Stage SOC"
    assert data["ready_to_send"] == []  # une session est ouverte : plus « prête à envoyer »
    assert client.get("/api/today").status_code == 200
