"""Tests des entretiens : création, modification, annulation et export iCalendar."""
import pytest

from jobradar import db

START = "2026-10-26T14:00:00+01:00"


@pytest.fixture
def app_id(data_dir) -> int:
    return db.create_application({"title": "Stage SOC", "company": "Acme", "status": "envoyee",
                                  "url": "https://example.test/offre"})["id"]


def _create(client, app_id: int, **fields: object):
    return client.post(f"/api/applications/{app_id}/interviews", json={"starts_at": START} | fields)


def _unfold(content: bytes) -> str:
    return content.replace(b"\r\n ", b"").decode("utf-8")


def test_creation_avec_valeurs_par_defaut(client, app_id) -> None:
    response = _create(client, app_id)
    assert response.status_code == 200
    interview = response.json()

    assert interview["uid"].endswith("@jobradar.local")
    assert {k: interview[k] for k in ("title", "starts_at", "ends_at", "starts_at_utc", "ends_at_utc",
                                      "duration_minutes", "timezone", "reminder_minutes", "sequence",
                                      "status", "location", "meeting_url", "notes")} == {
        "title": "Entretien — Acme",
        "starts_at": "2026-10-26T14:00:00+01:00",
        "ends_at": "2026-10-26T15:00:00+01:00",
        "starts_at_utc": "2026-10-26T13:00:00Z",
        "ends_at_utc": "2026-10-26T14:00:00Z",
        "duration_minutes": 60,
        "timezone": "Europe/Paris",
        "reminder_minutes": 1440,
        "sequence": 0,
        "status": "scheduled",
        "location": None,
        "meeting_url": None,
        "notes": "",
    }
    assert client.get(f"/api/applications/{app_id}/interviews").json() == [interview]
    assert db.get_application(app_id)["interviews_count"] == 1
    event = db.list_events(app_id)[-1]
    assert (event["type"], event["payload"]["interview_id"]) == ("interview_scheduled", interview["id"])


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"starts_at": "2026-10-26T14:00:00"}, "décalage"),
        ({"starts_at": "demain"}, "starts_at"),
        ({"duration_minutes": 10}, "duration_minutes"),
        ({"duration_minutes": 481}, "duration_minutes"),
        ({"meeting_url": "javascript:alert(1)"}, "http"),
        ({"meeting_url": "https://user:mdp@visio.test"}, "identifiant"),
        ({"timezone": "Mars/Olympus"}, "inconnu"),
        ({"reminder_minutes": -5}, "reminder_minutes"),
    ],
)
def test_creation_refuse_les_valeurs_invalides(client, app_id, fields, message) -> None:
    response = _create(client, app_id, **fields)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert client.get(f"/api/applications/{app_id}/interviews").json() == []


def test_creation_pour_une_candidature_inconnue(client) -> None:
    assert _create(client, 999).status_code == 404
    assert client.get("/api/applications/999/interviews").status_code == 404


def test_modification_incremente_la_sequence(client, app_id) -> None:
    created = _create(client, app_id, meeting_url="https://visio.test/abc", location="Paris").json()
    response = client.patch(f"/api/interviews/{created['id']}",
                            json={"duration_minutes": 90, "meeting_url": None, "title": "Entretien technique"})
    assert response.status_code == 200
    updated = response.json()

    assert updated["sequence"] == 1
    assert updated["ends_at"] == "2026-10-26T15:30:00+01:00"
    assert updated["meeting_url"] is None
    assert updated["location"] == "Paris"
    assert updated["title"] == "Entretien technique"
    assert updated["uid"] == created["uid"]

    moved = client.patch(f"/api/interviews/{created['id']}",
                         json={"starts_at": "2026-10-27T09:30:00-04:00", "timezone": "America/Montreal"}).json()
    assert moved["starts_at_utc"] == "2026-10-27T13:30:00Z"
    assert moved["starts_at"] == "2026-10-27T09:30:00-04:00"
    assert moved["duration_minutes"] == 90
    assert moved["sequence"] == 2


def test_modification_invalide_ou_inconnue(client, app_id) -> None:
    created = _create(client, app_id).json()
    assert client.patch(f"/api/interviews/{created['id']}", json={"meeting_url": "ftp://x"}).status_code == 400
    assert client.patch("/api/interviews/999", json={"notes": "x"}).status_code == 404
    assert client.get(f"/api/applications/{app_id}/interviews").json()[0]["sequence"] == 0


def test_export_ics(client, app_id) -> None:
    created = _create(client, app_id, meeting_url="https://visio.test/abc", location="Paris, La Défense",
                      notes="Apporter le portfolio").json()
    response = client.get(f"/api/interviews/{created['id']}.ics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"Entretien Acme 2026-10-26.ics\"; "
        "filename*=UTF-8''Entretien%20Acme%202026-10-26.ics"
    )
    plain = _unfold(response.content)
    assert f"UID:{created['uid']}" in plain
    assert "METHOD:PUBLISH" in plain
    assert "DTSTART:20261026T130000Z" in plain
    assert "DTEND:20261026T140000Z" in plain
    assert "SUMMARY:Entretien — Acme" in plain
    assert "LOCATION:Paris\\, La Défense" in plain
    assert "URL:https://visio.test/abc" in plain
    assert "TRIGGER:-P1D" in plain
    assert ("DESCRIPTION:Poste : Stage SOC\\nEntreprise : Acme\\nOffre : https://example.test/offre\\n"
            "Visioconférence : https://visio.test/abc\\n\\nApporter le portfolio") in plain
    assert str(db.config.DATA_DIR) not in plain


def test_export_ics_nom_de_fichier_unicode_et_sans_rappel(client, data_dir) -> None:
    app = db.create_application({"title": "Stage", "company": "Société Générale/CIB", "url": "file:///etc/passwd"})
    created = _create(client, app["id"], reminder_minutes=0).json()
    response = client.get(f"/api/interviews/{created['id']}.ics")
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''Entretien%20Soci%C3%A9t%C3%A9%20G%C3%A9n%C3%A9rale_CIB%202026-10-26.ics" in disposition
    assert 'filename="Entretien Soci_t_ G_n_rale_CIB 2026-10-26.ics"' in disposition
    plain = _unfold(response.content)
    assert "BEGIN:VALARM" not in plain
    assert "file://" not in plain


def test_annulation_conservee_pour_lexport(client, app_id) -> None:
    created = _create(client, app_id).json()
    cancelled = client.delete(f"/api/interviews/{created['id']}").json()
    assert (cancelled["status"], cancelled["sequence"]) == ("cancelled", 1)
    assert client.delete(f"/api/interviews/{created['id']}").json()["sequence"] == 1  # idempotent
    assert db.get_application(app_id)["interviews_count"] == 0

    plain = _unfold(client.get(f"/api/interviews/{created['id']}.ics").content)
    assert "METHOD:CANCEL" in plain
    assert "STATUS:CANCELLED" in plain
    assert "SEQUENCE:1" in plain

    response = client.patch(f"/api/interviews/{created['id']}", json={"notes": "x"})
    assert response.status_code == 409
    assert client.delete("/api/interviews/999").status_code == 404
    assert client.get("/api/interviews/999.ics").status_code == 404


def test_suppression_de_la_candidature_supprime_ses_entretiens(client, app_id) -> None:
    created = _create(client, app_id).json()
    client.delete(f"/api/applications/{app_id}")
    assert client.get(f"/api/interviews/{created['id']}.ics").status_code == 404
