"""Contrat aveugle : aucune donnée privée du candidat n'apparaît dans un prompt envoyé à l'IA.

Le profil porte des valeurs sentinelles uniques ; chaque tâche IA du parcours de candidature est exécutée avec un
espion à la place de l'assistant, puis chaque prompt capturé est fouillé.
"""
from __future__ import annotations

import pytest

from jobradar import ai, application_adapters, application_supervisor, profile, tailoring

SENTINELS = {
    "nom": "Zorglub Sentinelle", "email": "zorglub.sentinelle@example.org", "telephone": "+33 6 98 76 54 32",
    "adresse": "77 rue Sentinelle", "code_postal": "99777", "salaire": "SALAIRE-SENTINELLE-48K",
    "disponibilite": "DISPO-SENTINELLE", "mot_de_passe": "MDP-SENTINELLE", "chemin": "/home/sentinelle/cv.pdf",
}
CANDIDATE = {
    "identity": {"full_name": SENTINELS["nom"], "email": SENTINELS["email"], "phone": SENTINELS["telephone"],
                 "address_line1": SENTINELS["adresse"], "postal_code": SENTINELS["code_postal"], "city": "Meaux",
                 "links": [{"label": "LinkedIn", "url": "https://www.linkedin.com/in/zorglub-sentinelle"}]},
    "headline": "Élève ingénieur en cybersécurité",
    "summary": "Élève ingénieur en cybersécurité passionné de sécurité offensive.",
    "experiences": [{"id": "e1", "title": "Stagiaire SOC", "organization": "Acme", "start": "2025-06",
                     "end": "2025-08", "bullets": [{"id": "b1", "text": "Analyse d'alertes SIEM sous Splunk."}]}],
    "projects": [], "education": [{"school": "3IL", "degree": "Ingénieur cybersécurité", "end": "2027"}],
    "languages": [{"name": "Anglais", "level": "C1"}], "skills": [{"name": "Splunk"}],
    "standard_answers": {
        "salaire": {"question": "Prétentions salariales", "answer": SENTINELS["salaire"]},
        "dispo": {"question": "Date de disponibilité", "answer": SENTINELS["disponibilite"]},
    },
}
OFFER = {"title": "Stage analyste SOC", "company": "Banque Exemple", "description": "SOC, SIEM, Splunk."}


@pytest.fixture
def prompts(monkeypatch) -> list[str]:
    captured: list[str] = []

    def spy(prompt, schema, _task):
        captured.append(prompt)
        if schema is application_supervisor.FACTS_SCHEMA:
            return {"facts": []}
        if schema is application_adapters.MOTIVATION_SCHEMA:
            return {"message": "Votre stage SOC chez Banque Exemple correspond à mon expérience Splunk.",
                    "claims": []}
        if schema is application_adapters.FACTUAL_SCHEMA:
            return {"answers": []}
        return {"page_kind": "application_form", "understanding": "", "required_information": [], "actions": [],
                "confidence": 0.5}

    monkeypatch.setattr(ai, "ask_ai", spy)
    return captured


def _assert_blind(prompts: list[str]) -> None:
    assert prompts, "aucun appel IA capturé"
    for prompt in prompts:
        for name, value in SENTINELS.items():
            assert value.casefold() not in prompt.casefold(), f"{name} présent dans un prompt"
        assert "98 76 54" not in prompt and "zorglub" not in prompt.casefold()


def test_supervision_snapshot_and_catalog_are_blind(prompts) -> None:
    values = application_supervisor.derived_values(application_supervisor.profile_values(CANDIDATE))
    snap = {"url": "https://ats.example.com/apply", "title": "Candidature", "text": "Postuler", "hash": "x",
            "elements": [{"id": 1, "tag": "select", "type": "", "role": "", "label": "Pays", "name": "pays",
                          "text": "", "options": [f"Pays {i}" for i in range(60)] + ["France"]}]}
    application_supervisor.decide(snap, offer=OFFER, values=values)
    _assert_blind(prompts)


def test_cv_facts_are_read_without_sending_contacts(prompts, tmp_path) -> None:
    cv = tmp_path / "cv.txt"
    cv.write_text(f"{SENTINELS['nom']}\n{SENTINELS['adresse']}, {SENTINELS['code_postal']} Meaux\n"
                  f"{SENTINELS['email']} · {SENTINELS['telephone']}\nFormation : 3IL — Ingénieur cybersécurité",
                  encoding="utf-8")
    facts = application_supervisor.document_facts({"cv": str(cv)}, None, CANDIDATE["identity"])
    _assert_blind(prompts)
    assert facts["email"] == SENTINELS["email"]  # relevé en local


def test_motivation_message_is_blind(prompts) -> None:
    try:
        application_adapters.motivation_message(CANDIDATE, OFFER, "Lettre : je suis motivé par le SOC.", "fr", 600)
    except ValueError:
        pass  # le contenu simulé peut être refusé : seul le prompt compte ici
    _assert_blind(prompts)


def test_factual_questions_use_stored_answers_locally(prompts) -> None:
    plan = {"values": [], "ready": False, "interventions": [
        {"selector": "#dispo", "label": "Date de disponibilité *", "classification": "factual",
         "input_kind": "text", "kind": "text"},
        {"selector": "#ecole", "label": "École actuelle *", "classification": "factual", "input_kind": "text",
         "kind": "text"}]}
    result = application_adapters.ai_plan(plan, CANDIDATE, OFFER)
    _assert_blind(prompts)
    answered = {item["selector"]: item for item in result["values"]}
    assert answered["#dispo"]["value"] == SENTINELS["disponibilite"]
    assert answered["#dispo"]["source"] == "profil:reponse_type"


def test_tailoring_profile_view_is_blind() -> None:
    prompt = tailoring.build_prompt(profile.empty_profile() | CANDIDATE, OFFER, kinds=["cv"], cv_base=None,
                                    letter_base=None, cv_language="fr", letter_language="fr", family="soc")
    _assert_blind([prompt])
    assert "Splunk" in prompt  # le contenu professionnel, lui, est transmis


def test_profile_for_ai_drops_identity_and_answers() -> None:
    view = profile.for_ai(CANDIDATE)
    assert "identity" not in view and "standard_answers" not in view


@pytest.mark.parametrize(("label", "kind", "options", "expected"), [
    ("When can you start?", "text", [], "DISPO-SENTINELLE"),
    ("Are you legally authorized to work in France?", "select", ["Yes", "No"], "Yes"),
    ("Will you now or in the future require visa sponsorship?", "radio", ["Yes", "No"], "No"),
    ("Votre convention de stage est-elle possible ?", "select", ["Oui", "Non"], "Oui"),
    ("Quelle est votre couleur préférée ?", "text", [], None),
])
def test_stored_answers_are_found_whatever_the_wording(label, kind, options, expected) -> None:
    candidate = CANDIDATE | {"standard_answers": {
        "availability": {"question": "Date de disponibilité", "answer": "DISPO-SENTINELLE"},
        "work_authorization": {"question": "Autorisation de travail", "answer": "Oui"},
        "sponsorship": {"question": "Besoin d'un visa", "answer": "Non"},
        "internship_agreement": {"question": "Convention de stage possible", "answer": "Oui"}}}
    field = {"label": label, "input_kind": kind, "options": options}
    assert application_adapters.standard_answer_for(field, candidate) == expected


def test_stored_answers_survive_an_unavailable_ai(monkeypatch) -> None:
    def down(*_args):
        raise ai.QuotaError("quota épuisé")

    monkeypatch.setattr(ai, "ask_ai", down)
    plan = {"values": [], "ready": False, "interventions": [
        {"selector": "#dispo", "label": "Start date", "classification": "factual", "input_kind": "text"},
        {"selector": "#autre", "label": "Projet préféré", "classification": "factual", "input_kind": "text"}]}
    candidate = CANDIDATE | {"standard_answers": {"availability": {"question": "Date de disponibilité",
                                                                    "answer": "Janvier 2027"}}}
    result = application_adapters.ai_plan(plan, candidate, OFFER)
    assert [item["selector"] for item in result["values"]] == ["#dispo"]
    assert [item["selector"] for item in result["interventions"]] == ["#autre"]
