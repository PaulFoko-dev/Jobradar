import base64
import os

import pytest

from jobradar import vault


@pytest.fixture
def fresh_vault(data_dir, monkeypatch):
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    yield
    vault.lock()


def test_values_are_encrypted_non_deterministically_and_bound_to_their_place(fresh_vault) -> None:
    vault.init()
    first, second = (vault.encrypt_value("Disponible en janvier", "applications.notes.1") for _ in range(2))
    assert first != second and first.startswith("jrv1:") and "janvier" not in first
    assert vault.decrypt_value(first, "applications.notes.1") == "Disponible en janvier"
    with pytest.raises(vault.VaultError):
        vault.decrypt_value(first, "applications.notes.2")  # déplacée vers une autre ligne : refusée


def test_tampering_and_wrong_keys_are_detected(fresh_vault, monkeypatch) -> None:
    vault.init()
    token = vault.encrypt_value("secret", "p")
    raw = bytearray(base64.urlsafe_b64decode(token[5:]))
    raw[-1] ^= 1
    with pytest.raises(vault.VaultError):
        vault.decrypt_value("jrv1:" + base64.urlsafe_b64encode(bytes(raw)).decode(), "p")
    vault.lock()
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    with pytest.raises(vault.VaultError):
        vault.decrypt_value(token, "p")


def test_recovery_key_restores_access_after_losing_the_master_key(fresh_vault, monkeypatch) -> None:
    recovery = vault.init()
    token = vault.encrypt_value("faits privés", "private_facts.value.1")
    file_blob = vault.encrypt_bytes(b"%PDF cv", "candidatures/cv.pdf")
    vault.lock()
    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())  # trousseau perdu
    assert not vault.status()["unlocked"]
    with pytest.raises(vault.VaultError):
        vault.encrypt_value("x", "p")
    vault.unlock_with_recovery(recovery.lower().replace("-", " "))
    assert vault.decrypt_value(token, "private_facts.value.1") == "faits privés"
    assert vault.decrypt_bytes(file_blob, "candidatures/cv.pdf") == b"%PDF cv"


def test_recovery_key_checksum_rejects_typos(fresh_vault) -> None:
    recovery = vault.init()
    typo = ("B" if recovery[0] != "B" else "C") + recovery[1:]
    with pytest.raises(vault.VaultError):
        vault.unlock_with_recovery(typo)


def test_rotation_keeps_data_readable_and_vault_file_holds_no_clear_key(data_dir, monkeypatch) -> None:
    keyring: dict = {}  # trousseau simulé en mémoire

    def call(method, service, account, *value):
        if method == "set_password":
            keyring[(service, account)] = value[0]
        return keyring.get((service, account))

    monkeypatch.delenv(vault.ENV_KEY, raising=False)
    monkeypatch.setattr(vault.mailer, "_keyring_call", call)
    vault.lock()
    recovery = vault.init()
    token = vault.encrypt_value("valeur", "p")
    before = dict(keyring)
    vault.rotate_master()
    assert keyring != before
    vault.lock()
    assert vault.decrypt_value(token, "p") == "valeur"  # rouverte avec la nouvelle clé principale
    stored = (data_dir / "vault.json").read_text()
    assert recovery.replace("-", "") not in stored.replace("-", "")
    assert all(secret not in stored for secret in keyring.values())
    vault.lock()


def test_rotation_refuses_an_environment_key(fresh_vault) -> None:
    vault.init()
    with pytest.raises(vault.VaultError):
        vault.rotate_master()


def test_status_and_consent(fresh_vault) -> None:
    assert vault.status() == {"initialized": False, "unlocked": False, "recovery_confirmed": False, "cv_consent": False}
    vault.init()
    vault.confirm_recovery()
    assert vault.set_cv_consent(True)["cv_consent"] and vault.cv_consent()
    assert not vault.set_cv_consent(False)["cv_consent"]
    assert vault.status()["unlocked"] and vault.status()["recovery_confirmed"]
