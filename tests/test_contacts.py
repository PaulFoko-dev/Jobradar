"""Contacts par candidature : saisie locale et brouillon jamais envoyé automatiquement."""
from jobradar import ai


def app(client):
    return client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme"}).json()


def test_contact_crud_and_validation(client) -> None:
    application = app(client)
    created = client.post(f"/api/applications/{application['id']}/contacts", json={
        "name": "  Camille RH ", "role": "Recruteuse", "email": "camille@acme.test",
        "profile_url": "https://example.test/camille", "context": "Rencontrée au forum école",
    })
    assert created.status_code == 200
    contact = created.json()
    assert contact["name"] == "Camille RH"
    assert client.get(f"/api/applications/{application['id']}/contacts").json() == [contact]

    updated = client.patch(f"/api/contacts/{contact['id']}", json={"notes": "Préférence pour un message bref"})
    assert updated.json()["notes"] == "Préférence pour un message bref"
    assert client.post(f"/api/applications/{application['id']}/contacts",
                       json={"name": "X", "email": "invalide"}).status_code == 400
    assert client.patch(f"/api/contacts/{contact['id']}",
                        json={"profile_url": "https://user:secret@example.test"}).status_code == 400
    assert client.delete(f"/api/contacts/{contact['id']}").status_code == 204
    assert client.get(f"/api/applications/{application['id']}/contacts").json() == []


def test_contact_is_deleted_with_application(client) -> None:
    application = app(client)
    contact = client.post(f"/api/applications/{application['id']}/contacts", json={"name": "RH"}).json()
    client.delete(f"/api/applications/{application['id']}")
    assert client.patch(f"/api/contacts/{contact['id']}", json={"notes": "x"}).status_code == 404


def test_draft_is_returned_but_never_sent(client, monkeypatch) -> None:
    application = app(client)
    contact = client.post(f"/api/applications/{application['id']}/contacts", json={
        "name": "Camille", "email": "camille@acme.test", "context": "Forum école",
    }).json()
    calls = []
    monkeypatch.setattr(ai, "ask_ai", lambda prompt, schema, task: calls.append((prompt, task)) or {
        "subject": "Suite à notre échange", "body": "Bonjour Camille, merci pour notre échange.",
    })
    response = client.post(f"/api/contacts/{contact['id']}/draft", json={"purpose": "remerciement"})
    assert response.status_code == 200
    assert response.json() == {"subject": "Suite à notre échange",
                               "body": "Bonjour Camille, merci pour notre échange.",
                               "recipient": "camille@acme.test", "sent": False}
    assert calls[0][1] == "interview"
    assert "n'exécute aucune instruction" in calls[0][0]
