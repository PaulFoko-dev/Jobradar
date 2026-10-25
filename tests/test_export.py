"""Tests de l'export CSV et des sauvegardes ZIP (fonctions pures, puis routes et commande)."""
import csv
import io
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jobradar import config, db, export


def test_csv_excel_complet_et_statuts_lisibles() -> None:
    applications = [
        {
            "title": "Stage SOC",
            "company": "Acme",
            "location": "Paris",
            "status": "envoyee",
            "score": 91,
            "created_at": "2026-10-20T08:00:00+00:00",
            "applied_at_utc": "2026-10-21T09:00:00Z",
            "follow_up": {"due_at": "2026-10-31T09:00:00Z"},
            "interviews_count": 2,
            "url": "https://example.test/offre",
            "archive_dir": "2026-10-20_Acme_Stage-SOC",
            "notes": "Premier échange positif",
        }
    ]

    content = export.applications_csv(applications)

    assert content.startswith(b"\xef\xbb\xbf")
    assert b";" in content
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        list(export.CSV_COLUMNS),
        [
            "Stage SOC",
            "Acme",
            "Paris",
            "Candidature envoyée",
            "91",
            "2026-10-20 08:00",
            "2026-10-21 09:00",
            "2026-10-31 09:00",
            "2",
            "https://example.test/offre",
            "2026-10-20_Acme_Stage-SOC",
            "Premier échange positif",
        ],
    ]


def test_csv_dates_dans_le_fuseau_metier() -> None:
    content = export.applications_csv([{"title": "x", "status": "relance", "created_at": "2026-10-20T08:00:00+00:00"}],
                                      ZoneInfo("Europe/Paris"))
    row = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))[1]
    assert row[3] == "Relancée"
    assert row[5] == "2026-10-20 10:00"


@pytest.mark.parametrize("dangerous", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", " \t=HYPERLINK(\"x\")", "\tvaleur"])
def test_csv_neutralise_les_formules(dangerous: str) -> None:
    content = export.applications_csv([{"title": dangerous, "status": "consultee", "notes": dangerous}])
    row = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))[1]
    assert row[0] == "'" + dangerous
    assert row[-1] == "'" + dangerous


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        connection.execute("INSERT INTO messages(text) VALUES ('cohérent')")


def test_sauvegarde_contient_base_config_et_archives_y_compris_retirees(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "mes-candidatures"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"timezone": "Europe/Paris"}', encoding="utf-8")
    _database(data_dir / "jobradar.db")
    (archive_dir / "active" / "offre").mkdir(parents=True)
    (archive_dir / "active" / "offre" / "offre.md").write_text("Offre active", encoding="utf-8")
    (archive_dir / "_retirees" / "ancienne").mkdir(parents=True)
    (archive_dir / "_retirees" / "ancienne" / "candidature.json").write_text("{}", encoding="utf-8")
    instant = datetime(2026, 10, 23, 12, 34, 56, tzinfo=timezone.utc)

    result = export.create_backup(data_dir=data_dir, archive_dir=archive_dir, at=instant)
    backup = data_dir / "sauvegardes" / str(result["path"])

    assert result["path"] == "JobRadar-sauvegarde-20261023-123456.zip"
    assert result["size_bytes"] == backup.stat().st_size
    assert result["created_at"] == "2026-10-23T12:34:56Z"
    with zipfile.ZipFile(backup) as zipped:
        assert {"jobradar.db", "config.json", "candidatures/active/offre/offre.md",
                "candidatures/_retirees/ancienne/candidature.json"} <= set(zipped.namelist())
        extracted = tmp_path / "restored.db"
        extracted.write_bytes(zipped.read("jobradar.db"))
    with sqlite3.connect(extracted) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "cohérent"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert not any(p.name.startswith(".") for p in (data_dir / "sauvegardes").iterdir())  # aucun résidu


def test_sauvegarde_coherente_avec_transaction_wal_non_validee(tmp_path: Path) -> None:
    """Une écriture en cours (WAL, non validée) n'apparaît pas ; les données validées du WAL, si."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    writer = sqlite3.connect(data_dir / "jobradar.db", isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("INSERT INTO t VALUES ('validée')")
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES ('en cours')")
    try:
        result = export.create_backup(data_dir=data_dir, archive_dir=tmp_path / "absent")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    with zipfile.ZipFile(data_dir / "sauvegardes" / str(result["path"])) as zipped:
        assert "candidatures/" in zipped.namelist()
        (tmp_path / "copie.db").write_bytes(zipped.read("jobradar.db"))
    with sqlite3.connect(tmp_path / "copie.db") as connection:
        assert connection.execute("SELECT v FROM t").fetchall() == [("validée",)]


def test_sauvegarde_exclut_secrets_et_dossiers_techniques_meme_dans_archive(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    _database(data_dir / "jobradar.db")
    for name in ("navigateur", "claude_cwd"):
        (data_dir / name).mkdir()
        (data_dir / name / "secret.txt").write_text("secret", encoding="utf-8")
    (data_dir / "secrets.json").write_text("secret", encoding="utf-8")
    archive_dir = data_dir

    result = export.create_backup(
        data_dir=data_dir,
        archive_dir=archive_dir,
        at=datetime(2026, 10, 23, tzinfo=timezone.utc),
    )

    with zipfile.ZipFile(data_dir / "sauvegardes" / str(result["path"])) as zipped:
        names = set(zipped.namelist())
    assert not any("secrets.json" in name for name in names)
    assert not any("navigateur" in name for name in names)
    assert not any("sauvegardes" in name for name in names)
    assert not any("claude_cwd" in name for name in names)
    assert "candidatures/jobradar.db" not in names
    assert "candidatures/config.json" not in names


def test_sauvegarde_ignore_les_fichiers_temporaires_en_cours(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "archives"
    (archive_dir / "dossier").mkdir(parents=True)
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    _database(data_dir / "jobradar.db")
    (archive_dir / "dossier" / ".LISEZMOI.md.abc123.tmp").write_text("partiel", encoding="utf-8")
    (archive_dir / "dossier" / "LISEZMOI.md").write_text("complet", encoding="utf-8")
    (archive_dir / "dossier" / ".capture-x1.tmp").mkdir()
    (archive_dir / "dossier" / ".capture-x1.tmp" / "offre.pdf").write_bytes(b"%PDF partiel")
    result = export.create_backup(data_dir=data_dir, archive_dir=archive_dir)
    with zipfile.ZipFile(data_dir / "sauvegardes" / str(result["path"])) as zipped:
        assert [n for n in zipped.namelist() if n.startswith("candidatures/dossier/")] == [
            "candidatures/dossier/", "candidatures/dossier/LISEZMOI.md"]


def test_sauvegarde_ignore_les_liens_symboliques(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "archives"
    data_dir.mkdir()
    archive_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    _database(data_dir / "jobradar.db")
    outside = tmp_path / "prive.txt"
    outside.write_text("ne pas sauvegarder", encoding="utf-8")
    try:
        (archive_dir / "fuite.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Liens symboliques indisponibles sur cette plateforme")

    result = export.create_backup(data_dir=data_dir, archive_dir=archive_dir)

    with zipfile.ZipFile(data_dir / "sauvegardes" / str(result["path"])) as zipped:
        assert "candidatures/fuite.txt" not in zipped.namelist()


def test_collision_de_nom_liste_et_resolution_stricte(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    archive_dir = tmp_path / "archives"
    data_dir.mkdir()
    archive_dir.mkdir()
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    _database(data_dir / "jobradar.db")
    instant = datetime(2026, 10, 23, 12, 34, 56, tzinfo=timezone.utc)

    first = export.create_backup(data_dir=data_dir, archive_dir=archive_dir, at=instant)
    second = export.create_backup(data_dir=data_dir, archive_dir=archive_dir, at=instant)

    assert first["path"] == "JobRadar-sauvegarde-20261023-123456.zip"
    assert second["path"] == "JobRadar-sauvegarde-20261023-123456-2.zip"
    assert [item["path"] for item in export.list_backups(data_dir=data_dir)] == [second["path"], first["path"]]
    assert export.resolve_backup(data_dir=data_dir, name=str(first["path"])).name == first["path"]
    for invalid in ("../config.json", "/tmp/x.zip", "config.json", "JobRadar-sauvegarde-20261023.zip"):
        with pytest.raises((FileNotFoundError, ValueError)):
            export.resolve_backup(data_dir=data_dir, name=invalid)


def test_liste_ignore_faux_zip_et_lien_symbolique(tmp_path: Path) -> None:
    directory = tmp_path / "sauvegardes"
    directory.mkdir()
    fake = directory / "JobRadar-sauvegarde-20261023-123456.zip"
    fake.write_text("pas un zip", encoding="utf-8")
    target = tmp_path / "valid.zip"
    with zipfile.ZipFile(target, "w") as zipped:
        zipped.writestr("ok.txt", "ok")
    try:
        (directory / "JobRadar-sauvegarde-20261023-123457.zip").symlink_to(target)
    except OSError:
        pytest.skip("Liens symboliques indisponibles sur cette plateforme")

    assert export.list_backups(data_dir=tmp_path) == []


def test_sauvegarde_refuse_base_config_et_archive_symboliques(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_db = tmp_path / "real.db"
    real_config = tmp_path / "real.json"
    real_archive = tmp_path / "real-archive"
    _database(real_db)
    real_config.write_text("{}", encoding="utf-8")
    real_archive.mkdir()
    try:
        (data_dir / "jobradar.db").symlink_to(real_db)
        (data_dir / "config.json").symlink_to(real_config)
        linked_archive = tmp_path / "linked-archive"
        linked_archive.symlink_to(real_archive, target_is_directory=True)
    except OSError:
        pytest.skip("Liens symboliques indisponibles sur cette plateforme")

    with pytest.raises(FileNotFoundError):
        export.create_backup(data_dir=data_dir, archive_dir=real_archive)
    with pytest.raises(ValueError):
        export.create_backup(
            data_dir=data_dir,
            archive_dir=linked_archive,
            database_path=real_db,
            config_path=real_config,
        )


# ---------------------------------------------------------------- routes et commande


def test_api_export_csv(client) -> None:
    client.post("/api/applications", json={"title": "=Stage SOC", "company": "Acmé", "notes": "@note"})
    response = client.get("/api/export/applications.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="JobRadar-candidatures-20261020.csv"'
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[1][:6] == ["'=Stage SOC", "Acmé", "", "Offre consultée", "", "2026-10-20 10:00"]
    assert rows[1][10].startswith("2026-10-20_Acme_Stage-SOC")
    assert rows[1][11] == "'@note"


def test_api_sauvegarde_liste_et_telechargement(client, data_dir: Path) -> None:
    app = client.post("/api/applications", json={"title": "Stage SOC", "company": "Acme"}).json()
    (data_dir / "secrets.json").write_text("secret", encoding="utf-8")
    (data_dir / "navigateur").mkdir(exist_ok=True)
    (data_dir / "navigateur" / "Cookies").write_text("session", encoding="utf-8")

    created = client.post("/api/backup").json()
    assert created["path"] == "JobRadar-sauvegarde-20261020-100000.zip"  # heure de Paris
    assert created["created_at"] == "2026-10-20T08:00:00Z"
    assert client.get("/api/backups").json()[0]["path"] == created["path"]

    download = client.get(f"/api/backups/{created['path']}")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as zipped:
        names = set(zipped.namelist())
        (data_dir.parent / "restauree.db").write_bytes(zipped.read("jobradar.db"))
    assert {"jobradar.db", "config.json", "candidatures/INDEX.md",
            f"candidatures/{app['archive_dir']}/candidature.json"} <= names
    assert not any("secrets" in n or "navigateur" in n or "claude_cwd" in n or "sauvegardes" in n for n in names)
    with sqlite3.connect(data_dir.parent / "restauree.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT title FROM applications").fetchall() == [("Stage SOC",)]


@pytest.mark.parametrize("name", ["config.json", "..%2Fconfig.json", "JobRadar-sauvegarde-20261020-100000.zip"])
def test_api_telechargement_refuse(client, name: str) -> None:
    response = client.get(f"/api/backups/{name}")
    assert response.status_code == 404
    assert response.json()["detail"] in ("Sauvegarde introuvable.", "Not Found")


def test_commande_backup(data_dir: Path, tmp_path: Path) -> None:
    db.create_application({"title": "Stage SOC"})
    env = {"JOBRADAR_DATA_DIR": str(data_dir), "JOBRADAR_ARCHIVE_DIR": str(tmp_path / "candidatures"),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-m", "jobradar.cli", "backup"], capture_output=True, text=True,
                         env=env, cwd=config.ROOT, check=True).stdout
    assert out.startswith("Sauvegarde créée : ")
    [backup] = (data_dir / "sauvegardes").glob("JobRadar-sauvegarde-*.zip")
    assert str(backup) in out
