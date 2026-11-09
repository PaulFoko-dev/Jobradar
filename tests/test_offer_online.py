"""Annonces retirées : connues au niveau de l'offre, masquées des résultats et exclues de l'auto-candidature."""
import sqlite3

from jobradar import config, db, migrations, pipeline, preparation, today


def _offer(search_id, make_offer, score=90, **fields) -> int:
    offer_id = make_offer(**fields)
    with db.connect() as conn:
        conn.execute("UPDATE offers SET score=? WHERE id=?", (score, offer_id))
    return offer_id


def _online(offer_id: int):
    with db.connect() as conn:
        return conn.execute("SELECT online FROM offers WHERE id=?", (offer_id,)).fetchone()[0]


def test_migration_marks_offers_whose_application_saw_the_ad_removed(tmp_path) -> None:
    path = tmp_path / "v16.db"
    with sqlite3.connect(path) as conn:
        for _version, migration in migrations.MIGRATIONS[:16]:
            migration(conn)
        conn.execute("PRAGMA user_version = 16")
        conn.execute("INSERT INTO searches(id, name, created_at) VALUES (1, 'Stage', 'x')")
        conn.execute("""INSERT INTO offers(id, search_id, dedup_key, title, url, links_json, first_seen, last_seen)
                        VALUES (91, 1, 'k', 'Stage Ingénieur Cybersécurité', 'https://fr.indeed.com/viewjob?jk=1', '[]', 'x', 'x')""")
        conn.execute("""INSERT INTO applications(title, offer_id, created_at, updated_at, offer_online, offer_checked_at_utc)
                        VALUES ('Stage', 91, 'x', 'x', 0, '2026-11-03T16:26:05Z')""")
        conn.commit()
        migrations.migrate(conn)
        assert conn.execute("SELECT online, online_checked_at_utc FROM offers WHERE id=91").fetchone() == (
            0, "2026-11-03T16:26:05Z")


def test_a_removal_seen_on_the_application_is_reported_on_the_offer(client, search_id, make_offer) -> None:
    offer_id = _offer(search_id, make_offer, title="Stage Ingénieur(e) Cybersécurité", company="Disneyland Paris")
    application = client.post("/api/applications", json={"offer_id": offer_id, "title": "Stage", "company": "Disney",
                                                         "url": "https://example.test/disney"}).json()
    db.set_archive_fields(application["id"], {"offer_online": 0, "offer_checked_at_utc": "2026-11-03T16:26:05Z"})
    assert _online(offer_id) == 0
    offer = next(item for item in db.list_offers(search_id) if item["id"] == offer_id)
    assert not preparation._safe_offer(offer)  # jamais choisie par l'auto-candidature


def test_scan_rechecks_relevant_offers_and_needs_more_than_an_aggregator(search_id, make_offer, monkeypatch) -> None:
    from jobradar import capture

    gone = _offer(search_id, make_offer, url="https://careers.example.com/job/1",
                  links=[{"source": "carrières", "url": "https://careers.example.com/job/1"}])
    aggregator_only = _offer(search_id, make_offer, url="https://fr.indeed.com/viewjob?jk=2",
                             links=[{"source": "indeed", "url": "https://fr.indeed.com/viewjob?jk=2"},
                                    {"source": "carrières", "url": "https://careers.example.com/job/2"}])
    alive = _offer(search_id, make_offer, url="https://careers.example.com/job/3",
                   links=[{"source": "carrières", "url": "https://careers.example.com/job/3"}])
    weak = _offer(search_id, make_offer, score=20, url="https://careers.example.com/job/4",
                  links=[{"source": "carrières", "url": "https://careers.example.com/job/4"}])
    answers = {"https://careers.example.com/job/1": False, "https://fr.indeed.com/viewjob?jk=2": False,
               "https://careers.example.com/job/2": None, "https://careers.example.com/job/3": True}
    calls = []
    monkeypatch.setattr(capture, "is_offer_online", lambda url, **_kw: calls.append(url) or answers.get(url))

    result = pipeline.check_offers_online(search_id)

    assert result == {"checked": 3, "removed": 1}
    assert _online(gone) == 0 and _online(aggregator_only) is None and _online(alive) == 1
    assert "https://careers.example.com/job/4" not in calls  # offre peu pertinente : pas vérifiée
    calls.clear()
    assert pipeline.check_offers_online(search_id) == {"checked": 0, "removed": 0} and calls == []  # une fois par jour
    assert _online(weak) is None


def test_today_ignores_removed_offers(search_id, make_offer) -> None:
    from datetime import datetime, timezone

    offer_id = _offer(search_id, make_offer, title="Stage analyste SOC")
    with db.connect() as conn:  # « Aujourd'hui » montre les offres des 7 derniers jours (horloge réelle)
        conn.execute("UPDATE offers SET first_seen=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), offer_id))
    assert any(item["id"] == offer_id for item in today.summary()["new_offers"])
    db.set_offer_online(offer_id, False)
    assert not any(item["id"] == offer_id for item in today.summary()["new_offers"])
    assert config.DATA_DIR.exists()


def test_old_robot_removals_are_backfilled_and_their_stuck_attempts_closed(client, search_id, make_offer) -> None:
    from jobradar import application_flow

    offer_id = _offer(search_id, make_offer, title="Auditeur stagiaire cyber", company="EY")
    application = client.post("/api/applications", json={"offer_id": offer_id, "title": "Auditeur stagiaire cyber",
                                                         "company": "EY", "url": "https://example.test/ey"}).json()
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {}, {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    attempt = db.create_application_attempt(application["id"], mode="assisted", status="needs_user",
                                            cv_document_id=document["id"], letter_document_id=None,
                                            target_url="https://example.test/ey", provider="generic")
    with db.connect() as conn:
        conn.execute("UPDATE application_attempts SET intervention_json=? WHERE id=?",
                     ('{"kind": "offer_removed", "message": "L\'offre n\'est plus en ligne.", "action": "manual_fallback"}',
                      attempt["id"]))

    assert application_flow.backfill_removed_offers() == 1
    assert _online(offer_id) == 0 and db.get_application(application["id"])["offer_online"] in (0, False)
    assert db.get_application_attempt(attempt["id"])["status"] == "cancelled"
    assert application_flow.backfill_removed_offers() == 0
