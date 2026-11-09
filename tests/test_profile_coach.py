import pytest

from jobradar import ai, profile, profile_coach, tailoring


@pytest.fixture
def coached(data_dir):
    data = profile.empty_profile()
    data["identity"]["full_name"] = "Camille Durand"
    data["projects"] = [
        {"id": "proj-1", "title": "MatrixRecon", "technologies": [], "bullets": [
            {"id": "proj-1-b1", "text": "Outil de reconnaissance."}]},
        {"id": "proj-2", "title": "MatrixRecon framework", "technologies": ["Python"], "bullets": [
            {"id": "proj-2-b1", "text": "Scanner modulaire en Python."}]},
        {"id": "proj-3", "title": "SIEM simplifié", "technologies": ["Elastic"], "bullets": [
            {"id": "proj-3-b1", "text": "Collecte de 3 sources de logs."},
            {"id": "proj-3-b2", "text": "Tableaux de bord Elastic."}]},
    ]
    data["skills"] = [{"id": "s1", "name": "Python", "category": "", "level": ""},
                      {"id": "s2", "name": "Kubernetes", "category": "", "level": ""}]
    profile.save_profile(data)
    return data


def test_review_finds_weak_items_duplicates_and_unproven_skills(coached) -> None:
    report = profile_coach.review(profile.load_profile())
    weak = {row["id"]: row["problems"] for row in report["items"]}
    assert set(weak["proj-1"]) == {"moins de deux puces", "aucun résultat chiffré", "aucun outil précisé"}
    assert "proj-3" not in weak
    assert report["duplicates"][0]["keep"] == "proj-1" and report["duplicates"][0]["drop"] == "proj-2"
    assert report["unproven_skills"] == ["Kubernetes"]


def test_enrich_adds_only_bullets_grounded_in_the_answers(coached, monkeypatch) -> None:
    prompts = []

    def fake(prompt, _schema, _task):
        prompts.append(prompt)
        return {"bullets": ["Automatisation de la reconnaissance de 40 cibles avec Nmap."], "technologies": ["Nmap"]}

    monkeypatch.setattr(ai, "ask_ai", fake)
    answers = {"role": "J'ai automatisé la reconnaissance", "tools": "Nmap", "result": "40 cibles par jour"}
    saved = profile_coach.enrich("proj-1", answers)
    item = next(item for item in saved["projects"] if item["id"] == "proj-1")
    assert item["bullets"][-1]["text"].startswith("Automatisation") and item["bullets"][-1]["provenance"]["source"] == "utilisateur"
    assert item["technologies"] == ["Nmap"] and "Camille" not in prompts[0]

    monkeypatch.setattr(ai, "ask_ai", lambda *_a: {"bullets": ["Gain de 90 % sur les audits."], "technologies": []})
    with pytest.raises(tailoring.CompositionError, match="nombre absent"):
        profile_coach.enrich("proj-1", {"result": "un gain notable"})


def test_merge_and_link_skills(coached) -> None:
    merged = profile_coach.merge("proj-1", "proj-2")
    keep = next(item for item in merged["projects"] if item["id"] == "proj-1")
    assert [item["id"] for item in merged["projects"]] == ["proj-1", "proj-3"]
    assert {bullet["id"] for bullet in keep["bullets"]} == {"proj-1-b1", "proj-2-b1"} and keep["technologies"] == ["Python"]
    data = profile.load_profile()
    assert profile_coach.link_skills(data) >= 1
    python = next(skill for skill in data["skills"] if skill["name"] == "Python")
    assert python["evidence"] == ["proj-1"]
