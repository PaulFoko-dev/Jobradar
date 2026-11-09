import base64
import os
import sqlite3
from pathlib import Path

import pytest

from jobradar import config, db, private_io, profile, vault

SECRET = "Note-Sentinelle-Privee"


@pytest.fixture
def vault_on(data_dir, monkeypatch):
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    vault.init()
    yield data_dir
    vault.lock()


def _raw(table: str, column: str) -> list:
    with sqlite3.connect(config.DB_PATH) as conn:
        return [row[0] for row in conn.execute(f"SELECT {column} FROM {table}")]


def _application(client) -> dict:
    return client.post("/api/applications", json={"title": "Stage", "company": "Acme",
                                                  "url": "https://example.com/o"}).json()


def test_private_columns_are_encrypted_on_disk_and_clear_in_the_app(client, vault_on) -> None:
    application = _application(client)
    client.patch(f"/api/applications/{application['id']}", json={"notes": SECRET})
    assert db.get_application(application["id"])["notes"] == SECRET
    stored = _raw("applications", "notes")[0]
    assert stored.startswith("jrv1:") and SECRET not in stored
    assert SECRET.encode() not in Path(config.DB_PATH).read_bytes()


def test_existing_clear_values_are_migrated_and_purged(client, data_dir, monkeypatch) -> None:
    application = _application(client)  # avant le coffre : en clair
    client.patch(f"/api/applications/{application['id']}", json={"notes": SECRET})
    assert _raw("applications", "notes") == [SECRET]
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    vault.init()
    assert db.encrypt_existing() >= 1
    assert db.encrypt_existing() == 0  # reprenable : rien à refaire
    assert db.get_application(application["id"])["notes"] == SECRET
    for path in Path(config.DB_PATH).parent.glob("jobradar.db*"):
        assert SECRET.encode() not in path.read_bytes(), path.name
    vault.lock()


def test_a_locked_vault_blocks_private_writes(client, vault_on, monkeypatch) -> None:
    application = _application(client)
    vault.lock()
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())  # mauvaise clé
    with pytest.raises((vault.VaultError, sqlite3.OperationalError)):
        db.update_application(application["id"], {"notes": SECRET})
    assert SECRET.encode() not in Path(config.DB_PATH).read_bytes()


def test_profile_files_are_encrypted_and_read_transparently(vault_on) -> None:
    data = profile.empty_profile()
    data["identity"]["full_name"] = "Zorglub Sentinelle"
    profile.save_profile(data)
    raw = profile.profile_path().read_bytes()
    assert raw.startswith(private_io.MAGIC) and b"Zorglub" not in raw
    assert profile.load_profile()["identity"]["full_name"] == "Zorglub Sentinelle"


def test_seal_tree_encrypts_browser_files_and_plain_copies_are_removed(vault_on) -> None:
    proof = config.archive_dir() / "2026-11-09_Acme" / "envoi"
    proof.mkdir(parents=True)
    (proof / "etape-1.png").write_bytes(b"\x89PNG Zorglub")
    (proof / "cv.pdf").write_bytes(b"%PDF Zorglub")
    assert private_io.seal_tree() == 2 and private_io.seal_tree() == 0
    assert b"Zorglub" not in (proof / "etape-1.png").read_bytes()
    with private_io.plain_copies({"cv": str(proof / "cv.pdf")}) as copies:
        copy = Path(copies["cv"])
        assert copy.name == "cv.pdf" and copy.read_bytes() == b"%PDF Zorglub"
        assert oct(copy.parent.stat().st_mode & 0o777) == "0o700"
    assert not copy.exists() and not copy.parent.exists()


def test_without_vault_nothing_changes(data_dir) -> None:
    path = config.archive_dir() / "x" / "f.txt"
    profile.atomic_write(path, b"clair")
    assert path.read_bytes() == b"clair" and private_io.seal_tree() == 0
