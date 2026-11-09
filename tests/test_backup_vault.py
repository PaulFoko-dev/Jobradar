import base64
import os
import zipfile

import pytest

from jobradar import config, db, export, profile, vault


def test_encrypted_backup_restores_on_a_fresh_machine_with_the_recovery_key(client, data_dir, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    recovery = vault.init()
    data = profile.empty_profile()
    data["identity"]["full_name"] = "Zorglub Sentinelle"
    profile.save_profile(data)
    application = client.post("/api/applications", json={"title": "Stage", "company": "Acme",
                                                         "url": "https://example.com/o"}).json()
    db.update_application(application["id"], {"notes": "note privée"})
    backup = export.backup()
    path = data_dir / "sauvegardes" / backup["path"]
    with zipfile.ZipFile(path) as zipped:
        names = set(zipped.namelist())
        assert {"jobradar.db", "vault.json", "profil/profil.json", "sauvegarde.json"} <= names
        assert b"Zorglub" not in zipped.read("profil/profil.json")
        assert b"note priv" not in zipped.read("jobradar.db")

    # Nouvelle machine : autre dossier, trousseau vide (autre clé principale).
    fresh = tmp_path / "neuf"
    fresh.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", fresh)
    monkeypatch.setattr(config, "DB_PATH", fresh / "jobradar.db")
    monkeypatch.setenv("JOBRADAR_ARCHIVE_DIR", str(fresh / "candidatures"))
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    result = export.restore_backup(path, data_dir=fresh, archive_dir=fresh / "candidatures")
    assert result["encrypted"] and not vault.status()["unlocked"]
    vault.unlock_with_recovery(recovery)
    assert profile.load_profile()["identity"]["full_name"] == "Zorglub Sentinelle"
    assert db.get_application(application["id"])["notes"] == "note privée"
    with pytest.raises(FileExistsError):
        export.restore_backup(path, data_dir=fresh, archive_dir=fresh / "candidatures")
    vault.lock()


def test_backups_older_than_the_vault_are_flagged_and_deleted(data_dir, monkeypatch) -> None:
    backups = data_dir / "sauvegardes"
    backups.mkdir()
    old = backups / "avant-relance.db"
    old.write_bytes(b"SQLite clair")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    assert export.plaintext_backups(data_dir=data_dir) == []  # sans coffre : rien à signaler
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    vault.init()
    assert [item["name"] for item in export.plaintext_backups(data_dir=data_dir)] == ["avant-relance.db"]
    assert export.delete_plaintext_backups(data_dir=data_dir) == 1 and not old.exists()
    vault.lock()
