from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jobradar.calendar_export import build_interview_ics, safe_ics_filename, write_ics_atomic

PARIS = ZoneInfo("Europe/Paris")
STAMP = datetime(2026, 10, 22, 10, 30, tzinfo=timezone.utc)
START = datetime(2026, 10, 26, 14, 0, tzinfo=PARIS)
END = datetime(2026, 10, 26, 15, 30, tzinfo=PARIS)


def make_ics(**overrides: object) -> bytes:
    values = {
        "uid": "interview-42@jobradar.local",
        "summary": "Entretien cybersécurité 🛡️",
        "start": START,
        "end": END,
        "dtstamp": STAMP,
    }
    values.update(overrides)
    return build_interview_ics(**values)  # type: ignore[arg-type]


def unfold(content: bytes) -> bytes:
    return content.replace(b"\r\n ", b"")


def test_exports_utc_utf8_alarm_and_escaped_text() -> None:
    content = make_ics(
        description="Ligne 1\nLigne 2, test; chemin \\ partagé",
        location="Paris, France",
        preparation_url="https://example.org/preparation?id=42",
    )
    plain = unfold(content).decode("utf-8")

    assert content.endswith(b"\r\n")
    assert b"\n" not in content.replace(b"\r\n", b"")
    assert "DTSTART:20261026T130000Z" in plain
    assert "DTEND:20261026T143000Z" in plain
    assert "DTSTAMP:20261022T103000Z" in plain
    assert "LAST-MODIFIED:20261022T103000Z" in plain
    assert "DESCRIPTION:Ligne 1\\nLigne 2\\, test\\; chemin \\\\ partagé" in plain
    assert "LOCATION:Paris\\, France" in plain
    assert "URL:https://example.org/preparation?id=42" in plain
    assert "TRIGGER:-P1D" in plain
    assert "Entretien cybersécurité 🛡️" in plain


def test_folds_every_physical_line_at_75_utf8_octets_without_corruption() -> None:
    content = make_ics(description="émoji🙂" * 50)

    assert all(len(line) <= 75 for line in content.split(b"\r\n") if line)
    assert "émoji🙂" * 50 in unfold(content).decode("utf-8")


def test_neutralizes_text_injection() -> None:
    plain = unfold(make_ics(summary="Entretien\r\nBEGIN:VALARM")).decode("utf-8")

    assert "SUMMARY:Entretien\\nBEGIN:VALARM" in plain
    # Le texte injecté reste échappé dans SUMMARY et dans la description de l'alarme :
    # seul le véritable composant alarme ouvre une ligne de contenu « BEGIN:VALARM ».
    assert plain.split("\r\n").count("BEGIN:VALARM") == 1


def test_cancelled_event_uses_cancel_method_and_has_no_alarm() -> None:
    plain = unfold(make_ics(cancelled=True, sequence=3)).decode("utf-8")

    assert "METHOD:CANCEL" in plain
    assert "STATUS:CANCELLED" in plain
    assert "SEQUENCE:3" in plain
    assert "BEGIN:VALARM" not in plain


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"start": datetime(2026, 1, 1, 10)}, "fuseau"),
        ({"end": START}, "postérieur"),
        ({"sequence": -1}, "sequence"),
        ({"sequence": True}, "sequence"),
        ({"uid": "bad\r\nUID:injected"}, "contrôle"),
        ({"summary": ""}, "summary"),
        ({"reminder_before": timedelta(0)}, "positive"),
        ({"preparation_url": "file:///home/alice/fiche.md"}, "HTTP"),
        ({"preparation_url": "/home/alice/fiche.md"}, "HTTP"),
        ({"preparation_url": "https://user:secret@example.org/fiche"}, "identifiants"),
        ({"preparation_url": "https://example.org\r\nATTENDEE:x"}, "interdit"),
    ],
)
def test_rejects_invalid_or_sensitive_values(overrides: dict[str, object], error: str) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        make_ics(**overrides)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Entretien ACME", "Entretien ACME.ics"),
        ("CON.ics", "_CON.ics"),
        ("../rapport:<final>?.ics", "_rapport__final__.ics"),  # pas de fichier caché ni de « .. »
        ("...", "entretien.ics"),
    ],
)
def test_safe_windows_filename(source: str, expected: str) -> None:
    assert safe_ics_filename(source) == expected


def test_atomic_write_replaces_target_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    first = make_ics(sequence=1)
    second = make_ics(sequence=2)

    destination = write_ics_atomic(first, tmp_path, "CON.ics")
    assert destination == tmp_path / "_CON.ics"
    assert destination.read_bytes() == first

    replaced = write_ics_atomic(second, tmp_path, "CON.ics")
    assert replaced == destination
    assert destination.read_bytes() == second
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_requires_existing_directory_and_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="doit exister"):
        write_ics_atomic(b"data", tmp_path / "missing", "event")
    with pytest.raises(TypeError, match="octets"):
        write_ics_atomic("data", tmp_path, "event")  # type: ignore[arg-type]
