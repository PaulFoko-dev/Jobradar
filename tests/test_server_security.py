import base64
import os

import pytest

from jobradar import vault


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    yield
    vault.lock()


def test_foreign_origin_and_host_cannot_change_data(client) -> None:
    payload = {"title": "Stage", "company": "Acme", "url": "https://example.com/offre"}
    assert client.post("/api/applications", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/applications", json=payload, headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/applications", json=payload,
                       headers={"Origin": "http://127.0.0.1:8765"}).status_code in {200, 201}
    assert client.get("/api/applications", headers={"Host": "evil.example"}).status_code == 200  # lecture seule


def test_vault_routes_never_return_keys(client, env_key) -> None:
    assert client.get("/api/vault").json()["initialized"] is False
    created = client.post("/api/vault/init").json()
    recovery = created.pop("recovery_key")
    assert created == {"initialized": True, "unlocked": True, "recovery_confirmed": False, "cv_consent": False}
    assert client.post("/api/vault/init").status_code == 409  # jamais une seconde clé de récupération
    assert client.put("/api/vault/consent", json={"consent": True}).json()["cv_consent"] is True
    status = client.get("/api/vault").json()
    assert recovery not in str(status) and "wrapped" not in str(status)
    assert client.post("/api/vault/unlock", json={"recovery_key": "AAAA-" * 12}).status_code == 400
    assert client.post("/api/vault/unlock", json={"recovery_key": recovery}).json()["unlocked"] is True


def test_full_migration_requires_a_confirmed_recovery_key(client, env_key) -> None:
    client.post("/api/applications", json={"title": "Stage", "company": "Acme", "url": "https://example.com/o"})
    client.post("/api/vault/init")
    assert client.post("/api/vault/migrate").status_code == 409
    client.post("/api/vault/confirm-recovery")
    done = client.post("/api/vault/migrate").json()
    assert done["unlocked"] and done["values"] >= 1
    assert client.post("/api/vault/migrate").json()["values"] == 0


def test_a_locked_vault_gives_a_clear_423_instead_of_a_server_error(client, env_key, monkeypatch) -> None:
    client.post("/api/applications", json={"title": "Stage", "company": "Acme", "url": "https://example.com/o"})
    client.post("/api/vault/init")
    client.post("/api/vault/confirm-recovery")
    client.post("/api/vault/migrate")
    vault.lock()
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())  # clé perdue
    response = client.get("/api/applications")
    assert response.status_code == 423 and "clé de récupération" in response.json()["detail"]
