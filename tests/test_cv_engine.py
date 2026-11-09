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
