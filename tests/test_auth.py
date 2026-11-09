import pytest

from jobradar import auth


def test_api_is_open_until_an_account_exists_then_requires_a_session(client) -> None:
    assert client.get("/api/applications").status_code == 200
    assert client.get("/api/auth/status").json() == {"account": False, "authenticated": False, "username": None}
    created = client.post("/api/auth/setup", json={"username": "camille", "password": "motdepasse-solide"})
    assert created.status_code == 200 and created.json()["authenticated"]
    assert "httponly" in created.headers["set-cookie"].lower() and "samesite=strict" in created.headers["set-cookie"].lower()
    assert client.get("/api/applications").status_code == 200  # cookie de session conservé
    assert client.post("/api/auth/setup", json={"username": "autre", "password": "motdepasse-2"}).status_code == 409

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/applications").status_code == 401
    assert client.get("/api/auth/status").json() == {"account": True, "authenticated": False, "username": None}
    assert client.post("/api/auth/login", json={"username": "camille", "password": "mauvais-mot"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "camille", "password": "motdepasse-solide"}).status_code == 200
    assert client.get("/api/applications").status_code == 200


def test_passwords_are_hashed_and_sessions_are_signed(data_dir) -> None:
    token = auth.create_account("camille", "motdepasse-solide")
    stored = (data_dir / "compte.json").read_text()
    assert "motdepasse-solide" not in stored and "password_hash" in stored
    assert auth.session_user(token) == "camille"
    forged = token.split(".")[0] + ".AAAA"
    assert auth.session_user(forged) is None and auth.session_user("n'importe-quoi") is None
    renewed = auth.change_password("motdepasse-solide", "nouveau-motdepasse")
    assert auth.session_user(token) is None  # changer de mot de passe déconnecte les autres sessions
    assert auth.session_user(renewed) == "camille"
    with pytest.raises(auth.AuthError):
        auth.login("camille", "motdepasse-solide")


def test_short_passwords_and_brute_force_are_refused(data_dir, monkeypatch) -> None:
    with pytest.raises(auth.AuthError, match="8 caractères"):
        auth.create_account("camille", "court")
    auth.create_account("camille", "motdepasse-solide")
    monkeypatch.setattr(auth.time, "sleep", lambda _s: None)
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login("camille", "faux-mot-de-passe")
    with pytest.raises(auth.AuthError, match="Trop d'essais"):
        auth.login("camille", "motdepasse-solide")
    auth._FAILURES.clear()
