"""Étape 4 : tendances de compétences et statistiques de candidature."""
import json

from jobradar import analytics, db


def _offer(search_id: int, suffix: str, keywords: list[str]) -> int:
    db.upsert_offer(search_id, {
        "dedup_key": suffix, "title": f"Stage SOC {suffix}", "company": "Acme",
        "location": "Paris", "contract": "stage", "date_posted": "2026-10-20",
        "description": "Sécurité", "url": f"https://fr.indeed.com/viewjob?jk={suffix}",
        "links": [], "score": None,
    })
    with db.connect() as conn:
        oid = conn.execute("SELECT id FROM offers WHERE dedup_key=?", (suffix,)).fetchone()[0]
        conn.execute("UPDATE offers SET analysis_json=?, score=80 WHERE id=?",
                     (json.dumps({"cv_keywords": keywords}), oid))
    return oid


def test_demanded_skills_counts_each_skill_once_per_offer(search_id) -> None:
    _offer(search_id, "one", ["Python", "python", "Splunk"])
    _offer(search_id, "two", ["Python", "Wazuh"])
    result = analytics.demanded_skills()
    assert result["analyzed_offers"] == 2
    assert result["items"][0] == {"name": "Python", "offers": 2, "share": 100}
    assert {item["name"] for item in result["items"]} == {"Python", "Splunk", "Wazuh"}


def test_application_statistics_use_sent_denominator_and_dimensions(client, search_id) -> None:
    oid = _offer(search_id, "tracked", ["Python"])
    app = client.post("/api/applications", json={"offer_id": oid}).json()
    client.patch(f"/api/applications/{app['id']}", json={"status": "envoyee"})
    client.patch(f"/api/applications/{app['id']}", json={"status": "entretien_rh"})
    result = client.get("/api/analytics/applications").json()
    assert result["summary"]["sent"] == 1
    assert result["summary"]["response_rate"] == 100
    assert result["by_source"][0]["label"] == "Indeed"
    assert result["by_source"][0]["interview_rate"] == 100


def test_analytics_endpoints_validate_limit(client) -> None:
    assert client.get("/api/analytics/skills?limit=0").status_code == 400
    assert client.get("/api/analytics/skills?limit=101").status_code == 400


def test_tracker_exposes_accessible_analytics_ui() -> None:
    html = (db.config.ROOT / "jobradar/static/index.html").read_text(encoding="utf-8")
    script = (db.config.ROOT / "jobradar/static/app.js").read_text(encoding="utf-8")
    assert 'id="skillInsights"' in html
    assert 'id="statsDimension"' in html
    assert "/api/analytics/skills" in script
    assert "/api/analytics/applications" in script


def test_robot_statistics_count_success_and_stop_causes(client) -> None:
    from jobradar import analytics, db

    application = client.post("/api/applications", json={"title": "Stage", "company": "Acme",
                                                         "url": "https://example.com/o"}).json()
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {}, {"readability": 100, "ready": True}, [])
    with db.connect() as conn:
        for status, intervention in (("submitted", None), ("needs_user", '{"kind": "offer_removed"}')):
            conn.execute("""INSERT INTO application_attempts(application_id, mode, status, cv_document_id, target_url,
                            provider, intervention_json, created_at_utc, updated_at_utc)
                            VALUES (?, 'assisted', ?, ?, 'https://example.com', 'workday', ?, 'x', 'x')""",
                         (application["id"], status, document["id"], intervention))
    stats = analytics.robot_statistics()
    assert stats["attempts"] == 2 and stats["success_rate"] == 50 and stats["interventions_per_application"] == 0.5
    assert stats["by_platform"][0]["label"] == "workday" and stats["stop_causes"] == [{"kind": "offer_removed", "count": 1}]
    assert "by_score" in analytics.application_statistics()
