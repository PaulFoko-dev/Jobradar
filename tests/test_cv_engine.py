from datetime import date

from jobradar import cv_engine, profile, tailoring

PROFILE = {
    "experiences": [
        {"id": "exp-dev", "title": "Développeur mobile", "organization": "Studio", "start": "2022", "end": "2022",
         "technologies": ["Flutter"], "bullets": [{"id": "b-dev", "text": "Application de réservation en Flutter."}]},
        {"id": "exp-soc", "title": "Stagiaire analyste SOC", "organization": "Banque", "start": "2025", "end": "2025",
         "technologies": ["Splunk"], "bullets": [
             {"id": "b-soc-1", "text": "Création de 12 règles de détection Splunk pour le SIEM."},
             {"id": "b-soc-2", "text": "Rédaction de comptes rendus hebdomadaires."}]},
    ],
    "projects": [
        {"id": "proj-recon", "title": "Automatisation de la reconnaissance", "setting": "perso", "end": "2026",
         "technologies": ["Python", "Nmap"], "bullets": [{"id": "b-r", "text": "Scanner de vulnérabilités en Python."}]},
        {"id": "proj-jeu", "title": "Jeu de plateau", "setting": "école", "end": "2021", "technologies": ["Java"],
         "bullets": [{"id": "b-j", "text": "Interface graphique Swing."}]},
    ],
    "certifications": [], "skills": [{"id": "s1", "name": "Splunk"}, {"id": "s2", "name": "Java"},
                                     {"id": "s3", "name": "Python"}],
}
OFFER = {"title": "Stage analyste SOC H/F", "description": "Surveillance SIEM, détection d'incidents, scripts Python.",
         "analysis": {"cv_keywords": ["SIEM", "Splunk", "règles de détection"], "missions": ["Créer des règles de détection"]}}


def test_items_are_ranked_by_relevance_with_reasons() -> None:
    ranking = cv_engine.rank(PROFILE, OFFER, today=date(2026, 11, 9))

    assert [item.id for item in ranking["experiences"]] == ["exp-soc", "exp-dev"]
    assert [item.id for item in ranking["projects"]] == ["proj-recon", "proj-jeu"]
    soc = ranking["experiences"][0]
    assert {"splunk", "siem"} <= set(soc.matched)
    assert soc.bullets[0][0] == "b-soc-1"  # la puce chiffrée et ciblée passe devant
    assert [skill.label for skill in ranking["skills"] if skill.score > 0] == ["Splunk", "Python"]


def test_prompt_section_guides_selection_without_private_data() -> None:
    section = cv_engine.prompt_section(cv_engine.rank(PROFILE, OFFER, today=date(2026, 11, 9)))
    assert section.index("exp-soc") < section.index("exp-dev")
    assert "b-soc-1" in section and "Splunk" in section


def test_missed_top_items_and_explanation() -> None:
    ranking = cv_engine.rank(PROFILE, OFFER, today=date(2026, 11, 9))
    composition = {"sections": [{"type": "experience", "items": [{"ref": "exp-dev"}]},
                                {"type": "project", "items": [{"ref": "proj-recon"}]}]}
    assert [item.id for item in cv_engine.missed_top_items(composition, ranking)] == ["exp-soc"]
    rows = {row["id"]: row for row in cv_engine.explanation(composition, ranking)}
    assert rows["exp-soc"]["rank"] == 1 and not rows["exp-soc"]["kept"] and rows["proj-recon"]["kept"]


def test_tailoring_prompt_contains_the_ranking() -> None:
    data = profile.empty_profile() | PROFILE
    prompt = tailoring.build_prompt(data, OFFER, kinds=["cv"], cv_base=None, letter_base=None, cv_language="fr",
                                    letter_language="fr", family="soc")
    assert "CLASSEMENT DE PERTINENCE" in prompt and prompt.index("exp-soc —") < prompt.index("exp-dev —")


def test_cv_content_needs_consent_once_the_vault_exists(data_dir, monkeypatch) -> None:
    import base64
    import os

    import pytest

    from jobradar import vault

    monkeypatch.setenv(vault.ENV_KEY, base64.urlsafe_b64encode(os.urandom(32)).decode())
    vault.lock()
    vault.init()
    data = profile.empty_profile() | PROFILE
    arguments = {"kinds": ["cv"], "cv_base": None, "letter_base": None, "cv_language": "fr",
                 "letter_language": "fr", "family": "soc"}
    with pytest.raises(tailoring.CompositionError, match="consentement"):
        tailoring.build_prompt(data, OFFER, **arguments)
    vault.set_cv_consent(True)
    assert "CLASSEMENT DE PERTINENCE" in tailoring.build_prompt(data, OFFER, **arguments)
    vault.lock()


def test_bullets_are_tied_to_their_evidence() -> None:
    composition = {"sections": [{"type": "experience", "items": [{"ref": "exp-soc", "bullets": [
        {"ref": "b-soc-1", "text": "Création de 12 règles de détection Splunk pour le SIEM.", "why": ""},
        {"ref": "b-soc-2", "text": "Rédaction de comptes rendus hebdomadaires sur les incidents SIEM.",
         "why": "Met en avant le suivi SIEM demandé."}]}]}]}
    kept, reformulated = cv_engine.annotate_bullets(composition, PROFILE, OFFER)
    assert not kept["reformulated"] and kept["numbers"] == ["12"]
    assert reformulated["reformulated"] and reformulated["item"] == "exp-soc"
    assert reformulated["source_text"] == "Rédaction de comptes rendus hebdomadaires."
    assert "siem" in reformulated["highlighted_terms"]
    assert reformulated["why"] == "Met en avant le suivi SIEM demandé."
    assert all(set(bullet) == {"ref", "text"} for bullet in composition["sections"][0]["items"][0]["bullets"])


def test_rejected_reformulation_gets_one_motivated_retry(monkeypatch) -> None:
    from jobradar import ai

    data = profile.empty_profile() | PROFILE
    ctx = tailoring._context(data, ["Splunk"])
    source = PROFILE["experiences"][1]
    ctx.rejected = [{"ref": "b-soc-2", "source": source, "proposed": "Rédaction de 50 rapports Splunk.",
                     "reason": "nombre absent"}]
    composition = {"sections": [{"type": "experience", "items": [{"ref": "exp-soc", "bullets": [
        {"ref": "b-soc-2", "text": "Rédaction de comptes rendus hebdomadaires."}]}]}]}
    prompts = []

    def retry(prompt, _schema, _task):
        prompts.append(prompt)
        return {"bullets": [{"ref": "b-soc-2", "text": "Rédaction de comptes rendus hebdomadaires d'analyse.",
                             "why": "Communication des résultats."}]}

    monkeypatch.setattr(ai, "ask_ai", retry)
    notes = tailoring._retry_rejected(composition, ctx, OFFER)
    bullet = composition["sections"][0]["items"][0]["bullets"][0]
    assert "nombre absent" in prompts[0] and notes
    assert bullet["text"] == "Rédaction de comptes rendus hebdomadaires d'analyse."
    # Une seconde proposition encore inventée laisse le texte du profil.
    ctx.rejected = [{"ref": "b-soc-2", "source": source, "proposed": "x", "reason": "nombre absent"}]
    composition["sections"][0]["items"][0]["bullets"][0] = {"ref": "b-soc-2", "text": "Rédaction de comptes rendus hebdomadaires."}
    monkeypatch.setattr(ai, "ask_ai", lambda *_a: {"bullets": [{"ref": "b-soc-2", "text": "Rédaction de 99 rapports.",
                                                                "why": ""}]})
    tailoring._retry_rejected(composition, ctx, OFFER)
    assert composition["sections"][0]["items"][0]["bullets"][0]["text"] == "Rédaction de comptes rendus hebdomadaires."
