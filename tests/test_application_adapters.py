"""Formulaires ATS enregistrés : aucune requête réseau pendant les tests."""
from pathlib import Path

import pytest

from jobradar import ai
from jobradar.application_adapters import Field, adapter_for, fill_plan, generic_ai_plan

FIXTURES = Path(__file__).parent / "fixtures" / "apply"


def _candidate() -> dict:
    return {"identity": {"full_name": "Alice Martin", "email": "alice@example.test",
                         "phone": "+33 6 00 00 00 00", "city": "Paris", "links": []}}


@pytest.mark.parametrize("provider", ["greenhouse", "lever", "smartrecruiters"])
def test_adapter_extracts_only_real_form_controls(provider: str) -> None:
    fields = adapter_for(provider).parse((FIXTURES / f"{provider}.html").read_text(encoding="utf-8"))
    assert fields
    assert all(field.name and field.selector.startswith('[name="') for field in fields)
    assert all(field.kind != "submit" for field in fields)
    assert any(field.kind == "file" and field.required for field in fields)


def test_greenhouse_plan_maps_identity_and_documents_but_stops_on_free_question() -> None:
    fields = adapter_for("greenhouse").parse((FIXTURES / "greenhouse.html").read_text(encoding="utf-8"))
    plan = fill_plan(fields, _candidate(), {"cv": "/safe/CV.pdf", "lettre": "/safe/Lettre.pdf"})

    assert {item["value"] for item in plan["values"]} >= {
        "Alice", "Martin", "alice@example.test", "+33600000000", "/safe/CV.pdf", "/safe/Lettre.pdf",
    }
    assert plan["ready"] is False
    assert plan["interventions"] == [{
        "selector": '[name="question_1"]', "label": "Pourquoi ce poste ? *",
        "kind": "missing_required", "input_kind": "textarea", "options": [],
        "message": "Information obligatoire absente ou ambiguë.",
    }]


def test_lever_plan_is_ready_when_all_required_fields_are_known() -> None:
    fields = adapter_for("lever").parse((FIXTURES / "lever.html").read_text(encoding="utf-8"))
    plan = fill_plan(fields, _candidate(), {"cv": "/safe/CV.pdf"})
    assert plan["ready"] is True and plan["interventions"] == []


def test_sensitive_required_question_always_requires_user_decision() -> None:
    fields = adapter_for("smartrecruiters").parse(
        (FIXTURES / "smartrecruiters.html").read_text(encoding="utf-8")
    )
    plan = fill_plan(fields, _candidate(), {"cv": "/safe/CV.pdf"})
    assert plan["ready"] is False
    assert plan["interventions"][0]["kind"] == "sensitive"
    assert "décision" in plan["interventions"][0]["message"]


def test_exact_saved_answer_is_reused_locally_even_on_dedicated_adapter() -> None:
    fields = adapter_for("workday").parse((FIXTURES / "workday.html").read_text(encoding="utf-8"))
    candidate = _candidate() | {"standard_answers": {
        "notice": {"question": "Délai de préavis", "answer": "Aucun"},
        "work": {"question": "Êtes-vous autorisé à travailler en France ?", "answer": "Oui"},
        "privacy": {"question": "J'accepte la politique de confidentialité", "answer": "Oui",
                    "sensitive": True},
    }}
    plan = fill_plan(fields, candidate, {"cv": "/safe/CV.pdf"})
    assert plan["ready"] is True
    assert {item["value"] for item in plan["values"]} >= {"Aucun", "Oui", True}


def test_unknown_or_empty_form_fails_closed() -> None:
    with pytest.raises(ValueError, match="introuvable"):
        adapter_for("generic").parse("<main>aucun formulaire</main>")


def test_workday_adapter_uses_stable_automation_form_and_stops_on_unknown_question() -> None:
    fields = adapter_for("workday").parse((FIXTURES / "workday.html").read_text(encoding="utf-8"))
    plan = fill_plan(fields, _candidate(), {"cv": "/safe/CV.pdf"})
    assert {item["value"] for item in plan["values"]} >= {"alice@example.test", "/safe/CV.pdf"}
    assert plan["ready"] is False
    assert [item["label"] for item in plan["interventions"]] == [
        "Délai de préavis", "Êtes-vous autorisé à travailler en France ?",
    ]
    # Le consentement obligatoire au traitement de la candidature est coché par la règle.
    assert any(item["label"] == "J'accepte la politique de confidentialité" and item["value"] is True
               for item in plan["values"])
    radio = next(field for field in fields if field.kind == "radio")
    assert radio.options == ("Oui", "Non")
    assert sum(field.kind == "radio" for field in fields) == 1
    with pytest.raises(ValueError, match="introuvable"):
        adapter_for("greenhouse").parse("<html><body>aucun formulaire</body></html>")


def test_generic_ai_can_only_reuse_an_exact_validated_answer(monkeypatch) -> None:
    plan = {"values": [], "ready": False, "interventions": [{
        "selector": '[name="availability"]', "label": "Quand pouvez-vous commencer ?",
        "kind": "missing_required", "input_kind": "text", "options": [],
    }]}
    candidate = {"standard_answers": {"disponibilite": {
        "question": "Disponibilité", "answer": "Septembre 2027",
    }}}
    monkeypatch.setattr(ai, "ask_ai", lambda *_args: {"answers": [{
        "selector": '[name="availability"]', "answer_key": "disponibilite", "confidence": 0.96,
    }]})

    result = generic_ai_plan(plan, candidate)

    assert result["ready"] is True
    assert result["values"][0]["value"] == "Septembre 2027"
    assert result["values"][0]["source"] == "reponse_type:disponibilite"


def test_generic_ai_never_receives_sensitive_saved_answers(monkeypatch) -> None:
    plan = {"values": [], "ready": False, "interventions": [{
        "selector": '[name="q"]', "label": "Question", "kind": "missing_required",
        "input_kind": "text", "options": [],
    }]}
    candidate = {"standard_answers": {
        "secret": {"question": "Handicap", "answer": "Oui", "sensitive": True},
        "available": {"question": "Disponibilité", "answer": "Septembre"},
    }}

    def inspect_prompt(prompt, *_args):
        assert "Handicap" not in prompt and '"Oui"' not in prompt
        assert "Disponibilité" in prompt
        return {"answers": []}

    monkeypatch.setattr(ai, "ask_ai", inspect_prompt)
    assert generic_ai_plan(plan, candidate)["ready"] is False


@pytest.mark.parametrize(("answer_key", "confidence"), [("inconnue", 0.99), ("disponibilite", 0.89)])
def test_generic_ai_rejects_unknown_or_low_confidence_answers(monkeypatch, answer_key, confidence) -> None:
    plan = {"values": [], "ready": False, "interventions": [{
        "selector": '[name="q"]', "label": "Question", "kind": "missing_required",
        "input_kind": "text", "options": [],
    }]}
    candidate = {"standard_answers": {"disponibilite": {"question": "Disponibilité", "answer": "Septembre"}}}
    monkeypatch.setattr(ai, "ask_ai", lambda *_args: {"answers": [{
        "selector": '[name="q"]', "answer_key": answer_key, "confidence": confidence,
    }]})
    result = generic_ai_plan(plan, candidate)
    assert result["ready"] is False and result["values"] == []


def test_hostile_control_name_is_escaped_as_a_css_string() -> None:
    fields = adapter_for("greenhouse").parse(
        '<form id="application_form"><label for="x">Email</label>'
        '<input id="x" name="mail&quot;] body{display:none}\n" required></form>'
    )
    assert len(fields) == 1
    assert fields[0].selector.startswith('[name="') and "\\\"" in fields[0].selector
    assert isinstance(fields[0], Field)


def test_formulaires_de_recherche_et_de_newsletter_ne_sont_pas_des_candidatures() -> None:
    from jobradar.application_adapters import NotApplicationForm
    page = """<form action="/recherche" method="get"><label for="k">Quoi ?</label><input id="k" name="k">
      <label for="l">Où ? Ville</label><input id="l" name="l"><button>Rechercher</button></form>
      <form action="/newsletter" method="post"><input type="email" name="email" aria-label="E-mail"></form>"""
    with pytest.raises(NotApplicationForm):
        adapter_for("generic").locate(page)


def test_le_formulaire_de_candidature_est_choisi_meme_s_il_n_est_pas_le_premier() -> None:
    page = """<form role="search" method="get"><input name="q"></form>
      <form action="/apply" method="post"><label for="n">Nom</label><input id="n" name="name">
      <label for="e">E-mail</label><input id="e" name="mail" type="email"></form>"""
    match = adapter_for("generic").locate(page)
    assert match.index == 1
    assert {field.name for field in match.fields} == {"name", "mail"}


def test_le_mot_nombre_ne_fait_pas_passer_un_formulaire_pour_une_candidature() -> None:
    from jobradar.application_adapters import NotApplicationForm
    page = """<form method="post"><label for="e">E-mail</label><input id="e" name="contact" type="email">
      <label for="c">Nombre de places</label><input id="c" name="places"></form>"""
    with pytest.raises(NotApplicationForm):
        adapter_for("generic").locate(page)


def test_lien_postuler_d_une_page_d_offre() -> None:
    from jobradar.application_adapters import find_apply_link
    page = """<a href="javascript:void(0)">Postuler</a><a href="/aide">Aide</a>
      <a href="https://acme.example/jobs/42/apply">Postuler sur le site du recruteur</a>"""
    assert find_apply_link(page) == "https://acme.example/jobs/42/apply"
    assert find_apply_link("<a href='/offres'>Voir les offres</a>") is None


def test_lien_d_action_est_prefere_a_l_aide_placee_avant() -> None:
    from jobradar.application_adapters import find_apply_link
    page = """<a href='/how-to-apply'>How to apply?</a>
      <a href='/global/en/apply?jobSeqNo=42'>Apply Now</a>"""
    assert find_apply_link(page) == "/global/en/apply?jobSeqNo=42"


def test_formulaire_sans_balise_form_est_confine_au_plus_petit_conteneur() -> None:
    page = """<main><div class='application'><label>Full name<input name='name'></label>
      <label>Email<input type='email' name='email'></label><input type='file' name='resume'></div></main>"""
    match = adapter_for("generic").locate(page)
    assert match.selector == "[data-jobradar-form]"
    assert {field.name for field in match.fields} == {"name", "email", "resume"}


def test_motivation_reprend_la_lettre_sans_appel_ia(monkeypatch) -> None:
    from jobradar import ai
    from jobradar.application_adapters import ai_plan, letter_excerpt
    monkeypatch.setattr(ai, "ask_ai", lambda *_a: pytest.fail("aucun appel IA attendu"))
    paragraphs = ["Élève ingénieur en cybersécurité, je souhaite rejoindre Acme pour ce stage SOC. "
                  "Mon projet MatrixRecon automatise l'analyse de vulnérabilités en Python.",
                  "J'ai aussi centralisé des journaux Linux et Windows dans un SIEM. " * 3]
    assert letter_excerpt(paragraphs, 200) == ("Élève ingénieur en cybersécurité, je souhaite rejoindre Acme pour "
                                               "ce stage SOC. Mon projet MatrixRecon automatise l'analyse de "
                                               "vulnérabilités en Python.")
    assert letter_excerpt(["Trop court."], 600) is None
    plan = {"values": [], "ready": False, "interventions": [
        {"selector": "#m", "label": "Motivation", "classification": "motivation", "maxlength": 400, "kind": "missing"}]}
    result = ai_plan(plan, {"identity": {}}, {"company": "Acme"}, "", "fr", {"letter_paragraphs": paragraphs})
    assert result["values"][0]["value"].startswith("Élève ingénieur") and len(result["values"][0]["value"]) <= 400


def test_profil_envoye_a_l_ia_sans_metadonnees() -> None:
    from jobradar.application_adapters import prompt_profile
    candidate = {"schema": 1, "sources": [{"id": "src-1"}], "skills": [
        {"name": "Linux", "level": "", "evidence": [], "id": "skill-1", "provenance": {"source": "src-1"}}]}
    assert prompt_profile(candidate) == {"skills": [{"name": "Linux"}]}


def test_champ_sans_name_vise_par_son_id() -> None:
    # Dépôt de CV VINCI : <input type="file" id="candidate_resume_remote_url"> sans attribut name.
    html = """<form method="post"><label for="candidate_email">E-mail</label>
    <input id="candidate_email" name="candidate[email]" type="email">
    <label for="candidate_resume_remote_url">Importer un CV</label>
    <input id="candidate_resume_remote_url" type="file" required></form>"""
    fields = {field.label: field for field in adapter_for("generic").parse(html)}
    assert fields["Importer un CV"].selector == '[id="candidate_resume_remote_url"]'
    assert fields["E-mail"].selector == '[name="candidate[email]"]'


def test_champ_lettre_de_motivation_recoit_la_lettre_entiere(monkeypatch) -> None:
    from jobradar.application_adapters import ai_plan
    monkeypatch.setattr(ai, "ask_ai", lambda *_a: pytest.fail("aucun appel IA attendu"))
    paragraphs = ["Madame, Monsieur,", "Élève ingénieur en cybersécurité, je souhaite rejoindre Acme.",
                  "Je vous prie d'agréer mes salutations."]
    field = {"selector": "#l", "label": "Lettre de motivation*", "classification": "motivation",
             "input_kind": "textarea", "maxlength": None, "kind": "missing_required"}
    result = ai_plan({"values": [], "ready": False, "interventions": [field]}, {"identity": {}}, {}, "", "fr",
                     {"letter_paragraphs": paragraphs})
    assert result["values"][0]["value"] == "\n\n".join(paragraphs)
    assert result["values"][0]["source"] == "document:lettre" and result["ready"]


def test_comment_nous_avez_vous_connus_prend_le_site_de_l_entreprise_puis_linkedin() -> None:
    from jobradar.application_adapters import source_answer
    assert source_answer(["Indeed", "Offre Linkedin", "Site HGH.com", "Autre"]) == "Site HGH.com"
    assert source_answer(["Post LinkedIn", "Offre LinkedIn", "Salon"]) == "Offre LinkedIn"
    assert source_answer(["Site d'emploi", "Recommandation", "Autre"]) == "Autre"
    assert source_answer(["Recommandation", "Salon de recrutement"]) is None

    group = "Comment vous nous avez connu? *"
    boxes = [Field(f"#c{index}", f"c{index}", label, "checkbox", False, group=group)
             for index, label in enumerate(["Offre Linkedin", "Site HGH.com", "Autre"])]
    select = Field("#how", "how", "How did you hear about us?", "select", True, ("Indeed", "LinkedIn", "Other"))
    text = Field("#src", "src", "Comment avez-vous connu cette offre ?", "text", True)
    plan = fill_plan([*boxes, select, text], _candidate(), {})
    values = {item["selector"]: item["value"] for item in plan["values"]}
    assert values == {"#c1": True, "#how": "LinkedIn", "#src": "LinkedIn"} and plan["ready"]


def test_question_sensible_ou_sans_option_sure_reste_pour_l_utilisateur() -> None:
    rqth = Field("#rqth", "rqth", "Êtes-vous reconnu travailleur handicapé ?", "radio", True, ("Oui", "Non"),
                 sensitive=True)
    boxes = [Field(f"#c{index}", f"c{index}", label, "checkbox", False, group="Comment nous avez-vous connus ?")
             for index, label in enumerate(["Recommandation", "Salon"])]
    plan = fill_plan([rqth, *boxes], _candidate(), {})
    assert plan["values"] == [] and plan["interventions"][0]["kind"] == "sensitive"


def test_nom_et_indicatif_ne_recoivent_pas_le_nom_complet_ni_le_numero() -> None:
    fields = [Field('[name="lastname"]', "lastname", "Nom *", "text", True),
              Field("#cc", "cc", "Phone number country", "select", False, ("France", "Belgique"))]
    plan = fill_plan(fields, _candidate(), {})
    assert [(item["selector"], item["value"]) for item in plan["values"]] == [('[name="lastname"]', "Martin")]


def test_deuxieme_prenom_reste_vide() -> None:
    fields = [Field("#p", "p", "Prénom : *", "text", True), Field("#m", "m", "Deuxième Prénom", "text", False)]
    plan = fill_plan(fields, _candidate(), {})
    assert [(item["selector"], item["value"]) for item in plan["values"]] == [("#p", "Alice")]


def test_libelle_source_seul_est_la_question_comment_nous_avez_vous_connus() -> None:
    field = Field("#s", "s", "Source *", "select", True, ("Indeed", "LinkedIn", "Thales Careers Site", "Other"))
    plan = fill_plan([field], _candidate(), {})
    assert plan["values"][0]["value"] == "Thales Careers Site"


def test_formulaire_thales_ni_salon_ni_nom_dans_la_rue_ni_case_cochee() -> None:
    from jobradar.application_adapters import source_answer
    assert source_answer(["Careers Fair", "Job Board", "Social Media", "University / Polytechnic", "Website"]) == "Website"
    fields = [Field("#street", "street", "Street name and type", "text", False),
              Field("#company", "company", "Company name", "text", False),
              Field("#name", "name", "Name", "text", True),
              Field("#news", "news", "I consent to receive update messages by email.", "checkbox", True)]
    plan = fill_plan(fields, _candidate(), {})
    assert [(item["selector"], item["value"]) for item in plan["values"]] == [("#name", "Alice Martin")]


def test_telephone_national_quand_l_indicatif_est_une_liste_a_part() -> None:
    from jobradar.application_adapters import national_phone
    assert national_phone("+33 7 63 95 67 80") == "0763956780"
    assert national_phone("0033 6 00 00 00 00") == "0600000000"
    phone = Field("#tel", "tel", "Phone number *", "text", True)
    code = Field("#cc", "cc", "Country Phone Code *", "select", True, ("France (+33)", "Belgique (+32)"))
    assert fill_plan([phone, code], _candidate(), {})["values"][0]["value"] == "0600000000"
    assert fill_plan([phone], _candidate(), {})["values"][0]["value"] == "+33600000000"


def test_consentement_obligatoire_coche_jamais_la_newsletter() -> None:
    fields = [Field("#p", "p", "* I have read the Privacy Notice and I agree", "checkbox", True),
              Field("#u", "u", "I consent to receive update messages by email.", "checkbox", True),
              Field("#f", "f", "Oui, les filiales du groupe peuvent consulter ma candidature", "checkbox", True),
              Field("#o", "o", "J'accepte la politique de confidentialité", "checkbox", False),
              Field("#n", "n", "I have read the Privacy Notice and I do not agree to the AI tool", "checkbox", True)]
    plan = fill_plan(fields, _candidate(), {})
    assert [(item["selector"], item["value"]) for item in plan["values"]] == [("#p", True)]


def test_telephone_national_quand_le_champ_est_trop_court() -> None:
    field = Field("#tel", "tel", "Téléphone", "tel", True, maxlength=10)
    assert fill_plan([field], _candidate(), {})["values"][0]["value"] == "0600000000"


def test_adresse_postale_code_postal_departement_et_ville_de_residence() -> None:
    candidate = _candidate()
    candidate["identity"] |= {"postal_code": "77100", "residence_city": "Meaux", "region": "Seine-et-Marne"}
    fields = [Field("#cp", "cp", "Postal Code * Postal Code", "text", True),
              Field("#state", "state", "state *", "select", True, ("Paris", "Seine-et-Marne")),
              Field("#city", "city", "City", "text", False),
              Field("#st", "st", "Personal statement", "text", False)]
    values = {item["selector"]: item["value"] for item in fill_plan(fields, candidate, {})["values"]}
    assert values == {"#cp": "77100", "#state": "Seine-et-Marne", "#city": "Meaux"}


def test_workday_indicatif_extension_et_recherche_deja_choisie_restent_intacts() -> None:
    fields = [Field("#cc", "cc", "Country Phone Code * Search 1 item selected, France (+33)", "text", True),
              Field("#tel", "tel", "Phone Number *", "text", True),
              Field("#ext", "ext", "Phone Extension", "text", False),
              Field("#src", "src", "How Did You Hear About Us? * Search 1 item selected, Web - Best Jobs", "text", True)]
    plan = fill_plan(fields, _candidate(), {})
    assert {item["selector"]: item["value"] for item in plan["values"]} == {"#tel": "0600000000"}
    assert plan["interventions"] == [] and plan["ready"]


def test_seul_depot_sans_libelle_recoit_le_cv() -> None:
    drop = Field("[data-jobradar-field=\"2\"]", "2", "Drop files here or Select files", "file", False)
    other = Field("#extra", "extra", "Fichiers supplémentaires", "file", False)
    assert fill_plan([drop], _candidate(), {"cv": "/safe/CV.pdf"})["values"][0]["value"] == "/safe/CV.pdf"
    assert fill_plan([drop, other], _candidate(), {"cv": "/safe/CV.pdf"})["values"] == []
