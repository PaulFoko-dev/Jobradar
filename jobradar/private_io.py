"""Fichiers privés chiffrés au repos : profil (``data/profil``) et archives des candidatures (``candidatures/``).

Dès que le coffre existe, toute écriture sous ces dossiers est chiffrée (``vault.encrypt_bytes``) et toute lecture
déchiffre de façon transparente un fichier qui porte l'en-tête du coffre ; un fichier encore en clair est lu tel quel
(migration progressive, données antérieures au coffre). Les fichiers produits directement par le navigateur
(captures, PDF) sont scellés après coup par ``seal_tree``. Un document à déposer dans un formulaire est déchiffré
dans un dossier temporaire privé (0700), supprimé dès la fin de la candidature.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import config

PURPOSE = "jobradar-fichier-prive"
MAGIC = b"JRV1"
SKIPPED_SUFFIXES = (".tmp", ".lock", ".part")


def protected_roots() -> list[Path]:
    return [config.DATA_DIR / "profil", config.archive_dir()]


def is_protected(path: Path) -> bool:
    resolved = Path(os.path.abspath(path))
    return any(resolved == root or root in resolved.parents
               for root in (Path(os.path.abspath(item)) for item in protected_roots()))


def _vault_active() -> bool:
    from . import vault

    return vault._path().is_file()


def seal(path: Path, content: bytes) -> bytes:
    """Contenu à écrire : chiffré sous un dossier protégé dès que le coffre existe (VaultLocked s'il est fermé)."""
    if content.startswith(MAGIC) or not is_protected(path) or not _vault_active():
        return content
    from . import vault

    return vault.encrypt_bytes(content, PURPOSE)


def open_content(content: bytes) -> bytes:
    if not content.startswith(MAGIC):
        return content
    from . import vault

    return vault.decrypt_bytes(content, PURPOSE)


def read_bytes(path: Path) -> bytes:
    return open_content(Path(path).read_bytes())


def read_text(path: Path, encoding: str = "utf-8") -> str:
    return read_bytes(path).decode(encoding)


def is_sealed(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def seal_file(path: Path) -> bool:
    """Chiffre un fichier en clair sur place, vérifie le déchiffrement, puis remplace l'original."""
    from . import profile

    if path.is_symlink() or not path.is_file() or path.name.endswith(SKIPPED_SUFFIXES) or is_sealed(path):
        return False
    plain = path.read_bytes()
    sealed = seal(path, plain)
    if sealed is plain:
        return False
    if open_content(sealed) != plain:  # vérification avant d'effacer le clair
        raise RuntimeError(f"Vérification du chiffrement échouée : {path.name}")
    profile.atomic_write(path, sealed)
    return True


def seal_tree(root: Path | None = None) -> int:
    """Scelle tous les fichiers en clair d'un dossier protégé (par défaut : tous). Reprenable à tout moment."""
    if not _vault_active():
        return 0
    count = 0
    for base in [root] if root is not None else protected_roots():
        if base is None or not Path(base).is_dir() or not is_protected(Path(base)):
            continue
        for directory, _dirs, files in os.walk(base):
            for name in files:
                count += seal_file(Path(directory) / name)
    return count


@contextlib.contextmanager
def plain_copies(files: dict[str, str]) -> Iterator[dict[str, str]]:
    """Copies déchiffrées des documents à déposer, dans un dossier 0700 supprimé en sortie, même en cas d'erreur."""
    if not any(is_sealed(Path(path)) for path in files.values()):
        yield dict(files)
        return
    directory = Path(tempfile.mkdtemp(prefix="jobradar-envoi-"))
    os.chmod(directory, 0o700)
    try:
        copies = {}
        for key, path in files.items():
            target = directory / Path(path).name  # le site voit le vrai nom du fichier
            target.write_bytes(read_bytes(Path(path)))
            os.chmod(target, 0o600)
            copies[key] = str(target)
        yield copies
    finally:
        shutil.rmtree(directory, ignore_errors=True)
