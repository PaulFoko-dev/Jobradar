"""Un document produit avec une ancienne identité (CV sans nom, ancien e-mail) n'est jamais envoyé."""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from jobradar import application_flow, db, profile, tailoring

CURRENT = {"full_name": "Camille Durand", "email": "camille@example.test"}


@pytest.fixture
def documents(client, monkeypatch, tmp_path: Path):
    """Document dont le PDF contient l'identité donnée ; ats_check simulé lit le nom dans ce « PDF »."""
    def make(printed_name: str) -> dict:
        application = client.post("/api/applications", json={
            "title": "Stage SOC", "company": "Acme", "url": "https://boards.greenhouse.io/acme/jobs/42"}).json()
        pdf = tmp_path / f"cv-{application['id']}.pdf"
        pdf.write_text(f"CV de {printed_name}", encoding="utf-8")
        document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                      {"readability": 100, "ready": True}, [{"name": pdf.name, "path": pdf.name}])
        paths[document["id"]] = pdf
        return document

    def ats_check(pdf, *, composition, identity, offer_keywords, owned_keywords, docx_path=None):
        ready = bool(identity.get("full_name")) and identity["full_name"] in Path(pdf).read_text(encoding="utf-8")
        issues = [] if ready else [{"code": "name_missing", "field": "identity.full_name",
                                    "message": "Le nom du candidat est absent du CV."}]
        return types.SimpleNamespace(to_json=lambda: {"ready": ready, "readability": 100 if ready else 90,
                                                      "issues": issues})

    paths: dict[int, Path] = {}
    monkeypatch.setattr(tailoring, "_render_module", lambda: types.SimpleNamespace(ats_check=ats_check))
    monkeypatch.setattr(tailoring, "_report_json", lambda report: report.to_json())
    monkeypatch.setattr(tailoring, "document_file", lambda document_id, _name: paths[document_id])
    monkeypatch.setattr(profile, "load_profile", lambda: {"identity": CURRENT, "standard_answers": {}, "sources": []})
    return make


def test_cv_sans_nom_ne_peut_pas_etre_marque_pret(documents) -> None:
    document = documents("")  # généré quand le profil n'avait pas encore de nom
    with pytest.raises(db.Conflict, match="profil actuel"):
        tailoring.mark_ready(document["id"])
    assert db.get_document(document["id"])["status"] == "brouillon"


def test_document_a_jour_reste_utilisable(documents) -> None:
    document = documents("Camille Durand")
    assert tailoring.mark_ready(document["id"])["status"] == "pret"
    options = application_flow.options(document["application_id"])
    assert [item["id"] for item in options["documents"]] == [document["id"]]
    assert options["stale_documents"] == []


def test_document_deja_pret_mais_perime_n_est_plus_propose_ni_envoyable(documents) -> None:
    document = documents("")
    db.set_document_status(document["id"], "pret")  # marqué prêt avant le correctif

    options = application_flow.options(document["application_id"])

    assert options["documents"] == []
    assert [item["id"] for item in options["stale_documents"]] == [document["id"]]
    assert "Régénère-le" in options["stale_documents"][0]["message"]
    with pytest.raises(db.Conflict, match="profil actuel"):
        application_flow.start(document["application_id"], mode="manual", cv_document_id=document["id"],
                               letter_document_id=None)


def test_la_liste_des_documents_signale_le_document_perime(client, documents) -> None:
    stale, current = documents("Personne"), documents("Camille Durand")

    listed = {item["id"]: item for item in client.get(f"/api/applications/{stale['application_id']}/documents").json()}
    assert listed[stale["id"]]["current"]["ok"] is False
    assert "ne correspond plus" in listed[stale["id"]]["current"]["message"]
    listed = {item["id"]: item for item in client.get(f"/api/applications/{current['application_id']}/documents").json()}
    assert listed[current["id"]]["current"] == {"ok": True, "message": None}
