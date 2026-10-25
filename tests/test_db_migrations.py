"""Tests isolés des migrations : aucune base utilisateur n'est ouverte."""

import sqlite3
import threading
import time

import pytest

from jobradar import db
from jobradar import migrations as migration_module
from jobradar.migrations import LATEST_SCHEMA_VERSION, migrate


def _connection(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_new_database_reaches_latest_version(tmp_path):
    with _connection(tmp_path / "new.db") as conn:
        migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert {row["name"] for row in conn.execute("PRAGMA table_info(offers)")} >= {"id", "logo"}
        fk = conn.execute("PRAGMA foreign_key_list(applications)").fetchall()
        assert any(row["table"] == "offers" and row["on_delete"] == "SET NULL" for row in fk)


def test_migration_preserves_legacy_data_and_is_idempotent(tmp_path):
    with _connection(tmp_path / "legacy.db") as conn:
        # Schéma minimal représentatif d'une base antérieure aux colonnes logo.
        from jobradar.migrations import _create_initial_schema

        _create_initial_schema(conn)
        conn.execute(
            "INSERT INTO searches(id, name, created_at) VALUES (1, 'Stage', '2026-01-01')"
        )
        conn.execute(
            """INSERT INTO offers(
                   id, search_id, dedup_key, title, url, first_seen, last_seen
               ) VALUES (10, 1, 'key', 'Analyste', 'https://example.test', 'a', 'b')"""
        )
        conn.execute(
            """INSERT INTO applications(
                   id, offer_id, title, notes, history_json, created_at, updated_at
               ) VALUES (20, 10, 'Analyste', 'à conserver', '[{"status":"envoyee"}]', 'a', 'b')"""
        )
        conn.commit()

        migrate(conn)
        migrate(conn)

        application = conn.execute("SELECT * FROM applications WHERE id = 20").fetchone()
        assert application["offer_id"] == 10
        assert application["notes"] == "à conserver"
        assert application["logo"] is None
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


def test_orphaned_legacy_application_is_preserved_and_detached(tmp_path):
    with _connection(tmp_path / "orphan.db") as conn:
        from jobradar.migrations import _create_initial_schema

        _create_initial_schema(conn)
        conn.execute(
            """INSERT INTO applications(
                   id, offer_id, title, notes, created_at, updated_at
               ) VALUES (1, 999, 'Stage sécurité', 'historique utile', 'a', 'b')"""
        )
        conn.commit()

        migrate(conn)

        row = conn.execute("SELECT * FROM applications WHERE id = 1").fetchone()
        assert row["offer_id"] is None
        assert row["title"] == "Stage sécurité"
        assert row["notes"] == "historique utile"


def test_delete_search_keeps_application(data_dir):
    search_id = db.save_search({"name": "Stage"})
    db.upsert_offer(
        search_id,
        {
            "dedup_key": "one",
            "title": "Stage cybersécurité",
            "url": "https://example.test/job",
            "links": [{"url": "https://example.test/job", "apply": True}],
        },
    )
    with db.connect() as conn:
        offer_id = conn.execute("SELECT id FROM offers").fetchone()[0]
    application = db.create_application({"offer_id": offer_id, "notes": "Conserver"})

    db.delete_search(search_id)

    with db.connect() as conn:
        saved = conn.execute("SELECT * FROM applications WHERE id = ?", (application["id"],)).fetchone()
        assert saved["offer_id"] is None
        assert saved["notes"] == "Conserver"
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0


def test_future_database_version_is_rejected_without_changes(tmp_path):
    with _connection(tmp_path / "future.db") as conn:
        future_version = LATEST_SCHEMA_VERSION + 1
        conn.execute(f"PRAGMA user_version = {future_version}")

        with pytest.raises(RuntimeError, match=r"version maximale prise en charge"):
            migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 0


def test_failed_migration_rolls_back_schema_data_and_version(monkeypatch, tmp_path):
    def failing_migration(conn):
        conn.execute("CREATE TABLE must_be_rolled_back (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO must_be_rolled_back VALUES (1)")
        raise RuntimeError("échec simulé")

    monkeypatch.setattr(migration_module, "MIGRATIONS", ((1, failing_migration),))
    monkeypatch.setattr(migration_module, "LATEST_SCHEMA_VERSION", 1)

    with _connection(tmp_path / "rollback.db") as conn:
        with pytest.raises(RuntimeError, match="échec simulé"):
            migration_module.migrate(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'must_be_rolled_back'"
        ).fetchone()[0] == 0


def test_concurrent_connections_recheck_version_after_write_lock(monkeypatch, tmp_path):
    """La seconde connexion ne rejoue pas une migration décidée avant le verrou.

    La première migration reste volontairement ouverte pendant que la seconde
    connexion entre dans ``migrate``. Une lecture de version faite avant
    ``BEGIN IMMEDIATE`` conduirait ensuite à un second INSERT et à une erreur
    UNIQUE ; la lecture sous verrou voit au contraire la version validée.
    """
    first_has_lock = threading.Event()
    release_first = threading.Event()
    calls_lock = threading.Lock()
    call_count = 0

    def insert_once(conn):
        nonlocal call_count
        with calls_lock:
            call_count += 1
            is_first = call_count == 1
        conn.execute("CREATE TABLE migration_marker (value TEXT UNIQUE)")
        conn.execute("INSERT INTO migration_marker VALUES ('once')")
        if is_first:
            first_has_lock.set()
            assert release_first.wait(timeout=5)

    monkeypatch.setattr(migration_module, "MIGRATIONS", ((1, insert_once),))
    monkeypatch.setattr(migration_module, "LATEST_SCHEMA_VERSION", 1)
    database = tmp_path / "concurrent.db"
    errors = []

    def run_migration():
        try:
            with _connection(database) as conn:
                conn.execute("PRAGMA busy_timeout = 5000")
                migration_module.migrate(conn)
        except Exception as exc:  # L'assertion finale rend toute erreur visible.
            errors.append(exc)

    first = threading.Thread(target=run_migration)
    second = threading.Thread(target=run_migration)
    first.start()
    assert first_has_lock.wait(timeout=5)
    second.start()
    # Laisse à la seconde connexion le temps d'atteindre le verrou SQLite.
    time.sleep(0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert call_count == 1
    with _connection(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM migration_marker").fetchone()[0] == 1
