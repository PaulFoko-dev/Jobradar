"""Export CSV des candidatures et sauvegardes ZIP de JobRadar (base, configuration, archives)."""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db, workflow
from .archive import STATUS_LABELS

CSV_COLUMNS = (
    "Poste",
    "Entreprise",
    "Lieu",
    "Statut",
    "Score",
    "Date d'ajout",
    "Date d'envoi",
    "Relance",
    "Nombre d'entretiens",
    "Lien de l'offre",
    "Dossier d'archive",
    "Notes",
)

BACKUP_PREFIX = "JobRadar-sauvegarde-"
_BACKUP_NAME = re.compile(r"JobRadar-sauvegarde-\d{8}-\d{6}(?:-\d+)?\.zip")
# Fichiers d'une écriture atomique en cours dans les archives : jamais sauvegardés.
_TEMPORARY = re.compile(r"\..*\.tmp")


def csv_safe_cell(value: object) -> str:
    """Convertit une cellule et neutralise les formules (``=``, ``+``, ``-``, ``@``, même après des blancs)."""
    if value is None:
        return ""
    text = str(value)
    if text[:1] in ("\t", "\r") or text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _local(value: object, zone: ZoneInfo | None) -> str:
    """Instant ISO → « AAAA-MM-JJ HH:MM » dans le fuseau métier, lisible par Excel."""
    if not value:
        return ""
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if zone is not None and instant.tzinfo is not None:
        instant = instant.astimezone(zone)
    return instant.strftime("%Y-%m-%d %H:%M")


def _application_row(application: Mapping[str, Any], zone: ZoneInfo | None) -> tuple[object, ...]:
    follow_up = application.get("follow_up")
    due_at = follow_up.get("due_at") if isinstance(follow_up, Mapping) else ""
    status = application.get("status")
    return (
        application.get("title"),
        application.get("company"),
        application.get("location"),
        STATUS_LABELS.get(status, status),
        application.get("score"),
        _local(application.get("created_at"), zone),
        _local(application.get("applied_at") or application.get("applied_at_utc"), zone),
        _local(due_at, zone),
        application.get("interviews_count", 0),
        application.get("url"),
        application.get("archive_dir"),
        application.get("notes"),
    )


def applications_csv(applications: Iterable[Mapping[str, Any]], zone: ZoneInfo | None = None) -> bytes:
    """Produit un CSV pour Excel : UTF-8 avec BOM, séparateur point-virgule et CRLF."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for application in applications:
        writer.writerow(csv_safe_cell(value) for value in _application_row(application, zone))
    return stream.getvalue().encode("utf-8-sig")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: datetime | None) -> datetime:
    instant = at or _utc_now()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("La date de sauvegarde doit inclure un fuseau horaire.")
    return instant


def _utc_text(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _backup_dir(data_dir: Path) -> Path:
    return data_dir / "sauvegardes"


def _reserve_backup_path(directory: Path, instant: datetime) -> Path:
    stem = BACKUP_PREFIX + instant.strftime("%Y%m%d-%H%M%S")
    suffix: int | None = None
    while True:
        ending = "" if suffix is None else f"-{suffix}"
        candidate = directory / f"{stem}{ending}.zip"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix = 2 if suffix is None else suffix + 1
            continue
        os.close(descriptor)
        return candidate


def _sqlite_snapshot(database_path: Path, destination: Path) -> None:
    if not database_path.is_file() or database_path.is_symlink():
        raise FileNotFoundError(f"Base SQLite introuvable : {database_path}")
    source = sqlite3.connect(database_path, timeout=30)
    target = sqlite3.connect(destination)
    try:
        # API de sauvegarde : instantané cohérent même pendant une écriture (jamais de copie brute).
        source.backup(target)
        # Fichier autonome : la copie restaurée n'attend aucun journal -wal à côté d'elle.
        target.execute("PRAGMA journal_mode = DELETE")
    finally:
        target.close()
        source.close()


def _excluded_paths(data_dir: Path, database_path: Path, config_path: Path) -> tuple[Path, ...]:
    paths = [data_dir / name for name in ("secrets.json", "navigateur", "sauvegardes", "claude_cwd")]
    paths += [database_path, Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm"), config_path]
    return tuple(path.resolve() for path in paths)


def _is_excluded(path: Path, exclusions: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == excluded or resolved.is_relative_to(excluded) for excluded in exclusions)


def _archive_files(
    zipped: zipfile.ZipFile,
    archive_dir: Path,
    data_dir: Path,
    database_path: Path,
    config_path: Path,
) -> None:
    root_name = Path("candidatures")
    if not archive_dir.exists():
        zipped.writestr(root_name.as_posix() + "/", b"")
        return
    if not archive_dir.is_dir() or archive_dir.is_symlink():
        raise ValueError(f"Dossier d'archives invalide : {archive_dir}")

    exclusions = _excluded_paths(data_dir, database_path, config_path)
    for current, directories, files in os.walk(archive_dir, followlinks=False):
        directory = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not (directory / name).is_symlink()
            and not _TEMPORARY.fullmatch(name)  # capture en cours dans un dossier temporaire
            and not _is_excluded(directory / name, exclusions)
        )
        relative = directory.relative_to(archive_dir)
        zipped.write(directory, (root_name / relative).as_posix() + "/")
        for name in sorted(files):
            path = directory / name
            if _TEMPORARY.fullmatch(name) or path.is_symlink() or _is_excluded(path, exclusions):
                continue
            try:
                zipped.write(path, (root_name / relative / name).as_posix())
            except FileNotFoundError:  # remplacé ou déplacé pendant la sauvegarde
                continue


def create_backup(
    *,
    data_dir: Path,
    archive_dir: Path,
    database_path: Path | None = None,
    config_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, object]:
    """Crée un ZIP (écrit à part puis renommé) : instantané SQLite, config.json et archives, ``_retirees`` comprises.

    ``at`` fixe l'horodatage du nom de fichier, dans le fuseau qu'il porte (heure locale de l'utilisateur).
    """
    data_dir = data_dir.resolve()
    archive_dir = Path(archive_dir)
    database_path = Path(database_path or data_dir / "jobradar.db")
    config_path = Path(config_path or data_dir / "config.json")
    if not config_path.is_file() or config_path.is_symlink():
        raise FileNotFoundError(f"Configuration introuvable : {config_path}")

    instant = _aware(at)
    directory = _backup_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = _reserve_backup_path(directory, instant)
    fd, temporary_name = tempfile.mkstemp(prefix=".jobradar-backup-", suffix=".tmp", dir=directory)
    os.close(fd)
    temporary = Path(temporary_name)
    completed = False
    try:
        # Dossier de travail à côté des sauvegardes (exclu) : la copie de la base ne passe pas par /tmp.
        with tempfile.TemporaryDirectory(prefix=".jobradar-base-", dir=directory) as work:
            database_snapshot = Path(work) / "jobradar.db"
            _sqlite_snapshot(database_path, database_snapshot)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                zipped.write(database_snapshot, "jobradar.db")
                zipped.write(config_path, "config.json")
                _archive_files(zipped, archive_dir, data_dir, database_path, config_path)
        os.replace(temporary, destination)
        completed = True
    finally:
        temporary.unlink(missing_ok=True)
        if not completed:
            destination.unlink(missing_ok=True)

    return {
        "path": destination.name,
        "size_bytes": destination.stat().st_size,
        "created_at": _utc_text(instant),
    }


def backup() -> dict[str, object]:
    """Sauvegarde des données courantes (route ``POST /api/backup`` et ``cli backup``)."""
    config.load_config()  # config.json existe toujours dans la sauvegarde
    local = db.utcnow().astimezone(workflow.validate_timezone(config.settings().timezone))
    return create_backup(data_dir=config.DATA_DIR, archive_dir=config.archive_dir(), database_path=config.DB_PATH,
                         config_path=config.CONFIG_PATH, at=local)


def resolve_backup(*, data_dir: Path, name: str) -> Path:
    """Valide strictement un nom de sauvegarde et renvoie uniquement un ZIP local régulier."""
    if not isinstance(name, str) or _BACKUP_NAME.fullmatch(name) is None:
        raise ValueError("Nom de sauvegarde invalide.")
    directory = _backup_dir(data_dir.resolve())
    path = directory / name
    if path.parent != directory or path.is_symlink() or not path.is_file() or not zipfile.is_zipfile(path):
        raise FileNotFoundError("Sauvegarde introuvable.")
    return path


def list_backups(*, data_dir: Path) -> list[dict[str, object]]:
    """Liste les sauvegardes téléchargeables, de la plus récente à la plus ancienne."""
    directory = _backup_dir(data_dir.resolve())
    if not directory.is_dir():
        return []
    backups = []
    for path in directory.iterdir():
        try:
            valid = resolve_backup(data_dir=data_dir, name=path.name)
        except (FileNotFoundError, ValueError):
            continue
        info = valid.stat()
        backups.append((info.st_mtime_ns, {
            "path": valid.name,
            "size_bytes": info.st_size,
            "created_at": _utc_text(datetime.fromtimestamp(info.st_mtime, timezone.utc)),
        }))
    return [item for _, item in sorted(backups, key=lambda pair: pair[0], reverse=True)]
