"""API, stockage et archive des documents de candidature."""
import base64
import json

from jobradar import ai, archive, db, profile, tailoring


def seed_profile():
    data = profile.empty_profile()
    data["identity"] = {"full_name": "Alice Martin", "email": "alice@example.test", "phone": "",
                        "city": "Paris", "links": []}
    data["experiences"] = [{
        "id": "exp-1", "title": "Analyste SOC", "organization": "Acme", "location": "Paris",
        "start": "2024-09", "end": "2025-03", "context": "", "technologies": ["Splunk"],
        "bullets": [{"id": "exp-1-b1", "text": "Analyse de 20 alertes", "variants": [], "provenance": {}}],
        "provenance": {},
    }]
    data["skills"] = [{"id": "skill-splunk", "name": "Splunk", "category": "Sécurité", "level": "avance",
                       "evidence": ["exp-1"], "provenance": {}, "validated_at": "2026-01-01"}]
    return profile.save_profile(data)


def composition():
    return {
        "kind": "cv", "language": "fr", "base_source_id": "src-1",
        "template": {"kind": "jobradar", "id": "sobre-ats"}, "headline": "Analyste SOC", "summary": "",
        "sections": [
            {"type": "experience", "title": "Expérience professionnelle", "groups": [], "items": [{
                "ref": "exp-1", "fields": {"title": "Analyste SOC", "organization": "Acme",
                                                "location": "Paris", "start": "2024-09", "end": "2025-03"},
                "bullets": [{"ref": "exp-1-b1", "text": "Analyse de 20 alertes"}],
            }]},
            {"type": "skills", "title": "Compétences", "items": [],
             "groups": [{"label": "Sécurité", "skills": [{"ref": "skill-splunk", "text": "Splunk (avancé)"}]}]},
        ],
    }


def create_archived_application(client):
    response = client.post("/api/applications", json={"title": "Stage SOC", "company": "CyberCo"})
    assert response.status_code == 200
    return response.json()


def test_migration_11_creates_documents_table(data_dir):
    with db.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 11
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
    assert columns == {"id", "application_id", "kind", "version", "composition_json", "template_json",
                       "ats_json", "status", "files_json", "created_at_utc", "updated_at_utc"}


def test_render_route_archives_version_lists_and_serves_files(client, fake_render):
    seed_profile()
    application = create_archived_application(client)

    response = client.post(f"/api/applications/{application['id']}/documents/render", json={
        "composition": composition(), "template": {"kind": "jobradar", "id": "sobre-ats"},
    })

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ats"]["readability"] == 100
    document = payload["documents"][0]
    assert document["status"] == "brouillon" and document["version"] == 1
    listed = client.get(f"/api/applications/{application['id']}/documents").json()
    assert listed[0]["id"] == document["id"]
    file_response = client.get(f"/api/documents/{document['id']}/file", params={"name": document["files"][0]["name"]})
    assert file_response.status_code == 200
    assert file_response.content.startswith(b"%PDF-")
    directory = archive.directory_of(application["id"])
    assert (directory / document["files"][0]["path"]).is_file()
    assert "CV v1" in (directory / "LISEZMOI.md").read_text(encoding="utf-8")


def test_ready_is_blocked_below_95_with_explicit_409(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    fake_render.ats_readability = 94.9
    rendered = client.post(f"/api/applications/{application['id']}/documents/render", json={
        "composition": composition(), "template": {"kind": "jobradar", "id": "sobre-ats"},
    }).json()

    response = client.post(f"/api/documents/{rendered['documents'][0]['id']}/ready")

    assert response.status_code == 409
    assert "95" in response.json()["detail"] and "Lisibilité" in response.json()["detail"]


def test_ready_then_sent_copies_exact_files(client):
    seed_profile()
    application = create_archived_application(client)
    document = client.post(f"/api/applications/{application['id']}/documents/render", json={
        "composition": composition(), "template": {"kind": "jobradar", "id": "sobre-ats"},
    }).json()["documents"][0]

    assert client.post(f"/api/documents/{document['id']}/ready").json()["status"] == "pret"
    sent = client.post(f"/api/documents/{document['id']}/sent")

    assert sent.status_code == 200 and sent.json()["status"] == "envoye"
    directory = archive.directory_of(application["id"])
    for item in document["files"]:
        assert (directory / "documents" / "envoye" / item["name"]).read_bytes() == (directory / item["path"]).read_bytes()
    assert "envoyé" in (directory / "LISEZMOI.md").read_text(encoding="utf-8")


def test_patch_revalidates_before_rerender(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    document = client.post(f"/api/applications/{application['id']}/documents/render", json={
        "composition": composition(), "template": {"kind": "jobradar", "id": "sobre-ats"},
    }).json()["documents"][0]
    invalid = composition()
    invalid["sections"][0]["items"][0]["fields"]["start"] = "2020-01"

    response = client.patch(f"/api/documents/{document['id']}", json={"composition": invalid})

    assert response.status_code == 400
    assert len(fake_render.calls) == 1  # seul le rendu initial a eu lieu


def test_source_route_checks_signature_and_maps_ai_error_to_502(client, monkeypatch):
    invalid = client.post("/api/profile/sources", json={
        "kind": "cv", "name": "Faux", "tags": [], "filename": "cv.pdf",
        "content_base64": base64.b64encode(b"pas un PDF").decode(),
    })
    assert invalid.status_code == 400

    monkeypatch.setattr(ai, "ask_ai", lambda *_a, **_k: (_ for _ in ()).throw(ai.QuotaError("Quota IA atteint")))
    failed = client.post("/api/profile/sources", json={
        "kind": "cv", "name": "Texte", "tags": [], "filename": "cv.txt",
        "content_base64": base64.b64encode(b"Alice Martin\nProfil").decode(),
    })
    assert failed.status_code == 502 and "Quota" in failed.json()["detail"]


def test_profile_routes_versions_conflicts_and_restore(client):
    first = seed_profile()
    updated = client.put("/api/profile", json={"headline": "Nouveau titre"})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    versions = client.get("/api/profile/versions").json()
    assert {item["version"] for item in versions} == {1, 2}
    inspected = client.get(f"/api/profile/versions/{first['version']}")
    assert inspected.status_code == 200 and inspected.json()["headline"] != "Nouveau titre"
    assert client.get("/api/profile/versions/999999").status_code == 404

    restored = client.post(f"/api/profile/restore/{first['version']}")
    assert restored.status_code == 200 and restored.json()["version"] == 3
    assert client.get("/api/profile/conflicts").json() == []


def test_templates_include_jobradar_and_original_docx(client):
    data = profile.empty_profile()
    data["sources"] = [{"id": "src-7", "kind": "cv", "name": "CV original", "format": "docx",
                        "filename": "cv.docx", "tags": [], "language": "fr", "imported_at": "", "sha256": ""}]
    profile.save_profile(data)

    templates = client.get("/api/templates").json()

    assert {item["id"] for item in templates} == {"sobre-ats", "src-7"}


def test_document_file_rejects_path_traversal(client):
    seed_profile()
    application = create_archived_application(client)
    document = client.post(f"/api/applications/{application['id']}/documents/render", json={
        "composition": composition(), "template": {"kind": "jobradar", "id": "sobre-ats"},
    }).json()["documents"][0]

    response = client.get(f"/api/documents/{document['id']}/file", params={"name": "../candidature.json"})

    assert response.status_code == 400


def test_document_pdf_is_inline_and_docx_is_attachment(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    document = render(client, application).json()["documents"][0]
    responses = {item["name"]: client.get(f"/api/documents/{document['id']}/file", params={"name": item["name"]})
                 for item in document["files"]}
    assert responses[next(name for name in responses if name.endswith(".pdf"))].headers["content-disposition"].startswith(
        "inline")
    assert responses[next(name for name in responses if name.endswith(".docx"))].headers["content-disposition"].startswith(
        "attachment")


def test_render_warnings_are_persisted_in_ats_report(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    fake_render.render_warnings = ["LibreOffice est indisponible.", "LibreOffice est indisponible."]

    payload = render(client, application).json()

    assert payload["ats"]["render_warnings"] == ["LibreOffice est indisponible."]
    assert payload["documents"][0]["ats"]["render_warnings"] == ["LibreOffice est indisponible."]


def letter():
    return {"kind": "lettre", "language": "fr", "base_source_id": None,
            "template": {"kind": "jobradar", "id": "sobre-ats"}, "subject": "Candidature", "greeting": "Bonjour,",
            "paragraphs": ["Chez Acme, j'ai analysé 20 alertes."], "closing": "Cordialement", "facts_refs": ["exp-1"]}


def render(client, application, **body):
    return client.post(f"/api/applications/{application['id']}/documents/render",
                       json={"composition": composition()} | body)


def test_render_cv_and_letter_share_a_version_with_portable_names(client, fake_render):
    seed_profile()
    application = create_archived_application(client)

    payload = render(client, application, letter=letter()).json()

    cv, lettre = sorted(payload["documents"], key=lambda item: item["kind"])
    assert (cv["version"], lettre["version"]) == (1, 1)
    assert cv["files"][0]["name"] == "Alice_Martin_CV_CyberCo.pdf"
    assert lettre["files"][0]["name"] == "Alice_Martin_Lettre_CyberCo.pdf"
    assert fake_render.calls[0]["identity"]["full_name"] == "Alice Martin"
    version_dir = archive.directory_of(application["id"]) / cv["files"][0]["path"].rsplit("/", 1)[0]
    stored = json.loads((version_dir / "composition.json").read_text(encoding="utf-8"))
    assert stored["composition"]["headline"] == "Analyste SOC" and stored["letter"]["facts_refs"] == ["exp-1"]
    assert fake_render.ats_calls[0]["owned_keywords"] == []
    assert render(client, application).json()["documents"][0]["version"] == 2


def test_render_refuses_invalid_composition_without_writing_anything(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    invalid = composition()
    invalid["sections"][0]["items"][0]["bullets"][0]["text"] = "Analyse de 99 alertes"

    response = render(client, application, composition=invalid)

    assert response.status_code == 400 and "nombre absent" in response.json()["detail"]
    assert fake_render.calls == []
    assert client.get(f"/api/applications/{application['id']}/documents").json() == []
    documents_dir = archive.directory_of(application["id"]) / "documents"
    assert sorted(path.name for path in documents_dir.iterdir()) == ["README.txt"]


def test_render_error_is_reported_and_leaves_no_partial_version(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    fake_render.fail = "Chromium est introuvable."

    response = render(client, application)

    assert response.status_code == 500
    assert response.json()["detail"] == "Rendu du document impossible : Chromium est introuvable."
    documents_dir = archive.directory_of(application["id"]) / "documents"
    assert sorted(path.name for path in documents_dir.iterdir()) == ["README.txt"]


def test_patch_rerenders_in_place_resets_status_and_records_user_wording(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    document = render(client, application).json()["documents"][0]
    assert client.post(f"/api/documents/{document['id']}/ready").status_code == 200
    edited = composition()
    edited["sections"][0]["items"][0]["bullets"][0]["text"] = "Qualification de 20 alertes de sécurité"

    response = client.patch(f"/api/documents/{document['id']}", json={"composition": edited})

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["status"] == "brouillon" and updated["version"] == 1
    assert [item["path"] for item in updated["files"]] == [item["path"] for item in document["files"]]
    directory = archive.directory_of(application["id"])
    stored = json.loads((directory / document["files"][0]["path"]).with_name("composition.json")
                        .read_text(encoding="utf-8"))
    assert stored["composition"]["sections"][0]["items"][0]["bullets"][0]["text"].startswith("Qualification")
    variant = profile.load_profile()["experiences"][0]["bullets"][0]["variants"][0]
    assert variant["text"] == "Qualification de 20 alertes de sécurité" and variant["validated_at"]
    assert len(fake_render.calls) == 2


def test_prompt_improves_rendered_cv_as_a_new_controlled_version(client, fake_render, monkeypatch):
    seed_profile()
    application = create_archived_application(client)
    source = render(client, application).json()["documents"][0]

    def revised(*_args, **_kwargs):
        return {"composition": {
            "headline": "Analyste SOC", "summary": "",
            "sections": [
                {"type": "experience", "title": "Expérience professionnelle", "groups": [],
                 "items": [{"ref": "exp-1", "bullets": [
                     {"ref": "exp-1-b1", "text": "Analyse de 20 alertes"}]}]},
                {"type": "skills", "title": "Compétences", "items": [],
                 "groups": [{"label": "Sécurité", "skills": [
                     {"ref": "skill-splunk", "text": "Splunk (avancé)"}]}]},
            ],
        }, "letter": None, "questions": {}}

    monkeypatch.setattr(ai, "ask_ai", revised)
    response = client.post(f"/api/documents/{source['id']}/refine", json={
        "instruction": "Rends le CV plus direct et conserve uniquement les éléments pertinents.",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source_document_id"] == source["id"]
    assert body["documents"][0]["version"] == source["version"] + 1
    assert db.get_document(source["id"])["version"] == source["version"]
    assert len(fake_render.calls) == 2


def test_prompt_improvement_rejects_empty_or_oversized_instruction(client, fake_render):
    seed_profile()
    application = create_archived_application(client)
    source = render(client, application).json()["documents"][0]
    assert client.post(f"/api/documents/{source['id']}/refine", json={"instruction": ""}).status_code == 400
    assert client.post(f"/api/documents/{source['id']}/refine",
                       json={"instruction": "x" * 2001}).status_code == 400


def test_sent_requires_ready_and_sent_documents_are_frozen(client):
    seed_profile()
    application = create_archived_application(client)
    document = render(client, application).json()["documents"][0]

    early = client.post(f"/api/documents/{document['id']}/sent")
    client.post(f"/api/documents/{document['id']}/ready")
    client.post(f"/api/documents/{document['id']}/sent")
    patched = client.patch(f"/api/documents/{document['id']}", json={"composition": composition()})
    ready_again = client.post(f"/api/documents/{document['id']}/ready")

    assert early.status_code == 409 and "prêt" in early.json()["detail"]
    assert patched.status_code == 409 and "envoyé" in patched.json()["detail"]
    assert ready_again.status_code == 409


def test_prepare_and_answers_routes(client, monkeypatch):
    application = create_archived_application(client)
    response = client.post(f"/api/applications/{application['id']}/documents/prepare", json={"kinds": ["cv"]})
    assert response.status_code == 400 and "Importe d'abord" in response.json()["detail"]

    seed_profile()
    data = profile.load_profile() | {"sources": [{"id": "src-1", "kind": "cv", "name": "CV", "tags": [],
                                                  "filename": "cv.txt", "format": "txt", "language": "fr"}]}
    profile.save_profile(data)
    monkeypatch.setattr(ai, "ask_ai", lambda *_a, **_k: (_ for _ in ()).throw(ai.QuotaError("Quota IA atteint")))
    failed = client.post(f"/api/applications/{application['id']}/documents/prepare", json={"kinds": ["cv"]})
    assert failed.status_code == 502 and "Quota" in failed.json()["detail"]

    monkeypatch.setattr(ai, "ask_ai", lambda *_a, **_k: {
        "composition": {"headline": "Analyste SOC", "summary": "", "sections": composition()["sections"]},
        "letter": None, "questions": {"skills": [], "items": [], "missing": []}})
    prepared = client.post(f"/api/applications/{application['id']}/documents/prepare",
                           json={"kinds": ["cv"], "base_cv": "src-1"})
    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    assert set(body) == {"session", "composition", "letter", "questions", "suggested_bases", "notes"}

    answered = client.post(f"/api/applications/{application['id']}/documents/answers",
                           json={"session": body["session"], "skills": [{"name": "Sentinel", "status": "inconnu"}]})
    assert answered.status_code == 200, answered.text
    missing = client.post(f"/api/applications/{application['id']}/documents/answers",
                          json={"session": "00000000-0000-4000-8000-000000000000"})
    assert missing.status_code == 404


def test_missing_answers_are_strict_memorized_and_removed_from_session(client, monkeypatch):
    application = create_archived_application(client)
    seed_profile()
    data = profile.load_profile() | {"sources": [{"id": "src-1", "kind": "cv", "name": "CV", "tags": [],
                                                  "filename": "cv.txt", "format": "txt", "language": "fr"}]}
    profile.save_profile(data)
    monkeypatch.setattr(ai, "ask_ai", lambda *_a, **_k: {
        "composition": {"headline": "Analyste SOC", "summary": "", "sections": composition()["sections"]},
        "letter": None, "questions": {"skills": [], "items": [], "missing": ["Date de disponibilité"]}})
    prepared = client.post(f"/api/applications/{application['id']}/documents/prepare",
                           json={"kinds": ["cv"], "base_cv": "src-1"}).json()

    answered = client.post(f"/api/applications/{application['id']}/documents/answers", json={
        "session": prepared["session"],
        "missing": [{"question": "Date de disponibilité", "answer": "Septembre 2026", "remember": True}],
    })

    assert answered.status_code == 200, answered.text
    assert answered.json()["questions"]["missing"] == []
    stored = profile.load_profile()["standard_answers"]["date de disponibilite"]
    assert stored["answer"] == "Septembre 2026"
    assert stored["provenance"]["source"] == f"candidature:{application['id']}"
    invalid = client.post(f"/api/applications/{application['id']}/documents/answers", json={
        "session": prepared["session"], "missing": [{"question": "Question injectée", "answer": "x"}],
    })
    assert invalid.status_code == 400


def test_base_suggestions_are_available_before_prepare(client):
    application = create_archived_application(client)
    data = profile.empty_profile()
    data["sources"] = [{"id": "src-1", "kind": "cv", "name": "CV SOC", "tags": ["soc"],
                       "filename": "cv.txt", "format": "txt", "language": "fr", "keywords": ["SOC"]}]
    profile.save_profile(data)

    response = client.get(f"/api/applications/{application['id']}/documents/bases",
                          params={"kind": "cv", "language": "fr"})

    assert response.status_code == 200
    assert response.json()[0]["id"] == "src-1"
    assert client.get("/api/applications/999999/documents/bases").status_code == 404


def test_source_route_size_limit_and_conflict_resolution(client, monkeypatch):
    too_big = client.post("/api/profile/sources", json={
        "kind": "cv", "name": "Gros", "tags": [], "filename": "cv.txt",
        "content_base64": "A" * 13_981_020})
    assert too_big.status_code == 400 and "10 Mo" in too_big.json()["detail"]

    replies = [
        {"headline": "", "summary": "", "projects": [], "education": [], "certifications": [], "languages": [],
         "skills": [], "experiences": [{"title": "Analyste", "organization": "Acme", "location": "", "start": "2024",
                                        "end": end, "context": "", "technologies": [], "bullets": []}]}
        for end in ("2025", "2026")]
    monkeypatch.setattr(ai, "ask_ai", lambda *_a, **_k: replies.pop(0))
    for name in ("Un", "Deux"):
        imported = client.post("/api/profile/sources", json={
            "kind": "cv", "name": name, "tags": ["soc"], "filename": "cv.md",
            "content_base64": base64.b64encode(b"# CV").decode()})
        assert imported.status_code == 200, imported.text
    conflicts = client.get("/api/profile/conflicts").json()
    assert [(item["field"], item["values"]) for item in conflicts] == [("end", ["2025", "2026"])]

    resolved = client.post("/api/profile/conflicts/resolve", json={"conflict_id": conflicts[0]["id"], "value": "2026"})
    assert resolved.status_code == 200 and resolved.json()["experiences"][0]["end"] == "2026"
    assert client.get("/api/profile/conflicts").json() == []
    assert client.delete("/api/profile/sources/src-1").status_code == 200
    assert client.delete("/api/profile/sources/src-2").status_code == 400
    assert client.delete("/api/profile/sources/src-9").status_code == 404


def test_original_docx_template_uses_the_template_map(client, fake_render):
    seed_profile()
    data = profile.load_profile()
    data["sources"] = [{"id": "src-1", "kind": "cv", "name": "CV", "tags": [], "filename": "cv.docx",
                        "format": "docx", "language": "fr"}]
    profile.save_profile(data)
    profile.atomic_write(profile.sources_dir() / "src-1.docx", b"PK\x03\x04")
    application = create_archived_application(client)

    response = render(client, application, template={"kind": "original", "id": "src-1"})

    assert response.status_code == 200, response.text
    assert fake_render.calls[0]["analyze"].name == "src-1.docx"
    assert fake_render.calls[1]["template"] == {"kind": "original", "id": "src-1"}
    assert tailoring.list_templates()[-1]["kind"] == "original"
