"""Dialogue d'adaptation et garde-fous anti-invention, sans vraie IA."""
from copy import deepcopy

import pytest

from jobradar import ai, db, profile, tailoring

SECRETS = ("Alice", "Martin", "alice@example.test", "0612345678", "06 12 34 56 78", "Paris", "github.com/alice")
OFFER = {"title": "Stage Analyste SOC", "company": "CyberCo", "description":
         "Vous surveillerez les alertes avec Splunk et Microsoft Sentinel. Kubernetes est un plus. "
         "Équipe de 12 analystes.",
         "analysis": {"cv_keywords": ["Splunk", "Sentinel", "Kubernetes"], "summary": "Stage SOC"}}


def candidate_profile():
    data = profile.empty_profile()
    data["identity"] = {"full_name": "Alice Martin", "email": "alice@example.test", "phone": "06 12 34 56 78",
                        "city": "Paris", "links": [{"label": "GitHub", "url": "https://github.com/alice"}]}
    data["headline"] = "Étudiante en cybersécurité"
    data["experiences"] = [{
        "id": "exp-1", "title": "Analyste SOC", "organization": "Acme", "location": "Paris",
        "start": "2024-09", "end": "2025-03", "context": "Stage", "technologies": ["Splunk"],
        "bullets": [
            {"id": "exp-1-b1", "text": "Analyse de 20 alertes avec Splunk", "provenance": {},
             "variants": [{"text": "Tri de 25 alertes par jour", "theme": "soc", "validated_at": "2026-01-02"}]},
            {"id": "exp-1-b2", "text": "Rédaction de rapports pour Alice Martin", "variants": [], "provenance": {}},
        ],
        "provenance": {"source": "src-1", "at": "2026-01-01T00:00:00Z"},
    }, {
        "id": "exp-2", "title": "Équipier", "organization": "Restaurant", "location": "Lyon",
        "start": "2022", "end": "2022", "context": "", "technologies": [],
        "bullets": [{"id": "exp-2-b1", "text": "Service en salle", "variants": [], "provenance": {}}],
        "provenance": {},
    }]
    data["projects"] = [{"id": "proj-1", "title": "Lab SIEM", "setting": "école", "start": "2025-01",
                         "end": "2025-03", "technologies": ["Python"], "link": None, "provenance": {},
                         "bullets": [{"id": "proj-1-b1", "text": "Scripts Python de collecte", "variants": [],
                                      "provenance": {}}]}]
    data["skills"] = [
        {"id": "skill-1", "name": "Splunk", "category": "Sécurité", "level": "avance",
         "evidence": ["exp-1"], "provenance": {}, "validated_at": "2026-01-01T00:00:00Z"},
        {"id": "skill-2", "name": "Python", "category": "Langages", "level": "notion",
         "evidence": ["proj-1"], "provenance": {}, "validated_at": "2026-01-01T00:00:00Z"},
    ]
    data["unknown_skills"] = [{"name": "Kubernetes", "declared_at": "2026-01-01", "application_id": 1}]
    data["sources"] = [
        {"id": "src-1", "kind": "cv", "name": "CV SOC", "tags": ["soc"], "filename": "cv.txt", "format": "txt",
         "language": "fr", "imported_at": "2026-01-01", "sha256": "x", "keywords": ["SOC", "Splunk"]},
        {"id": "src-2", "kind": "lettre", "name": "Lettre", "tags": [], "filename": "lettre.txt", "format": "txt",
         "language": "fr", "imported_at": "2026-01-01", "sha256": "x", "keywords": []},
    ]
    return data


def valid_composition():
    return {
        "kind": "cv", "language": "fr", "base_source_id": "src-1",
        "template": {"kind": "jobradar", "id": "sobre-ats"},
        "headline": "Stagiaire SOC", "summary": "Analyse sécurité",
        "sections": [
            {"type": "experience", "title": "Expérience professionnelle", "groups": [], "items": [{
                "ref": "exp-1", "fields": {"title": "Analyste SOC", "organization": "Acme",
                                           "location": "Paris", "start": "2024-09", "end": "2025-03"},
                "bullets": [{"ref": "exp-1-b1", "text": "Qualification de 20 alertes avec Splunk"}],
            }]},
            {"type": "skills", "title": "Compétences", "items": [], "groups": [{
                "label": "Sécurité", "skills": [{"ref": "skill-1", "text": "Splunk (avancé)"},
                                                {"ref": "skill-2", "text": "Python (notion)"}],
            }]},
        ],
    }


def valid_letter():
    return {"kind": "lettre", "language": "fr", "base_source_id": "src-2",
            "template": {"kind": "jobradar", "id": "sobre-ats"}, "subject": "Candidature — Stage Analyste SOC",
            "greeting": "Madame, Monsieur,", "closing": "Cordialement",
            "paragraphs": ["Rejoindre votre équipe de 12 analystes me motive.",
                           "Chez Acme, j'ai analysé 20 alertes avec Splunk."],
            "facts_refs": ["exp-1"]}


def ai_cv(**overrides):
    """Réponse de l'assistant au format du schéma (sans champs factuels)."""
    return {
        "headline": "Stagiaire Analyste SOC", "summary": "Étudiante en cybersécurité",
        "sections": [
            {"type": "experience", "title": "Expérience professionnelle", "groups": [],
             "items": [{"ref": "exp-1", "bullets": [{"ref": "exp-1-b1", "text": "Qualification de 20 alertes "
                                                                                 "avec Splunk"}]}]},
            {"type": "skills", "title": "Compétences", "items": [],
             "groups": [{"label": "Sécurité", "skills": [{"ref": "skill-1", "text": "Splunk (avancé)"}]}]},
        ],
    } | overrides


@pytest.fixture
def saved_profile(data_dir):
    saved = profile.save_profile(candidate_profile())
    profile.atomic_write(profile.sources_dir() / "src-1.txt",
                         "Alice Martin\nalice@example.test — 06 12 34 56 78\n75001 Paris\nAnalyste SOC".encode())
    profile.atomic_write(profile.sources_dir() / "src-2.txt",
                         "Madame, Monsieur,\nJe souhaite rejoindre votre équipe.\nAlice Martin".encode())
    return saved


@pytest.fixture
def assistant(saved_profile, monkeypatch):
    """Faux assistant : renvoie les réponses prévues et garde les prompts envoyés."""
    calls = []
    replies = []

    def fake_ask(prompt, schema, task):
        calls.append({"prompt": prompt, "schema": schema, "task": task})
        return deepcopy(replies.pop(0))

    monkeypatch.setattr(ai, "ask_ai", fake_ask)
    monkeypatch.setattr(tailoring, "_snapshot", lambda _aid: deepcopy(OFFER))
    return calls, replies


@pytest.fixture
def application(saved_profile):
    return db.create_application({"title": "Stage SOC", "company": "CyberCo"})


def tailored(composition=None, letter=None, questions=None):
    return {"composition": composition if composition is not None else ai_cv(), "letter": letter,
            "questions": questions or {"skills": [], "items": [], "missing": []}}


# ---------------------------------------------------------------- validateur

def test_valid_composition_accepts_number_preserving_rewrite(saved_profile):
    assert tailoring.validate_composition(valid_composition(), saved_profile)["headline"] == "Stagiaire SOC"


def test_validated_variant_is_accepted_verbatim_with_its_own_numbers(saved_profile):
    composition = valid_composition()
    composition["sections"][0]["items"][0]["bullets"][0]["text"] = "Tri de 25 alertes par jour"
    tailoring.validate_composition(composition, saved_profile)


@pytest.mark.parametrize(("change", "message"), [
    (lambda c: c["sections"][0]["items"][0].update(ref="exp-404"), "inexistante"),
    (lambda c: c["sections"][0]["items"][0]["bullets"][0].update(ref="exp-1-b9"), "inexistante"),
    (lambda c: c["sections"][0]["items"][0]["fields"].update(end="2026-03"), "diffère du profil"),
    (lambda c: c["sections"][0]["items"][0]["fields"].update(organization="Google"), "diffère du profil"),
    (lambda c: c["sections"][0]["items"][0]["fields"].update(salary="1"), "non autorisés"),
    (lambda c: c["sections"][0]["items"][0]["bullets"][0].update(text="Qualification de 200 alertes avec Splunk"),
     "nombre absent"),
    (lambda c: c["sections"][0]["items"][0]["bullets"][0].update(text="Analyse de 20 alertes Kubernetes"),
     "inconnue"),
    (lambda c: c["sections"][0]["items"][0]["bullets"][0].update(text="Analyse de 20 alertes en Python"),
     "absent de cet élément"),
    (lambda c: c["sections"][1]["groups"][0]["skills"][1].update(text="Python"), "notion"),
    (lambda c: c["sections"][1]["groups"][0]["skills"][1].update(text="Python (avancé)"), "diffère"),
    (lambda c: c["sections"][1]["groups"][0]["skills"][0].update(text="Rust (avancé)"), "ne nomme pas"),
    (lambda c: c["sections"][1]["groups"][0]["skills"].append({"ref": "skill-rust", "text": "Rust"}), "inexistante"),
    (lambda c: c["sections"][1]["groups"][0]["skills"].append({"ref": "skill-1", "text": "Splunk"}), "deux fois"),
    (lambda c: c.update(headline="Analyste SOC, 5 ans d'expérience"), "nombre absent"),
    (lambda c: c.update(summary="Passionnée par Kubernetes"), "inconnue"),
    (lambda c: c.update(template={"kind": "original", "id": "src-1"}), "DOCX"),
    (lambda c: c.update(template={"kind": "jobradar", "id": "fantaisie"}), "inconnu"),
    (lambda c: c.update(language="de"), "langue"),
])
def test_validator_rejects_invention(saved_profile, change, message):
    composition = valid_composition()
    change(composition)
    with pytest.raises(tailoring.CompositionError, match=message):
        tailoring.validate_composition(composition, saved_profile)


def test_validator_rejects_offer_skill_absent_from_profile(saved_profile):
    composition = valid_composition()
    composition["sections"][0]["items"][0]["bullets"][0]["text"] = "Analyse de 20 alertes Splunk et Sentinel"
    tailoring.validate_composition(composition, saved_profile)  # sans l'offre, « Sentinel » n'est pas connu
    with pytest.raises(tailoring.CompositionError, match="demandé par l'offre"):
        tailoring.validate_composition(composition, saved_profile, ["Sentinel"])


def test_letter_validator(saved_profile):
    keywords = OFFER["analysis"]["cv_keywords"]
    assert tailoring.validate_letter(valid_letter(), saved_profile, keywords, OFFER)["facts_refs"] == ["exp-1"]
    for change, message in [
        (lambda letter: letter.update(facts_refs=["proj-invente"]), "absent du profil"),
        (lambda letter: letter["paragraphs"].append("J'ai traité 300 incidents."), "nombre absent"),
        (lambda letter: letter["paragraphs"].append("Je maîtrise Kubernetes."), "inconnue"),
        (lambda letter: letter["paragraphs"].append("J'utilise Sentinel."), "demandé par l'offre"),
        (lambda letter: letter.update(paragraphs=[]), "paragraphe"),
    ]:
        letter = valid_letter()
        change(letter)
        with pytest.raises(tailoring.CompositionError, match=message):
            tailoring.validate_letter(letter, saved_profile, keywords, OFFER)


# ---------------------------------------------------------------- préparation

def test_prepare_never_sends_identity_and_builds_facts_from_profile(assistant, application):
    calls, replies = assistant
    replies.append(tailored(questions={"skills": [], "items": [], "missing": []}))

    result = tailoring.prepare(application["id"], kinds=["cv"])

    prompt = calls[0]["prompt"]
    assert calls[0]["task"] == "tailoring"
    for secret in SECRETS:
        assert secret.casefold() not in prompt.casefold(), secret
    assert "{{NOM}}" in prompt and "{{VILLE}}" in prompt and "Analyste SOC" in prompt
    item = result["composition"]["sections"][0]["items"][0]
    assert item["fields"] == {"title": "Analyste SOC", "organization": "Acme", "location": "Paris",
                              "start": "2024-09", "end": "2025-03"}
    assert result["composition"]["base_source_id"] == "src-1"
    assert result["composition"]["template"] == {"kind": "jobradar", "id": "sobre-ats"}
    assert any("DOCX" in note for note in result["notes"])


def test_refine_masks_instruction_preserves_source_and_revalidates(assistant, application):
    calls, replies = assistant
    replies.append(tailored())
    prepared = tailoring.prepare(application["id"], kinds=["cv"])
    revised = ai_cv(summary="Profil orienté analyse de sécurité")
    replies.append(tailored(composition=revised))

    result = tailoring.refine(application["id"], {
        "session": prepared["session"],
        "instruction": "Raccourcis pour Alice Martin à Paris",
        "composition": prepared["composition"],
    })

    assert "Alice Martin" not in calls[-1]["prompt"] and "{{NOM}}" in calls[-1]["prompt"]
    assert "Paris" not in calls[-1]["prompt"] and "{{VILLE}}" in calls[-1]["prompt"]
    assert result["composition"]["base_source_id"] == "src-1"
    assert result["composition"]["summary"] == "Profil orienté analyse de sécurité"


def test_refine_rejects_tampered_current_preview_before_calling_ai(assistant, application):
    calls, replies = assistant
    replies.append(tailored())
    prepared = tailoring.prepare(application["id"], kinds=["cv"])
    tampered = deepcopy(prepared["composition"])
    tampered["summary"] = "Experte Kubernetes"

    with pytest.raises(tailoring.CompositionError, match="inconnue"):
        tailoring.refine(application["id"], {
            "session": prepared["session"], "instruction": "Améliore", "composition": tampered,
        })
    assert len(calls) == 1


def test_prepare_filters_questions_and_adds_missing_offer_skills(assistant, application):
    calls, replies = assistant
    replies.append(tailored(questions={
        "skills": [{"name": "Splunk", "excerpt": "Splunk"}, {"name": "Kubernetes", "excerpt": "x"},
                   {"name": "Terraform", "excerpt": "inventé"}],
        "items": [{"item_id": "exp-2", "reason": "Peu lié au SOC", "replacement_ids": ["proj-1", "proj-404"]},
                  {"item_id": "exp-999", "reason": "?", "replacement_ids": []}],
        "missing": ["Date de disponibilité", "Date de disponibilité"],
    }))

    questions = tailoring.prepare(application["id"], kinds=["cv"])["questions"]

    assert [item["name"] for item in questions["skills"]] == ["Sentinel"]
    assert "Microsoft Sentinel" in questions["skills"][0]["excerpt"]
    assert questions["items"] == [{"item_id": "exp-2", "reason": "Peu lié au SOC", "replacement_ids": ["proj-1"]}]
    assert questions["missing"] == ["Date de disponibilité"]


def test_questions_keep_only_important_unproven_skills(saved_profile):
    profile.atomic_write(profile.sources_dir() / "src-1.txt", b"CV technique : pratique de Docker en projet")
    offer = {
        "title": "Consultant risques",
        "description": "La maîtrise d'EBIOS RM est obligatoire. Docker est requis. Autonomie attendue.",
        "analysis": {"cv_keywords": ["Autonomie", "Docker", "EBIOS RM"],
                     "profile": ["Maîtrise d'EBIOS RM obligatoire"]},
    }
    context = tailoring._context(saved_profile, tailoring.offer_keywords(offer))
    raw = {"skills": [{"name": name} for name in ("Autonomie", "Docker", "EBIOS RM")],
           "items": [], "missing": []}

    questions = tailoring._questions(raw, context, offer, "")

    assert [item["name"] for item in questions["skills"]] == ["EBIOS RM"]


def test_context_recognises_a_known_skill_inside_a_longer_offer_label(saved_profile):
    context = tailoring._context(saved_profile, ["Microsoft Splunk"])
    assert context.absent == []


@pytest.mark.parametrize("name", ["Leadership", "Capacité d'adaptation", "Esprit critique",
                                   "Problem solving", "Communication skills", "Travail en équipe"])
def test_soft_skills_never_generate_questions(saved_profile, name):
    offer = {"title": "Stage", "description": f"{name} obligatoire", "analysis": {"cv_keywords": [name]}}
    context = tailoring._context(saved_profile, [name])
    raw = {"skills": [{"name": name}], "items": [], "missing": []}
    assert tailoring._questions(raw, context, offer, "")["skills"] == []


def test_prepare_repairs_ai_inventions_before_validation(assistant, application):
    _calls, replies = assistant
    composition = ai_cv(headline="Analyste SOC, 3 ans d'expérience")
    composition["sections"][0]["items"][0]["bullets"] = [
        {"ref": "exp-1-b1", "text": "Analyse de 500 alertes avec Splunk et Sentinel"},
        {"ref": "exp-1-b404", "text": "Puce inventée"}]
    composition["sections"][0]["items"].append({"ref": "exp-404", "bullets": []})
    composition["sections"][1]["groups"][0]["skills"].append({"ref": "skill-2", "text": "Python (expert)"})
    replies.append(tailored(composition))

    result = tailoring.prepare(application["id"], kinds=["cv"])

    shown = result["composition"]
    # Titre inventé écarté ; repris du profil, il doublait l'accroche : il est donc aligné sur l'intitulé de l'offre.
    assert shown["headline"] == "Stage Analyste SOC"
    assert [item["ref"] for item in shown["sections"][0]["items"]] == ["exp-1"]
    assert shown["sections"][0]["items"][0]["bullets"] == [{"ref": "exp-1-b1",
                                                            "text": "Analyse de 20 alertes avec Splunk"}]
    assert {"ref": "skill-2", "text": "Python (notion)"} in shown["sections"][1]["groups"][0]["skills"]
    assert len(result["notes"]) >= 4
    tailoring.validate_composition(shown, profile.load_profile(), OFFER["analysis"]["cv_keywords"])


def test_prepare_uses_base_language_unless_english_is_requested(assistant, application):
    _calls, replies = assistant
    data = profile.load_profile()
    data["sources"][0]["language"] = "en"
    data["sources"][1]["language"] = "fr"
    profile.save_profile(data)
    replies += [tailored(letter=valid_letter() | {"facts_refs": ["exp-1"]}), tailored()]

    first = tailoring.prepare(application["id"], kinds=["cv", "lettre"])
    second = tailoring.prepare(application["id"], kinds=["cv"], language="fr")

    assert first["composition"]["language"] == "en"
    assert first["letter"]["language"] == "fr" and first["letter"]["base_source_id"] == "src-2"
    assert second["composition"]["language"] == "fr"


def test_prepare_requires_a_cv_base_and_rejects_bad_requests(assistant, application):
    with pytest.raises(tailoring.CompositionError, match="cv"):
        tailoring.prepare(application["id"], kinds=["photo"])
    with pytest.raises(tailoring.CompositionError, match="langue"):
        tailoring.prepare(application["id"], kinds=["cv"], language="de")
    with pytest.raises(tailoring.CompositionError, match="introuvable"):
        tailoring.prepare(application["id"], kinds=["cv"], base_cv="src-2")
    with pytest.raises(tailoring.CompositionError, match="DOCX"):
        tailoring.prepare(application["id"], kinds=["cv"], template={"kind": "original", "id": "src-1"})


def test_invalid_ai_letter_is_corrected_instead_of_silently_discarded(assistant, application):
    calls, replies = assistant
    replies += [tailored(letter=valid_letter() | {"paragraphs": ["J'ai géré 40 serveurs."]}),
                tailored(composition=None, letter=valid_letter())]

    result = tailoring.prepare(application["id"], kinds=["cv", "lettre"])

    assert result["letter"] is not None
    assert len(calls) == 2 and "CORRECTION OBLIGATOIRE DE LA LETTRE" in calls[1]["prompt"]
    assert any("corrigée automatiquement" in note for note in result["notes"])


def test_requested_letter_never_falls_back_to_cv_only(assistant, application):
    _calls, replies = assistant
    replies += [tailored(letter=None), tailored(composition=None, letter=None)]
    with pytest.raises(tailoring.CompositionError, match="lettre demandée"):
        tailoring.prepare(application["id"], kinds=["cv", "lettre"])


# ---------------------------------------------------------------- réponses du dialogue

def test_answers_are_saved_with_provenance_and_never_asked_again(assistant, application):
    calls, replies = assistant
    asked = {"skills": [{"name": "Sentinel", "excerpt": ""}],
             "items": [{"item_id": "exp-2", "reason": "Hors sujet", "replacement_ids": ["proj-1"]}], "missing": []}
    composition = ai_cv()
    composition["sections"][0]["items"].append({"ref": "exp-2", "bullets": [{"ref": "exp-2-b1",
                                                                               "text": "Service en salle"}]})
    replies.append(tailored(composition, questions=asked))
    first = tailoring.prepare(application["id"], kinds=["cv"])
    assert [item["name"] for item in first["questions"]["skills"]] == ["Sentinel"]

    result = tailoring.answer(application["id"], {
        "session": first["session"],
        "skills": [{"name": "Sentinel", "status": "notion", "evidence": ["proj-1", "TP SOC"]}],
        "items": [{"item_id": "exp-2", "action": "remplacer", "replacement_id": "proj-1"}],
    })

    saved = profile.load_profile()
    sentinel = next(item for item in saved["skills"] if item["name"] == "Sentinel")
    assert sentinel["level"] == "notion" and sentinel["evidence"] == ["proj-1"]
    assert sentinel["evidence_note"] == "TP SOC"
    assert sentinel["provenance"]["source"] == f"candidature:{application['id']}"
    decision = saved["exclusions"][0]
    assert decision | {"decided_at": "x"} == {
        "item_id": "exp-2", "job_family": "soc", "action": "remplacer", "replacement_id": "proj-1",
        "decided_at": "x", "application_id": application["id"],
        "provenance": {"source": f"candidature:{application['id']}", "at": decision["decided_at"]}}
    assert result["questions"] == {"skills": [], "items": [], "missing": []}
    refs = [item["ref"] for section in result["composition"]["sections"] for item in section["items"]]
    assert "exp-2" not in refs and "proj-1" in refs
    skills = {skill["ref"]: skill["text"] for section in result["composition"]["sections"]
              for group in section["groups"] for skill in group["skills"]}
    assert skills[sentinel["id"]] == "Sentinel (notion)"

    replies.append(tailored(composition, questions=asked))
    second = tailoring.prepare(application["id"], kinds=["cv"])

    assert second["questions"]["skills"] == [] and second["questions"]["items"] == []
    refs = [item["ref"] for section in second["composition"]["sections"] for item in section["items"]]
    assert "exp-2" not in refs and "proj-1" in refs
    assert "ÉLÉMENTS DÉJÀ TRANCHÉS : exp-2" in calls[1]["prompt"]


def test_unknown_skill_answer_is_recorded_and_then_forbidden(assistant, application):
    _calls, replies = assistant
    replies.append(tailored(questions={"skills": [{"name": "Sentinel", "excerpt": ""}], "items": [], "missing": []}))
    session = tailoring.prepare(application["id"], kinds=["cv"])["session"]

    tailoring.answer(application["id"], {"session": session, "skills": [{"name": "Sentinel", "status": "inconnu"}]})

    saved = profile.load_profile()
    assert {"name": "Sentinel", "application_id": application["id"]}.items() <= saved["unknown_skills"][-1].items()
    composition = valid_composition()
    composition["summary"] = "Curieuse de Sentinel"
    with pytest.raises(tailoring.CompositionError, match="inconnue"):
        tailoring.validate_composition(composition, saved)


def test_user_edited_bullet_becomes_validated_variant_but_ai_rewrite_does_not(assistant, application):
    _calls, replies = assistant
    replies.append(tailored())
    first = tailoring.prepare(application["id"], kinds=["cv"])

    tailoring.answer(application["id"], {"session": first["session"], "composition": first["composition"]})
    assert len(profile.load_profile()["experiences"][0]["bullets"][0]["variants"]) == 1

    edited = deepcopy(first["composition"])
    edited["sections"][0]["items"][0]["bullets"][0]["text"] = "Qualification de 20 alertes de sécurité avec Splunk"
    tailoring.answer(application["id"], {"session": first["session"], "composition": edited})

    variants = profile.load_profile()["experiences"][0]["bullets"][0]["variants"]
    assert variants[-1]["text"] == "Qualification de 20 alertes de sécurité avec Splunk"
    assert variants[-1]["validated_at"] and variants[-1]["theme"] == "soc"
    assert variants[-1]["provenance"]["source"] == f"candidature:{application['id']}"


def test_invalid_edited_composition_is_rejected_and_nothing_is_saved(assistant, application):
    _calls, replies = assistant
    replies.append(tailored())
    first = tailoring.prepare(application["id"], kinds=["cv"])
    version = profile.load_profile()["version"]
    edited = deepcopy(first["composition"])
    edited["sections"][0]["items"][0]["bullets"][0]["text"] = "Analyse de 2000 alertes avec Splunk"

    with pytest.raises(tailoring.CompositionError, match="nombre absent"):
        tailoring.answer(application["id"], {"session": first["session"], "composition": edited,
                                             "skills": [{"name": "Sentinel", "status": "notion"}]})

    assert profile.load_profile()["version"] == version


def test_answers_reject_unknown_session_and_bad_values(assistant, application):
    with pytest.raises(tailoring.CompositionError, match="invalide"):
        tailoring.answer(application["id"], {"session": "../../etc/passwd"})
    with pytest.raises(FileNotFoundError):
        tailoring.answer(application["id"], {"session": "00000000-0000-4000-8000-000000000000"})
    _calls, replies = assistant
    replies.append(tailored())
    session = tailoring.prepare(application["id"], kinds=["cv"])["session"]
    with pytest.raises(tailoring.CompositionError, match="statut"):
        tailoring.answer(application["id"], {"session": session, "skills": [{"name": "X", "status": "bof"}]})
    with pytest.raises(tailoring.CompositionError, match="remplacement"):
        tailoring.answer(application["id"], {"session": session, "items": [
            {"item_id": "exp-2", "action": "ajouter_projet", "replacement_id": "exp-1"}]})
    with pytest.raises(tailoring.CompositionError, match="autre candidature"):
        tailoring.answer(application["id"] + 1, {"session": session})


# ---------------------------------------------------------------- ajout guidé de projet

def test_project_assistant_masks_identity_and_formats_only_given_facts(assistant):
    calls, replies = assistant
    replies += [{"questions": ["Quel outil ?", "Combien d'alertes ?"]},
                {"title": "Lab SOC", "setting": "perso", "start": "2025-02", "end": None,
                 "technologies": ["Wazuh"], "bullets": ["Détection de 30 alertes avec Wazuh"], "link": None}]
    context = {"titre": "Lab SOC d'Alice Martin à Paris", "contact": "alice@example.test"}

    questions = tailoring.assist_project({"context": context})
    answers = {"Quel outil ?": "Wazuh, en février 2025", "Combien d'alertes ?": "30"}
    result = tailoring.assist_project({"context": context, "answers": answers, "save": True, "application_id": 4})

    assert questions == {"questions": ["Quel outil ?", "Combien d'alertes ?"]}
    for call in calls:
        for secret in SECRETS[:3] + ("Paris",):
            assert secret not in call["prompt"]
    assert result["project"]["id"] == "proj-2"
    assert result["project"]["provenance"]["source"] == "candidature:4"
    assert profile.load_profile()["projects"][-1]["bullets"][0]["text"] == "Détection de 30 alertes avec Wazuh"


@pytest.mark.parametrize(("reply", "message"), [
    ({"bullets": ["Détection de 300 alertes"]}, "nombre absent"),
    ({"technologies": ["Splunk"]}, "technologie absente"),
    ({"bullets": ["Scripts Python"]}, "absent des réponses"),
    ({"start": "2019-01"}, "date absente"),
])
def test_project_assistant_rejects_invented_facts(assistant, reply, message):
    _calls, replies = assistant
    replies.append({"title": "Lab", "setting": "perso", "start": None, "end": None, "technologies": [],
                    "bullets": ["Détection d'alertes"], "link": None} | reply)
    with pytest.raises(tailoring.CompositionError, match=message):
        tailoring.assist_project({"answers": {"Quoi ?": "Détection d'alertes, 30 par jour"}})


def test_project_assistant_requires_two_or_three_questions(assistant):
    _calls, replies = assistant
    replies.append({"questions": ["Une seule ?"]})
    with pytest.raises(tailoring.CompositionError, match="deux ou trois"):
        tailoring.assist_project({"context": "Projet"})


# ---------------------------------------------------------------- schémas

def _walk(schema, path="racine"):
    yield path, schema
    for key, value in (schema.get("properties") or {}).items():
        yield from _walk(value, f"{path}.{key}")
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], f"{path}[]")
    for option in schema.get("anyOf") or []:
        yield from _walk(option, f"{path}|")


@pytest.mark.parametrize("schema", [profile.PROFILE_EXTRACTION_SCHEMA, tailoring.TAILORING_SCHEMA,
                                    tailoring.PROJECT_QUESTIONS_SCHEMA, tailoring.PROJECT_RESULT_SCHEMA])
def test_ai_schemas_are_compatible_with_codex_strict_mode(schema):
    for path, node in _walk(ai.strict_schema(schema)):
        assert not {"minItems", "maxItems", "minLength", "maxLength", "pattern", "format"} & set(node), path
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert sorted(node["required"]) == sorted(node["properties"]), path


@pytest.mark.parametrize(("title", "expected"), [
    ("Stage – Analyste CERT H/F", "Stage – Analyste CERT"),
    ("Stage - Ingénieur Cybersécurité (F/H) - Paris (6 mois)", "Stage – Ingénieur Cybersécurité"),
    ("Final Year Internship Consultant - Cybersecurity", "Final Year Internship Consultant – Cybersecurity"),
])
def test_titre_du_cv_deduit_de_l_intitule_de_l_offre(title: str, expected: str) -> None:
    assert tailoring.headline_from_offer(title) == expected
