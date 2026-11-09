"""Connexion locale par identifiant et mot de passe.

Le compte (``data/compte.json``, droits 0600) contient l'identifiant, le mot de passe haché avec scrypt et un sel
aléatoire, et une clé de session aléatoire. La session est un cookie signé (HMAC-SHA256) qui porte l'identifiant et son
expiration : rien à garder en mémoire, elle survit au redémarrage du serveur. Tant qu'aucun compte n'existe, l'API
reste ouverte (premier lancement, tests) ; dès sa création, toute route ``/api`` exige une session valide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config, profile

COOKIE = "jobradar_session"
SESSION_DAYS = 30
USERNAME = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
MIN_PASSWORD = 8
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_FAILURES: dict[str, list[float]] = {}
_LOCK = threading.Lock()


class AuthError(ValueError):
    pass


def _path() -> Path:
    return config.DATA_DIR / "compte.json"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _read() -> dict | None:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AuthError("compte.json illisible.") from exc
    return data if isinstance(data, dict) and data.get("username") else None


def _write(data: dict) -> None:
    profile.atomic_write(_path(), json.dumps(data, indent=2).encode())
    try:
        os.chmod(_path(), 0o600)
    except OSError:
        pass


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Le mot de passe doit contenir au moins {MIN_PASSWORD} caractères.")


def account_exists() -> bool:
    return _read() is not None


def username() -> str | None:
    data = _read()
    return data["username"] if data else None


def create_account(name: str, password: str) -> str:
    """Premier compte uniquement ; renvoie le jeton de session."""
    with _LOCK:
        if _read() is not None:
            raise AuthError("Un compte existe déjà : connecte-toi.")
        if not USERNAME.fullmatch(name or ""):
            raise AuthError("Identifiant : 3 à 64 caractères (lettres, chiffres, . _ @ -).")
        _check_password(password)
        salt = os.urandom(16)
        _write({"username": name, "salt": _b64(salt), "password_hash": _b64(_hash(password, salt)),
                "session_secret": _b64(os.urandom(32)), "created_at": int(time.time())})
        return _token(name)


def _throttled(name: str) -> bool:
    now = time.time()
    recent = [moment for moment in _FAILURES.get(name, []) if now - moment < 300]
    _FAILURES[name] = recent
    return len(recent) >= 5  # 5 échecs en 5 minutes : pause


def login(name: str, password: str) -> str:
    data = _read()
    if data is None:
        raise AuthError("Aucun compte : crée d'abord ton compte.")
    with _LOCK:
        if _throttled(name):
            raise AuthError("Trop d'essais : réessaie dans quelques minutes.")
    expected = _unb64(data["password_hash"])
    valid = hmac.compare_digest(name or "", data["username"]) & hmac.compare_digest(
        _hash(password or "", _unb64(data["salt"])), expected)
    if not valid:
        with _LOCK:
            _FAILURES.setdefault(name, []).append(time.time())
        time.sleep(0.5)  # freine les essais en série
        raise AuthError("Identifiant ou mot de passe incorrect.")
    _FAILURES.pop(name, None)
    return _token(data["username"])


def change_password(current: str, new: str) -> str:
    data = _read()
    if data is None:
        raise AuthError("Aucun compte.")
    if not hmac.compare_digest(_hash(current or "", _unb64(data["salt"])), _unb64(data["password_hash"])):
        raise AuthError("Mot de passe actuel incorrect.")
    _check_password(new)
    salt = os.urandom(16)
    # Nouvelle clé de session : toutes les autres sessions ouvertes sont déconnectées.
    data.update({"salt": _b64(salt), "password_hash": _b64(_hash(new, salt)), "session_secret": _b64(os.urandom(32))})
    _write(data)
    return _token(data["username"])


def _token(name: str, expires: int | None = None) -> str:
    data = _read()
    expires = expires or int(time.time()) + SESSION_DAYS * 86400
    payload = f"{name}|{expires}"
    signature = hmac.new(_unb64(data["session_secret"]), payload.encode(), hashlib.sha256).digest()
    return f"{_b64(payload.encode())}.{_b64(signature)}"


def session_user(token: str | None) -> str | None:
    """Identifiant d'une session valide (signature, expiration, compte courant), sinon None."""
    data = _read()
    if not token or data is None or "." not in token:
        return None
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = _unb64(payload_part)
        expected = hmac.new(_unb64(data["session_secret"]), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature_part)):
            return None
        name, expires = payload.decode().rsplit("|", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    if int(expires) < time.time() or name != data["username"]:
        return None
    return name
