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
