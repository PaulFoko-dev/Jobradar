"""Coffre local des données privées : AES-256-GCM, clé de données enveloppée deux fois.

Hiérarchie des clés :
- la **clé de données** (DEK, 256 bits, aléatoire) chiffre toutes les valeurs et tous les fichiers privés ;
- elle est enveloppée par la **clé principale**, rangée dans le trousseau du système (ou, pour les tests et les
  déploiements automatisés, dans la variable ``JOBRADAR_VAULT_KEY``, en base64) ;
- elle est aussi enveloppée par la **clé de récupération** (256 bits), affichée une seule fois au format imprimable
  et seule voie de récupération sur une autre machine ou après la perte du trousseau.

``vault.json`` (dossier de données) ne contient que des clés chiffrées. Les données associées de chaque chiffrement
(``purpose``) lient une valeur à son emplacement (table, colonne, ligne) : déplacée ailleurs, elle est refusée.
Sans clé disponible, toute écriture privée lève ``VaultLocked``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, mailer, profile

SERVICE = "jobradar.private-vault"
ACCOUNT = "master-key-v1"
ENV_KEY = "JOBRADAR_VAULT_KEY"
FORMAT, VERSION = "jobradar-vault", 1
VALUE_PREFIX = "jrv1:"
FILE_MAGIC = b"JRV1"
NONCE_BYTES = 12
_LOCK = threading.RLock()
_DEK: bytes | None = None


class VaultError(RuntimeError):
    """Coffre absent, altéré ou clé invalide."""


class VaultLocked(VaultError):
    """Aucune clé disponible : lecture de ce qui est en mémoire seulement, aucune écriture privée."""


def _path() -> Path:
    return config.DATA_DIR / "vault.json"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise VaultError("Enveloppe chiffrée invalide.") from exc


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    if len(sealed) < NONCE_BYTES + 16:
        raise VaultError("Donnée chiffrée tronquée.")
    try:
        return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise VaultError("Donnée chiffrée altérée ou clé incorrecte.") from exc


def _aad(purpose: str) -> bytes:
    return f"{FORMAT}:{VERSION}:{purpose}".encode()


# ---------------------------------------------------------------- clé de récupération imprimable

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans 0/O ni 1/I : lisible une fois imprimé


def format_recovery_key(key: bytes) -> str:
    """32 octets + 2 octets de contrôle, en groupes de 4 caractères : « ABCD-EFGH-… »."""
    data = key + hashlib.sha256(key).digest()[:2]
    number, chars = int.from_bytes(data, "big"), []
    for _ in range((len(data) * 8 + 4) // 5):
        number, index = divmod(number, 32)
        chars.append(_RECOVERY_ALPHABET[index])
    text = "".join(reversed(chars))
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_recovery_key(text: str) -> bytes:
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum()).replace("0", "O").replace("1", "I")
    try:
        number = 0
        for char in cleaned:
            number = number * 32 + _RECOVERY_ALPHABET.index(char)
        data = number.to_bytes(34, "big")
    except (ValueError, OverflowError) as exc:
        raise VaultError("Clé de récupération illisible.") from exc
    key, check = data[:32], data[32:]
    if hashlib.sha256(key).digest()[:2] != check:
        raise VaultError("Clé de récupération incorrecte (somme de contrôle).")
    return key


# ---------------------------------------------------------------- clé principale

def _master_key() -> bytes | None:
    if os.environ.get(ENV_KEY):
        key = _unb64(os.environ[ENV_KEY])
        if len(key) != 32:
            raise VaultError(f"{ENV_KEY} doit contenir 32 octets en base64.")
        return key
    try:
        encoded = mailer._keyring_call("get_password", SERVICE, ACCOUNT)
    except mailer.SecretUnavailable:
        return None
    return _unb64(encoded) if encoded else None


def _store_master_key(key: bytes) -> None:
    if os.environ.get(ENV_KEY):
        return  # clé fournie par l'environnement : rien à ranger
    try:
        mailer._keyring_call("set_password", SERVICE, ACCOUNT, _b64(key))
    except mailer.SecretUnavailable as exc:
        raise VaultLocked("Trousseau du système indisponible : la clé principale ne peut pas être rangée.") from exc


# ---------------------------------------------------------------- état du coffre

def _read() -> dict | None:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise VaultError("vault.json illisible.") from exc
    if data.get("format") != FORMAT or data.get("version") != VERSION:
        raise VaultError("Format de coffre inconnu.")
    return data


def _write(data: dict) -> None:
    profile.atomic_write(_path(), json.dumps(data, indent=2).encode())
    try:
        os.chmod(_path(), 0o600)
    except OSError:
        pass


def status() -> dict:
    """État public minimal : aucune clé ni enveloppe n'en sort."""
    data = _read()
    if data is None:
        return {"initialized": False, "unlocked": False, "recovery_confirmed": False, "cv_consent": False}
    return {"initialized": True, "unlocked": _unlock_quietly(data), "recovery_confirmed": bool(data.get("recovery_confirmed")),
            "cv_consent": bool(data.get("cv_consent"))}


def _unlock_quietly(data: dict) -> bool:
    try:
        _data_key(data)
        return True
    except VaultError:
        return False


def init() -> str:
    """Crée le coffre et renvoie la clé de récupération, à afficher une seule fois."""
    global _DEK
    with _LOCK:
        if _read() is not None:
            raise VaultError("Le coffre existe déjà.")
        dek, master, recovery = os.urandom(32), _master_key() or os.urandom(32), os.urandom(32)
        _store_master_key(master)
        _write({"format": FORMAT, "version": VERSION, "created_at": _now(),
                "wrapped_by_master": _b64(_seal(master, dek, _aad("dek:master"))),
                "wrapped_by_recovery": _b64(_seal(recovery, dek, _aad("dek:recovery"))),
                "recovery_confirmed": False, "cv_consent": False})
        _DEK = dek
        return format_recovery_key(recovery)


def _data_key(data: dict | None = None) -> bytes:
    global _DEK
    with _LOCK:
        if _DEK is not None:
            return _DEK
        data = data or _read()
        if data is None:
            raise VaultLocked("Coffre non initialisé.")
        master = _master_key()
        if master is None:
            raise VaultLocked("Clé principale indisponible (trousseau verrouillé ou absent).")
        _DEK = _open(master, _unb64(data["wrapped_by_master"]), _aad("dek:master"))
        return _DEK


def unlock_with_recovery(recovery_text: str) -> None:
    """Après la perte du trousseau ou sur une autre machine : la clé de récupération réenveloppe la clé de données."""
    global _DEK
    with _LOCK:
        data = _read()
        if data is None:
            raise VaultError("Coffre non initialisé.")
        dek = _open(parse_recovery_key(recovery_text), _unb64(data["wrapped_by_recovery"]), _aad("dek:recovery"))
        master = os.urandom(32) if not os.environ.get(ENV_KEY) else _master_key()
        _store_master_key(master)
        data["wrapped_by_master"] = _b64(_seal(master, dek, _aad("dek:master")))
        _write(data)
        _DEK = dek


def lock() -> None:
    global _DEK
    with _LOCK:
        _DEK = None


def rotate_master() -> None:
    """Nouvelle clé principale ; la clé de données (et donc les données chiffrées) ne change pas."""
    if os.environ.get(ENV_KEY):
        raise VaultError(f"Clé principale fournie par {ENV_KEY} : change la variable pour la faire tourner.")
    with _LOCK:
        data = _read()
        dek = _data_key(data)
        master = os.urandom(32)
        _store_master_key(master)
        data["wrapped_by_master"] = _b64(_seal(master, dek, _aad("dek:master")))
        _write(data)


def _set_flag(name: str, value: bool) -> dict:
    with _LOCK:
        data = _read()
        if data is None:
            raise VaultError("Coffre non initialisé.")
        data[name] = bool(value)
        _write(data)
        return status()


def confirm_recovery() -> dict:
    return _set_flag("recovery_confirmed", True)


def set_cv_consent(value: bool) -> dict:
    """Consentement global et révocable à l'envoi du contenu professionnel des CV à l'IA."""
    return _set_flag("cv_consent", value)


def cv_consent() -> bool:
    try:
        data = _read()
    except VaultError:
        return False
    return bool(data and data.get("cv_consent"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- chiffrement des valeurs et des fichiers

def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(VALUE_PREFIX)


def encrypt_value(plaintext: str, purpose: str) -> str:
    """Texte -> « jrv1:… » ; ``purpose`` (ex. « applications.notes.42 ») lie la valeur à son emplacement."""
    return VALUE_PREFIX + _b64(_seal(_data_key(), plaintext.encode(), _aad(purpose)))


def decrypt_value(token: str, purpose: str) -> str:
    if not is_encrypted(token):
        raise VaultError("Valeur non chiffrée.")
    return _open(_data_key(), _unb64(token[len(VALUE_PREFIX):]), _aad(purpose)).decode()


def encrypt_bytes(plaintext: bytes, purpose: str) -> bytes:
    return FILE_MAGIC + _seal(_data_key(), plaintext, _aad(purpose))


def decrypt_bytes(sealed: bytes, purpose: str) -> bytes:
    if not sealed.startswith(FILE_MAGIC):
        raise VaultError("Fichier non chiffré par JobRadar.")
    return _open(_data_key(), sealed[len(FILE_MAGIC):], _aad(purpose))
