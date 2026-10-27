"""Identifiants de connexion par domaine, stockés sans jamais exposer les mots de passe."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from . import mailer

SERVICE = "jobradar-login"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


def normalize_domain(value: str) -> str:
    host = (urlsplit(value).hostname if "://" in value else value).strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        valid = True
    except ValueError:
        valid = host == "localhost" or bool(_HOST.fullmatch(host))
    if not valid:
        raise ValueError("Domaine invalide.")
    return host


def _covers(domain: str, host: str) -> bool:
    """Hôte exact ou sous-domaine du domaine enregistré : jamais un voisin sur un hébergement partagé."""
    return host == domain or host.endswith("." + domain)


def _index() -> dict:
    value = mailer._read_file().get("login_accounts", {})
    return value if isinstance(value, dict) else {}


def list_accounts() -> list[dict]:
    return [{"domain": domain, "username": str(item.get("username") or ""), "password_set": True}
            for domain, item in sorted(_index().items()) if isinstance(item, dict)]


def store(domain: str, username: str, password: str) -> dict:
    domain, username = normalize_domain(domain), str(username).strip()
    if not username or not password:
        raise ValueError("Domaine, identifiant et mot de passe sont obligatoires.")
    account = f"{domain}|{username}"
    try:
        mailer._keyring_call("set_password", SERVICE, account, password)
        backend = "keyring"
        secret = None
    except mailer.SecretUnavailable:
        backend, secret = "file", password
    data = mailer._read_file()
    index = data.get("login_accounts") if isinstance(data.get("login_accounts"), dict) else {}
    index[domain] = {"username": username, "account": account, "backend": backend}
    if secret is not None:
        index[domain]["password"] = secret
    data["login_accounts"] = index
    mailer._write_file(data)
    return {"domain": domain, "username": username, "password_set": True}


def delete(domain: str) -> None:
    domain = normalize_domain(domain)
    data, index = mailer._read_file(), _index()
    item = index.pop(domain, None)
    if item and item.get("backend") == "keyring":
        try:
            mailer._keyring_call("delete_password", SERVICE, item["account"])
        except mailer.SecretUnavailable:
            pass
    if index:
        data["login_accounts"] = index
    else:
        data.pop("login_accounts", None)
    if data:
        mailer._write_file(data)
    else:
        mailer._secrets_path().unlink(missing_ok=True)


def for_host(host: str) -> tuple[str, str] | None:
    host = normalize_domain(host)
    matches = [(domain, item) for domain, item in _index().items() if _covers(domain, host)]
    if not matches:
        return None
    # Le domaine enregistré le plus précis l'emporte.
    domain, item = max(matches, key=lambda match: len(match[0]))
    password = item.get("password") if item.get("backend") == "file" else None
    if not password:
        try:
            password = mailer._keyring_call("get_password", SERVICE, item["account"])
        except mailer.SecretUnavailable:
            password = None
    return (str(item.get("username") or ""), str(password)) if password else None
