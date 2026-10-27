"""Archives des candidatures : dossiers, fichiers, file de capture, chemins, retrait, réindexation."""
import asyncio
import base64
import json
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FIXED_NOW, fake_capture_module

from jobradar import archive, cli, config, db
from jobradar.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, migrate

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"x" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode()


def _root() -> Path:
    return config.archive_dir()


def _idle() -> None:
    assert archive._QUEUE is not None and archive._QUEUE.wait_idle()


def _create(client, **fields: object) -> dict:
    app = client.post("/api/applications", json={"title": "Stage Analyste CERT", "company": "Intrinsec"} | fields)
    assert app.status_code == 200, app.text
    _idle()
    return db.get_application(app.json()["id"])


def _use_capture(monkeypatch, module) -> None:
    monkeypatch.setitem(sys.modules, "jobradar.capture", module)


# ---------------------------------------------------------------- emplacement


def test_emplacement_des_archives(tmp_path) -> None:
    assert config.archive_dir({}, {"JOBRADAR_ARCHIVE_DIR": str(tmp_path)}) == tmp_path
    assert config.archive_dir({"archive_dir": ""}, {}) == config.ROOT / "candidatures"
    assert config.archive_dir({"archive_dir": "/srv/cv"}, {}) == Path("/srv/cv")
    assert config.archive_dir({"archive_dir": "mes-candidatures"}, {}) == config.ROOT / "mes-candidatures"
    with pytest.raises(RuntimeError, match="chemin absolu"):
        config.archive_dir({}, {"JOBRADAR_ARCHIVE_DIR": "relatif"})


def test_les_tests_necrivent_jamais_dans_les_vraies_archives(data_dir) -> None:
    assert os.environ["JOBRADAR_ARCHIVE_DIR"] == str(data_dir / "candidatures")
    assert config.archive_dir() != config.ROOT / "candidatures"


# ---------------------------------------------------------------- noms de dossiers


def test_nom_translittere_et_sans_caracteres_interdits() -> None:
    name = archive.folder_name("2026-10-23", "Société Générale & Cie", 'Stage « Analyste » <SOC>: N°1 / Œuvre?*',
                               set())
    assert name == "2026-10-23_Societe-Generale-Cie_Stage-Analyste-SOC-N1-OEuvre"
    assert name.isascii() and not set('<>:"/\\|?*') & set(name)


def test_nom_vide_et_noms_reserves_windows() -> None:
    assert archive.folder_name("2026-10-23", None, "!!!", set()) == "2026-10-23_Entreprise_Poste"
    assert archive.portable_name("CON") == "_CON"
    assert archive.portable_name("nul.txt") == "_nul.txt"
    assert archive.portable_name("LPT9") == "_LPT9"
    assert archive.portable_name("dossier. ") == "dossier"
    assert archive.portable_name("CONSOLE") == "CONSOLE"


def test_nom_limite_a_80_et_collisions_sans_tenir_compte_de_la_casse() -> None:
    long_title = "Ingénieur " * 30
    first = archive.folder_name("2026-10-23", "Une entreprise au nom vraiment très long", long_title, set())
    assert len(first) <= 80 and not first.endswith("-")
    assert first.startswith("2026-10-23_Une-entreprise-au-nom-vraiment_Ingenieur-")
    taken = {first.casefold(), (first[:78] + "-2").casefold()}
    third = archive.folder_name("2026-10-23", "Une entreprise au nom vraiment très long", long_title, taken)
    assert third.endswith("-3") and len(third) <= 80
    assert archive.folder_name("2026-10-23", "acme", "soc", {"2026-10-23_acme_soc"}) == "2026-10-23_acme_soc-2"


def test_date_du_dossier_dans_le_fuseau_metier(data_dir, clock) -> None:
    from datetime import datetime, timezone
    clock(datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc))  # 23 octobre 01 h 30 à Paris
    app = db.create_application({"title": "Stage", "company": "Acme"})
    assert archive.ensure_archive(app["id"])
    assert db.get_application(app["id"])["archive_dir"] == "2026-10-23_Acme_Stage"


# ---------------------------------------------------------------- création et contenu


def test_creation_depuis_une_offre_arborescence_et_contenu(client, make_offer, data_dir) -> None:
    oid = make_offer(title="Stage Analyste CERT", company="Intrinsec", location="Paris", contract="Stage",
                     description="Description complète\nsur plusieurs lignes.",
                     links=[{"source": "wttj", "url": "https://example.test/offre/1"},
                            {"source": "ats", "url": "https://ats.test/postuler", "apply": True}])
    db.save_analysis(oid, {"score": 87, "in_zone": True, "verdict": "Très pertinent", "summary": "Stage au CERT.",
                           "missions": ["Analyse d'incidents"], "profile": ["Bac+5"], "cv_keywords": ["SIEM"],
                           "red_flags": ["Anglais courant"]})
    app = _create(client, offer_id=oid)
    directory = _root() / app["archive_dir"]

    assert app["archive_dir"] == "2026-10-20_Intrinsec_Stage-Analyste-CERT"
    assert app["capture_status"] == "done" and app["capture_level"] == "reconstituee"
    assert app["offer_online"] is None
    assert sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")) == [
        "LISEZMOI.md", "candidature.json", "documents", "documents/README.txt", "entretien", "envoi", "offre",
        "offre/offre.json", "offre/offre.md", "offre/offre.pdf"]
    assert (_root() / "_retirees").is_dir()

    offer_md = (directory / "offre" / "offre.md").read_text(encoding="utf-8")
    for expected in ("# Stage Analyste CERT", "**Entreprise** : Intrinsec", "**Contrat** : Stage",
                     "Description complète\nsur plusieurs lignes.", "**Score** : 87", "**Verdict** : Très pertinent",
                     "- Analyse d'incidents", "### Mots-clés pour le CV\n\n- SIEM", "- Anglais courant",
                     "- Candidater : https://ats.test/postuler", "**Entrée dans le suivi** : 2026-10-20 10:00"):
        assert expected in offer_md
    readme = (directory / "LISEZMOI.md").read_text(encoding="utf-8")
    for expected in ("# Stage Analyste CERT — Intrinsec", "**Statut** : Offre consultée",
                     "**Lien de candidature** : https://ats.test/postuler", "copie reconstituée",
                     "- 2026-10-20 10:00 — Offre consultée", "**Annonce** : non vérifiée"):
        assert expected in readme
    index = (_root() / "INDEX.md").read_text(encoding="utf-8")
    assert ("| 2026-10-20 | Intrinsec | Stage Analyste CERT | Offre consultée | "
            "[Ouvrir](2026-10-20_Intrinsec_Stage-Analyste-CERT/LISEZMOI.md) |") in index
    for markdown in (offer_md, readme, index):
        assert str(data_dir) not in markdown and str(Path.home()) not in markdown

    document = json.loads((directory / "candidature.json").read_text(encoding="utf-8"))
    assert document["archive_id"] == app["archive_id"]
    assert document["application"]["title"] == "Stage Analyste CERT"
    assert document["offer_snapshot"]["analysis"]["score"] == 87


def test_instantane_fige_malgre_une_nouvelle_analyse(client, make_offer) -> None:
    oid = make_offer(description="Version d'origine")
    db.save_analysis(oid, {"score": 70, "summary": "Avant"})
    app = _create(client, offer_id=oid)
    with db.connect() as c:
        c.execute("UPDATE offers SET description='Version modifiée' WHERE id=?", (oid,))
    db.save_analysis(oid, {"score": 20, "summary": "Après"})
    client.patch(f"/api/applications/{app['id']}", json={"notes": "Relu"})

    snapshot = client.get(f"/api/applications/{app['id']}/offer-snapshot").json()
    assert snapshot["description"] == "Version d'origine"
    assert snapshot["analysis"]["summary"] == "Avant"
    on_disk = json.loads((_root() / app["archive_dir"] / "offre" / "offre.json").read_text(encoding="utf-8"))
    assert on_disk == snapshot
    assert "Avant" in (_root() / app["archive_dir"] / "offre" / "offre.md").read_text(encoding="utf-8")


def test_ajout_manuel_et_offre_deja_suivie(client, make_offer) -> None:
    app = _create(client, title="Alternance SOC", company=None, url="https://example.test/manuel")
    assert app["archive_dir"] == "2026-10-20_Entreprise_Alternance-SOC"
    snapshot = client.get(f"/api/applications/{app['id']}/offer-snapshot").json()
    assert snapshot["url"] == "https://example.test/manuel" and snapshot["analysis"] is None

    oid = make_offer()
    first = _create(client, offer_id=oid)
    again = _create(client, offer_id=oid)
    assert again["archive_dir"] == first["archive_dir"]
    assert len([p for p in _root().iterdir() if p.is_dir() and p.name != "_retirees"]) == 2


def test_mises_a_jour_regenerent_les_fichiers_et_les_ics(client) -> None:
    app = _create(client)
    directory = _root() / app["archive_dir"]
    client.patch(f"/api/applications/{app['id']}", json={"status": "envoyee", "notes": "Envoyée par le site"})
    readme = (directory / "LISEZMOI.md").read_text(encoding="utf-8")
    assert "**Statut** : Candidature envoyée" in readme and "Envoyée par le site" in readme
    assert "| Candidature envoyée |" in (_root() / "INDEX.md").read_text(encoding="utf-8")

    interview = client.post(f"/api/applications/{app['id']}/interviews",
                            json={"starts_at": "2026-10-27T14:00:00+01:00"}).json()
    assert [p.name for p in (directory / "entretien").iterdir()] == [f"2026-10-27_entretien-{interview['id']}.ics"]
    assert "2026-10-27 14:00 — Entretien — Intrinsec" in (directory / "LISEZMOI.md").read_text(encoding="utf-8")

    client.patch(f"/api/interviews/{interview['id']}", json={"starts_at": "2026-10-29T09:00:00+01:00"})
    files = list((directory / "entretien").iterdir())
    assert [p.name for p in files] == [f"2026-10-29_entretien-{interview['id']}.ics"]
    client.delete(f"/api/interviews/{interview['id']}")
    assert b"STATUS:CANCELLED" in files[0].read_bytes()
    document = json.loads((directory / "candidature.json").read_text(encoding="utf-8"))
    assert document["interviews"][0]["status"] == "cancelled"


def test_un_echec_decriture_ne_fait_pas_echouer_la_requete(client, data_dir, monkeypatch) -> None:
    blocked = data_dir / "fichier"
    blocked.write_text("pas un dossier", encoding="utf-8")
    monkeypatch.setenv("JOBRADAR_ARCHIVE_DIR", str(blocked))
    response = client.post("/api/applications", json={"title": "Stage"})
    assert response.status_code == 200
    assert response.json()["archive_dir"] is None
    assert client.patch(f"/api/applications/{response.json()['id']}", json={"notes": "ok"}).status_code == 200


# ---------------------------------------------------------------- file de capture


def test_capture_reussie_page_dorigine(client, monkeypatch, data_dir) -> None:
    fake = fake_capture_module(level="page")
    _use_capture(monkeypatch, fake)
    app = _create(client, url="https://example.test/offre")
    assert app["capture_status"] == "done" and app["capture_level"] == "page"
    call = fake.calls[0]
    assert call["url"] == "https://example.test/offre"
    # capture faite à part, dans un dossier temporaire du dossier offre/, supprimé ensuite
    assert call["dest_dir"].parent == (_root() / app["archive_dir"] / "offre").resolve()
    assert call["dest_dir"].name.startswith(".capture-") and not call["dest_dir"].exists()
    assert call["browser_profile_dir"] == (data_dir / "navigateur-captures").resolve()
    assert call["offer"]["title"] == "Stage Analyste CERT"
    assert "copie de la page d'origine" in (_root() / app["archive_dir"] / "LISEZMOI.md").read_text(encoding="utf-8")


def test_capture_en_echec(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="indisponible", error="Page inaccessible."))
    app = _create(client)
    assert (app["capture_status"], app["capture_level"]) == ("failed", "indisponible")
    view = client.get(f"/api/applications/{app['id']}/archive").json()
    assert view["capture"] == {"status": "failed", "level": "indisponible", "error": "Page inaccessible.",
                               "captured_at": "2026-10-20T08:00:05Z"}
    assert "Page inaccessible." in (_root() / app["archive_dir"] / "LISEZMOI.md").read_text(encoding="utf-8")


def test_capture_qui_leve_une_exception(client, monkeypatch) -> None:
    fake = fake_capture_module()

    def boom(**_kwargs):
        raise RuntimeError("navigateur planté")

    fake.capture_offer = boom
    _use_capture(monkeypatch, fake)
    app = _create(client)
    assert app["capture_status"] == "failed"
    assert db.archive_row(app["id"])["capture_error"] == "Échec inattendu de la capture : navigateur planté"


def test_module_de_capture_absent(client, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "jobradar.capture", None)  # import impossible
    app = _create(client)
    row = db.archive_row(app["id"])
    assert row["capture_status"] == "failed"
    assert row["capture_error"].startswith("Module de capture indisponible")
    assert (_root() / app["archive_dir"] / "offre" / "offre.md").is_file()  # le texte reste archivé
    assert client.get("/api/archives/info").json()["browser"]["available"] is False


def test_captures_relancees_au_redemarrage(data_dir, fake_capture) -> None:
    first = db.create_application({"title": "Stage A"})
    second = db.create_application({"title": "Stage B"})
    archive.ensure_archive(first["id"])
    archive.ensure_archive(second["id"])
    db.set_archive_fields(second["id"], {"capture_status": "running"})  # arrêt brutal pendant la capture

    queue = archive.start_queue()
    assert queue.wait_idle()
    assert [db.archive_row(i)["capture_status"] for i in (first["id"], second["id"])] == ["done", "done"]
    assert len(fake_capture.calls) == 2


def test_recapture_et_conflit_409(client, monkeypatch) -> None:
    app = _create(client)
    db.set_archive_fields(app["id"], {"capture_status": "running"})
    response = client.post(f"/api/applications/{app['id']}/archive/recapture")
    assert response.status_code == 409
    assert response.json()["detail"] == "Une capture de cette offre est déjà en attente ou en cours."

    db.set_archive_fields(app["id"], {"capture_status": "failed"})
    fake = fake_capture_module(level="page")
    _use_capture(monkeypatch, fake)
    response = client.post(f"/api/applications/{app['id']}/archive/recapture")
    assert response.status_code == 200
    assert response.json()["capture"]["status"] in ("pending", "running", "done")
    _idle()
    assert db.archive_row(app["id"])["capture_level"] == "page" and len(fake.calls) == 1


def _recapture(client, monkeypatch, app_id: int, module) -> dict:
    _use_capture(monkeypatch, module)
    assert client.post(f"/api/applications/{app_id}/archive/recapture").status_code == 200
    _idle()
    return db.archive_row(app_id)


def test_fenetre_visible_reservee_aux_recaptures_demandees(client, monkeypatch) -> None:
    fake = fake_capture_module(level="page")
    _use_capture(monkeypatch, fake)
    app = _create(client)
    _idle()
    assert client.post(f"/api/applications/{app['id']}/archive/recapture").status_code == 200
    _idle()
    # Capture automatique à la création : jamais de fenêtre ; recapture demandée : fenêtre permise.
    assert [call["allow_headed"] for call in fake.calls] == [False, True]


def test_recapture_sans_image_retire_l_ancienne_image(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="page"))
    app = _create(client)
    _idle()
    png = _root() / app["archive_dir"] / "offre" / "offre.png"
    png.write_bytes(b"\x89PNG ancienne image")
    # Le faux module produit un PDF sans PNG : cas d'une page trop haute pour une image complète.
    _recapture(client, monkeypatch, app["id"], fake_capture_module(level="page"))
    assert not png.exists()
    assert (png.parent / "offre.pdf").read_bytes() == b"%PDF-1.4 faux"


def test_recapture_moins_fidele_ignoree(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="page"))
    app = _create(client)
    pdf = _root() / app["archive_dir"] / "offre" / "offre.pdf"
    pdf.write_bytes(b"%PDF-1.4 page d'origine")

    row = _recapture(client, monkeypatch, app["id"],
                     fake_capture_module(level="reconstituee", error="Page devenue inaccessible."))
    assert pdf.read_bytes() == b"%PDF-1.4 page d'origine"
    assert (row["capture_status"], row["capture_level"]) == ("done", "page")
    assert row["capture_error"] == ("Recapture moins fidèle (reconstituee) ignorée : la copie précédente (page) "
                                    "est conservée. Détail : Page devenue inaccessible.")
    assert "Recapture moins fidèle" in (_root() / app["archive_dir"] / "LISEZMOI.md").read_text(encoding="utf-8")

    monkeypatch.setitem(sys.modules, "jobradar.capture", None)  # module absent : la copie reste aussi
    assert client.post(f"/api/applications/{app['id']}/archive/recapture").status_code == 200
    _idle()
    assert db.archive_row(app["id"])["capture_level"] == "page" and pdf.read_bytes().endswith(b"d'origine")
    assert not [p for p in pdf.parent.iterdir() if p.name.startswith(".")]


def test_recapture_aussi_fidele_remplace(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="reconstituee"))
    app = _create(client)
    pdf = _root() / app["archive_dir"] / "offre" / "offre.pdf"
    pdf.write_bytes(b"%PDF-1.4 ancienne")
    row = _recapture(client, monkeypatch, app["id"], fake_capture_module(level="page"))
    assert (row["capture_level"], row["capture_error"]) == ("page", None)
    assert pdf.read_bytes() == b"%PDF-1.4 faux"


def test_copie_montrant_un_mur_remplacee_meme_par_une_copie_reconstituee(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="page"))
    app = _create(client)
    pdf = _root() / app["archive_dir"] / "offre" / "offre.pdf"
    pdf.write_bytes(b"%PDF-1.4 mur de connexion")
    row = _recapture(client, monkeypatch, app["id"], fake_capture_module(level="reconstituee", error="Mur."))
    assert (row["capture_level"], row["capture_error"]) == ("reconstituee", "Mur.")
    assert pdf.read_bytes() == b"%PDF-1.4 faux"


def test_copies_montrant_un_mur_relancees_au_demarrage(client, monkeypatch) -> None:
    _use_capture(monkeypatch, fake_capture_module(level="page"))
    walled, sound = _create(client), _create(client, title="Stage SOC")
    (_root() / walled["archive_dir"] / "offre" / "offre.pdf").write_bytes(b"%PDF-1.4 mur de connexion")
    archive.stop_queue()

    assert archive.requeue_walls() == [walled["id"]]
    assert db.archive_row(walled["id"])["capture_status"] == "pending"
    assert db.archive_row(sound["id"])["capture_status"] == "done"
    assert archive.run_pending_captures() == 1
    assert (_root() / walled["archive_dir"] / "offre" / "offre.pdf").read_bytes() == b"%PDF-1.4 faux"
    assert archive.requeue_walls() == []


def test_capture_ignoree_si_la_candidature_est_retiree_entre_temps(data_dir, fake_capture) -> None:
    app = db.create_application({"title": "Stage"})
    archive.ensure_archive(app["id"])
    db.delete_application(app["id"])
    archive.run_capture(app["id"])
    assert fake_capture.calls == []


# ---------------------------------------------------------------- fichiers servis


def test_vue_archive_et_fichier_servi_en_ligne(client) -> None:
    app = _create(client)
    view = client.get(f"/api/applications/{app['id']}/archive").json()
    assert view["dir"] == app["archive_dir"]
    assert [f["path"] for f in view["files"]] == ["LISEZMOI.md", "candidature.json", "documents/README.txt",
                                                  "offre/offre.json", "offre/offre.md", "offre/offre.pdf"]
    pdf = next(f for f in view["files"] if f["kind"] == "pdf")
    assert pdf["size"] == len(b"%PDF-1.4 faux") and pdf["modified_at"].endswith("Z")
    assert view["browser"] == {"available": False, "engine": None, "detail": "Navigateur simulé."}

    response = client.get(f"/api/applications/{app['id']}/archive/file", params={"path": "offre/offre.pdf"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"
    md = client.get(f"/api/applications/{app['id']}/archive/file", params={"path": "offre/offre.md"})
    assert md.headers["content-type"] == "text/markdown; charset=utf-8"
    assert md.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("path", ["../INDEX.md", "offre/../../INDEX.md", "/etc/passwd", "C:/Windows/win.ini",
                                  "offre\\offre.md", "./LISEZMOI.md", ""])
def test_traversee_de_chemin_refusee(client, path: str) -> None:
    app = _create(client)
    response = client.get(f"/api/applications/{app['id']}/archive/file", params={"path": path})
    assert response.status_code == 400


def test_fichier_non_liste_refuse(client) -> None:
    app = _create(client)
    directory = _root() / app["archive_dir"]
    (directory / "offre" / "page.html").write_text("<script>", encoding="utf-8")
    (directory / ".cache.md").write_text("caché", encoding="utf-8")
    for path in ("offre/page.html", ".cache.md", "offre/absent.pdf"):
        response = client.get(f"/api/applications/{app['id']}/archive/file", params={"path": path})
        assert response.status_code == 404, path


def test_liens_symboliques_refuses(client, tmp_path) -> None:
    app = _create(client)
    directory = _root() / app["archive_dir"]
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    outside = tmp_path / "dehors"
    outside.mkdir()
    (outside / "x.pdf").write_bytes(PDF)
    try:
        (directory / "offre" / "lien.pdf").symlink_to(secret)
        (directory / "envoi" / "dossier").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Liens symboliques indisponibles")
    paths = [f["path"] for f in client.get(f"/api/applications/{app['id']}/archive").json()["files"]]
    assert "offre/lien.pdf" not in paths and "envoi/dossier/x.pdf" not in paths
    for path in ("offre/lien.pdf", "envoi/dossier/x.pdf"):
        assert client.get(f"/api/applications/{app['id']}/archive/file", params={"path": path}).status_code == 400


def test_dossier_de_candidature_symbolique_refuse(client, tmp_path) -> None:
    app = _create(client)
    directory = _root() / app["archive_dir"]
    os.replace(directory, tmp_path / "ailleurs")
    try:
        directory.symlink_to(tmp_path / "ailleurs", target_is_directory=True)
    except OSError:
        pytest.skip("Liens symboliques indisponibles")
    assert client.get(f"/api/applications/{app['id']}/archive").status_code == 400


def test_archive_absente_404(client, data_dir) -> None:
    app = db.create_application({"title": "Sans archive"})
    for url in (f"/api/applications/{app['id']}/archive", f"/api/applications/{app['id']}/offer-snapshot",
                "/api/applications/999/archive"):
        assert client.get(url).status_code == 404
    assert client.post(f"/api/applications/{app['id']}/archive/recapture").status_code == 404


# ---------------------------------------------------------------- pièces jointes


@pytest.mark.parametrize(("filename", "content", "extension"),
                         [("capture.PNG", PNG, "png"), ("page.pdf", PDF, "pdf"), ("photo.jpeg", JPEG, "jpg")])
def test_piece_jointe_acceptee(client, filename: str, content: bytes, extension: str) -> None:
    app = _create(client)
    response = client.post(f"/api/applications/{app['id']}/archive/attach",
                           json={"filename": filename, "content_base64": _b64(content)})
    assert response.status_code == 200
    assert response.json() == {"path": f"offre/capture-utilisateur-20261020-100000.{extension}",
                               "size": len(content), "kind": extension}
    assert (_root() / app["archive_dir"] / response.json()["path"]).read_bytes() == content
    second = client.post(f"/api/applications/{app['id']}/archive/attach",
                         json={"filename": filename, "content_base64": _b64(content)}).json()
    assert second["path"] == f"offre/capture-utilisateur-20261020-100000-2.{extension}"


@pytest.mark.parametrize(("filename", "content"), [
    ("page.png", PDF),                          # signature et extension divergentes
    ("page.html", b"<html><script>"),           # type refusé
    ("image.svg", b"<svg onload=alert(1)>"),
    ("vide.pdf", b""),
])
def test_piece_jointe_refusee(client, filename: str, content: bytes) -> None:
    app = _create(client)
    response = client.post(f"/api/applications/{app['id']}/archive/attach",
                           json={"filename": filename, "content_base64": _b64(content)})
    assert response.status_code == 400
    assert not list((_root() / app["archive_dir"] / "offre").glob("capture-utilisateur-*"))


def test_piece_jointe_base64_invalide_et_trop_grande(client, monkeypatch) -> None:
    app = _create(client)
    url = f"/api/applications/{app['id']}/archive/attach"
    bad = client.post(url, json={"filename": "a.png", "content_base64": "pas du base64 !"})
    assert bad.status_code == 400 and bad.json()["detail"] == "Contenu base64 invalide."
    monkeypatch.setattr(archive, "MAX_ATTACHMENT_BYTES", 30)
    big = client.post(url, json={"filename": "a.png", "content_base64": _b64(PNG)})
    assert big.status_code == 400 and big.json()["detail"] == "Le fichier dépasse 20 Mo."


# ---------------------------------------------------------------- retrait, réindexation, rattrapage


def test_retrait_deplace_dans_retirees_sans_rien_supprimer(client) -> None:
    first = _create(client)
    second = _create(client)
    assert second["archive_dir"] == first["archive_dir"] + "-2"
    assert client.delete(f"/api/applications/{first['id']}").json() == {"ok": True}
    retired = _root() / "_retirees" / first["archive_dir"]
    assert (retired / "LISEZMOI.md").is_file() and (retired / "offre" / "offre.pdf").is_file()
    assert not (_root() / first["archive_dir"]).exists()
    assert first["archive_dir"] + "/" not in (_root() / "INDEX.md").read_text(encoding="utf-8")

    # même nom déjà présent dans _retirees : suffixe, aucun écrasement
    (_root() / first["archive_dir"]).mkdir()
    assert archive.retire(first["archive_dir"]) == f"_retirees/{first['archive_dir']}-2"
    assert (retired / "LISEZMOI.md").is_file()


def test_reindexation_sans_doublon(client) -> None:
    kept = _create(client, title="Stage conservé")
    lost = _create(client, title="Stage perdu", url="https://example.test/perdu")
    client.patch(f"/api/applications/{lost['id']}", json={"status": "envoyee", "notes": "Note à retrouver"})
    client.post(f"/api/applications/{lost['id']}/interviews", json={"starts_at": "2026-10-27T14:00:00+01:00"})
    retired = _create(client, title="Stage retiré")
    client.delete(f"/api/applications/{retired['id']}")
    with db.connect() as c:  # base perdue pour cette candidature, dossier intact
        c.execute("DELETE FROM applications WHERE id=?", (lost["id"],))
    (_root() / "dossier-etranger").mkdir()

    assert client.post("/api/archives/reindex").json() == {"created": 1, "skipped": 2}
    assert client.post("/api/archives/reindex").json() == {"created": 0, "skipped": 3}

    titles = sorted(a["title"] for a in client.get("/api/applications").json())
    assert titles == ["Stage conservé", "Stage perdu"]
    restored = next(a for a in db.list_applications() if a["title"] == "Stage perdu")
    assert restored["archive_id"] == lost["archive_id"]
    assert (restored["status"], restored["notes"], restored["applied_at"]) == ("envoyee", "Note à retrouver",
                                                                               "2026-10-20T08:00:00Z")
    assert restored["archive_dir"] == lost["archive_dir"] and restored["interviews_count"] == 1
    assert restored["created_at"] == lost["created_at"]
    assert client.get(f"/api/applications/{restored['id']}/offer-snapshot").json()["url"] == \
        "https://example.test/perdu"
    document = json.loads((_root() / lost["archive_dir"] / "candidature.json").read_text(encoding="utf-8"))
    assert document["archive_id"] == lost["archive_id"]
    assert kept["archive_id"] != lost["archive_id"]


def test_rattrapage_au_demarrage_du_serveur(data_dir, fake_capture) -> None:
    from jobradar import server

    old = db.create_application({"title": "Stage ancien", "company": "Acme"})  # avant l'étape 2 : sans archive

    async def start_and_read():
        async with server.lifespan(server.app):
            _idle()
            return db.list_applications()[0]

    app = asyncio.run(start_and_read())
    assert app["id"] == old["id"]
    assert app["archive_dir"] == "2026-10-20_Acme_Stage-ancien"
    assert app["capture_status"] == "done"
    assert (_root() / app["archive_dir"] / "offre" / "offre.pdf").is_file()


def test_rattrapage_et_captures_par_cli_refresh(data_dir, fake_capture, capsys) -> None:
    app = db.create_application({"title": "Stage ancien"})
    cli.maintain_archives()
    row = db.archive_row(app["id"])
    assert row["archive_dir"] and row["capture_status"] == "done"
    assert "1 dossier(s) créé(s), 1 capture(s)" in capsys.readouterr().out
    cli.maintain_archives()
    assert len([c for c in fake_capture.calls if "dest_dir" in c]) == 1


def test_creation_reservee_une_seule_fois(data_dir) -> None:
    app = db.create_application({"title": "Stage"})
    assert db.claim_archive(app["id"], None, "a")
    assert not db.claim_archive(app["id"], None, "b")  # l'autre processus a perdu
    assert db.claim_archive(app["id"], "a", "c")  # reprise après un arrêt avant la création du dossier
    assert archive.ensure_archive(app["id"]) and not archive.ensure_archive(app["id"])


# ---------------------------------------------------------------- annonce retirée


def test_verification_annonce_retiree_une_fois_par_jour(data_dir, clock, monkeypatch) -> None:
    fake = fake_capture_module(online=False)
    _use_capture(monkeypatch, fake)
    open_app = db.create_application({"title": "Ouverte", "url": "https://example.test/a"})
    db.create_application({"title": "Refusée", "url": "https://example.test/b", "status": "refuse"})
    db.create_application({"title": "Sans lien"})
    archive.ensure_archive(open_app["id"])

    assert archive.check_offers_online() == {"checked": 1, "skipped": 0}
    app = db.get_application(open_app["id"])
    assert app["offer_online"] is False and app["offer_checked_at_utc"] == "2026-10-20T08:00:00Z"
    assert "**Annonce** : retirée" in (_root() / app["archive_dir"] / "LISEZMOI.md").read_text(encoding="utf-8")

    clock(FIXED_NOW + timedelta(hours=10))  # même jour à Paris
    assert archive.check_offers_online() == {"checked": 0, "skipped": 1}
    fake_online = fake_capture_module(online=None)
    _use_capture(monkeypatch, fake_online)
    clock(FIXED_NOW + timedelta(days=1))
    assert archive.check_offers_online() == {"checked": 1, "skipped": 0}
    assert db.get_application(open_app["id"])["offer_online"] is None
    assert fake.calls == [{"online": "https://example.test/a"}]


# ---------------------------------------------------------------- informations et explorateur


def test_infos_archives(client) -> None:
    _create(client)
    _create(client, title="Autre")
    body = client.get("/api/archives/info").json()
    assert body["root"] == str(_root().resolve())
    assert body["count"] == 2 and body["size_bytes"] > 0
    assert body["browser"]["available"] is False


def test_ouvrir_le_dossier(client, monkeypatch) -> None:
    app = _create(client)
    url = f"/api/applications/{app['id']}/archive/open"
    monkeypatch.setattr(archive.sys, "platform", "linux")
    monkeypatch.setattr(archive.shutil, "which", lambda _name: None)
    response = client.post(url)
    assert response.status_code == 501
    assert response.json()["detail"] == "Aucun explorateur de fichiers n'est disponible sur cette machine."

    launched = []

    class FakeProcess:
        def __init__(self, args, **kwargs):
            launched.append((args, kwargs["start_new_session"]))

        def wait(self):
            return 0

    monkeypatch.setattr(archive.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(archive.subprocess, "Popen", FakeProcess)
    assert client.post(url).json() == {"ok": True}
    assert launched == [(["/usr/bin/xdg-open", str(_root() / app["archive_dir"])], True)]


def test_candidatures_exposent_les_champs_darchive(client) -> None:
    app = _create(client)
    listed = client.get("/api/applications").json()[0]
    assert {"archive_dir", "capture_status", "capture_level", "offer_online"} <= set(listed)
    assert "offer_snapshot_json" not in listed
    assert listed["archive_dir"] == app["archive_dir"]


# ---------------------------------------------------------------- migration 10


def test_migration_10_depuis_une_base_en_version_9(tmp_path) -> None:
    with sqlite3.connect(tmp_path / "v9.db") as conn:
        conn.execute("BEGIN")
        for _version, step in MIGRATIONS[:9]:
            step(conn)
        conn.execute("PRAGMA user_version = 9")
        conn.execute("INSERT INTO applications(title, created_at, updated_at) VALUES ('Ancienne', 'a', 'b')")
        conn.commit()

        migrate(conn)
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        columns = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
        assert set(db.ARCHIVE_FIELDS) <= columns
        row = conn.execute("SELECT title, archive_dir, capture_status, offer_online FROM applications").fetchone()
        assert row == ("Ancienne", None, None, None)
        conn.execute("UPDATE applications SET archive_id='x'")
        conn.execute("INSERT INTO applications(title, created_at, updated_at) VALUES ('Autre', 'a', 'b')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE applications SET archive_id='x' WHERE title='Autre'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE applications SET capture_status='inconnu'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE applications SET offer_online=2")
