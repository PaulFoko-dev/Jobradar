"""Création sûre de fichiers iCalendar pour les entretiens JobRadar.

Le module ne dépend ni de la base de données ni du serveur web. Les dates reçues
doivent être explicites (avec fuseau) puis sont normalisées en UTC dans l'ICS.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

_CRLF = b"\r\n"
_WINDOWS_RESERVED = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _aware_utc(value: datetime, field: str) -> datetime:
    """Valide un instant avec fuseau et le convertit en UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} doit être un datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} doit inclure un fuseau horaire")
    return value.astimezone(timezone.utc)


def _ical_datetime(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    """Échappe une valeur TEXT RFC 5545 et neutralise les retours injectés."""
    if not isinstance(value, str):
        raise TypeError("les champs texte doivent être des chaînes")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _validate_identifier(uid: str) -> str:
    if not isinstance(uid, str):
        raise TypeError("uid doit être une chaîne")
    if not uid or uid != uid.strip():
        raise ValueError("uid ne doit être ni vide ni entouré d'espaces")
    if any(ord(character) < 32 or ord(character) == 127 for character in uid):
        raise ValueError("uid contient un caractère de contrôle")
    if len(uid.encode("utf-8")) > 255:
        raise ValueError("uid dépasse 255 octets")
    return _escape_text(uid)


def _validate_url(value: str) -> str:
    """N'accepte que des URL web absolues, sans identifiants intégrés."""
    if not isinstance(value, str):
        raise TypeError("preparation_url doit être une chaîne")
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError("preparation_url contient un caractère interdit")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("preparation_url doit être une URL HTTP(S) absolue")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("preparation_url ne doit pas contenir d'identifiants")
    try:
        _ = parsed.port  # urllib ne valide le port qu'à la lecture de cet attribut
    except ValueError as exc:
        raise ValueError("preparation_url contient un port invalide") from exc
    return value


def _duration_before_event(value: timedelta) -> str:
    if not isinstance(value, timedelta):
        raise TypeError("reminder_before doit être un timedelta")
    seconds = int(value.total_seconds())
    if seconds <= 0 or value != timedelta(seconds=seconds):
        raise ValueError("reminder_before doit être une durée positive en secondes entières")
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    date_part = f"{days}D" if days else ""
    time_part = ""
    if hours or minutes or seconds:
        time_part = f"T{hours or ''}{'H' if hours else ''}{minutes or ''}{'M' if minutes else ''}{seconds or ''}{'S' if seconds else ''}"
    return f"-P{date_part}{time_part}"


def _fold_line(line: str) -> list[bytes]:
    """Plie une ligne sans couper de caractère UTF-8 (75 octets maximum)."""
    folded: list[bytes] = []
    current = bytearray()
    limit = 75
    for character in line:
        encoded = character.encode("utf-8")
        if current and len(current) + len(encoded) > limit:
            folded.append(bytes(current))
            current = bytearray(b" ")
            limit = 75
        current.extend(encoded)
    folded.append(bytes(current))
    return folded


def _serialize(lines: list[str]) -> bytes:
    physical_lines = [part for line in lines for part in _fold_line(line)]
    return _CRLF.join(physical_lines) + _CRLF


def build_interview_ics(
    *,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime,
    dtstamp: datetime,
    last_modified: datetime | None = None,
    sequence: int = 0,
    cancelled: bool = False,
    description: str | None = None,
    location: str | None = None,
    preparation_url: str | None = None,
    reminder_before: timedelta | None = timedelta(days=1),
) -> bytes:
    """Construit un calendrier RFC 5545 UTF-8 contenant un entretien.

    ``uid`` est fourni par l'appelant afin de rester stable entre deux exports.
    Une URL de préparation locale (`file:`, chemin absolu…) est refusée pour ne
    pas divulguer l'arborescence de la machine dans un calendrier partagé.
    """
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end, "end")
    stamp_utc = _aware_utc(dtstamp, "dtstamp")
    modified_utc = _aware_utc(last_modified or dtstamp, "last_modified")
    if end_utc <= start_utc:
        raise ValueError("end doit être strictement postérieur à start")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence <= 2_147_483_647:
        raise ValueError("sequence doit être un entier compris entre 0 et 2147483647")
    if not isinstance(cancelled, bool):
        raise TypeError("cancelled doit être un booléen")

    safe_summary = _escape_text(summary)
    if not safe_summary:
        raise ValueError("summary ne doit pas être vide")

    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//JobRadar//Interview Calendar//FR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
        "BEGIN:VEVENT",
        f"UID:{_validate_identifier(uid)}",
        f"DTSTAMP:{_ical_datetime(stamp_utc)}",
        f"LAST-MODIFIED:{_ical_datetime(modified_utc)}",
        f"DTSTART:{_ical_datetime(start_utc)}",
        f"DTEND:{_ical_datetime(end_utc)}",
        f"SEQUENCE:{sequence}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        f"SUMMARY:{safe_summary}",
    ]
    if location is not None:
        lines.append(f"LOCATION:{_escape_text(location)}")
    if description is not None:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    if preparation_url is not None:
        lines.append(f"URL:{_validate_url(preparation_url)}")
    if reminder_before is not None and not cancelled:
        lines.extend(
            [
                "BEGIN:VALARM",
                f"TRIGGER:{_duration_before_event(reminder_before)}",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape_text(f'Rappel : {summary}')}",
                "END:VALARM",
            ]
        )
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return _serialize(lines)


def safe_ics_filename(name: str) -> str:
    """Retourne un nom `.ics` portable sous Windows, Linux et macOS."""
    if not isinstance(name, str):
        raise TypeError("name doit être une chaîne")
    clean = _UNSAFE_FILENAME.sub("_", name).strip(" .")
    if clean.lower().endswith(".ics"):
        clean = clean[:-4].rstrip(" .")
    if not clean:
        clean = "entretien"
    if clean.upper() in _WINDOWS_RESERVED:
        clean = f"_{clean}"
    # Réserve de la place pour l'extension et limite par octets sans couper UTF-8.
    encoded = clean.encode("utf-8")[:240]
    while True:
        try:
            clean = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return f"{clean}.ics"


def write_ics_atomic(content: bytes, directory: str | os.PathLike[str], filename: str) -> Path:
    """Écrit un ICS par remplacement atomique dans un dossier existant."""
    if not isinstance(content, bytes):
        raise TypeError("content doit être produit sous forme d'octets")
    destination_dir = Path(directory)
    if not destination_dir.is_dir():
        raise ValueError("le dossier de destination doit exister")
    destination = destination_dir / safe_ics_filename(filename)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
