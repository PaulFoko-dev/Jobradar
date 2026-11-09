from jobradar import playbooks


def test_site_memory_fills_known_fields_without_values_on_disk(data_dir) -> None:
    url = "https://acme.wd3.myworkdayjobs.com/fr/job/123/apply"
    assert playbooks.remember(url, {"Adresse électronique *": "email", "Ville de résidence": "city"}) == 2
    stored = (data_dir / "profil" / "parcours.json").read_text()
    assert "camille@example.test" not in stored and "acme.wd3.myworkdayjobs.com" in stored
    plan = {"values": [], "ready": False, "interventions": [
        {"selector": "#mail", "label": "Adresse électronique *", "input_kind": "email", "kind": "missing_required"},
        {"selector": "#pays", "label": "Pays", "input_kind": "select", "kind": "missing_required"},
        {"selector": "#santé", "label": "Ville de résidence", "input_kind": "text", "kind": "sensitive"}]}
    filled, labels = playbooks.apply(url.replace("/123/", "/456/"), plan, {"email": "camille@example.test", "city": "Meaux"})
    assert labels == ["Adresse électronique *"]
    assert filled["values"][0] == {"selector": "#mail", "value": "camille@example.test", "kind": "email",
                                   "confidence": 1.0, "source": "parcours:email", "label": "Adresse électronique *"}
    assert [item["selector"] for item in filled["interventions"]] == ["#pays", "#santé"]
    assert playbooks.apply("https://autre.example.com/apply", plan, {"email": "x"})[1] == []
