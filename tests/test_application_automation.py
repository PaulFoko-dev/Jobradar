"""Contrats de l'auto-candidature autonome (IA, navigateur du système et connexions)."""
from __future__ import annotations

import pytest

from jobradar import application_adapters as adapters
from jobradar import application_browser, application_queue, config, login_credentials, profile


def _candidate():
    return {"identity": {"full_name": "Alice Martin"}, "skills": [{"name": "Python"}],
            "standard_answers": {"notice": {"question": "Préavis", "answer": "Un mois"}}}


def test_classification_and_maxlength_apply_to_every_adapter() -> None:
    html = """<form id="application_form" method="post"><input name="email" type="email" required>
    <input name="name" required><textarea name="why_us" maxlength="420" required
    aria-label="Why do you want to join us?"></textarea><input name="notice" aria-label="Préavis" required>
    <textarea name="gender_story" aria-label="Genre" required></textarea></form>"""
    for provider in ("greenhouse", "lever", "smartrecruiters", "workday", "generic"):
        source = html
        if provider == "lever":
            source = html.replace("<form ", '<form class="application-form" ')
        if provider == "smartrecruiters":
            source = html.replace('id="application_form"', 'id="st-apply"')
        fields = adapters.adapter_for(provider).parse(source)
        by_name = {field.name: field for field in fields}
        assert by_name["why_us"].classification == "motivation"
        assert by_name["why_us"].maxlength == 420
        assert by_name["notice"].classification == "factual"
        assert by_name["gender_story"].sensitive is True
        assert by_name["gender_story"].classification is None


def _motivation_result(source="Python", message=None):
    message = message or ("Je souhaite rejoindre Acme au poste de Développeuse Python. "
                          "Ma pratique de Python correspond aux missions proposées. "
                          "Je serais heureuse de contribuer avec sérieux à votre équipe.")
    return {"message": message, "claims": [{"claim": "pratique de Python", "profile_source": source}]}


def test_motivation_validates_bounds_sources_language_and_cache(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda prompt, schema, task: calls.append(prompt) or _motivation_result())
    pending = {"selector": "#why", "label": "Pourquoi nous rejoindre ?", "kind": "missing_required",
               "input_kind": "textarea", "options": [], "maxlength": 300, "classification": "motivation"}
    cache = {}
    first = adapters.ai_plan({"values": [], "interventions": [pending], "ready": False}, _candidate(),
                             {"title": "Développeuse Python", "company": "Acme"}, language="fr", motivation_cache=cache)
    second = adapters.ai_plan({"values": [], "interventions": [pending], "ready": False}, _candidate(),
                              {"title": "Développeuse Python", "company": "Acme"}, language="fr", motivation_cache=cache)
    assert first["ready"] and second["ready"] and len(calls) == 1
    assert '"language": "fr"' in calls[0]
    assert first["values"][0]["source"] == "ia:motivation"


def test_motivation_rejects_unsourced_claim_and_bad_length(monkeypatch) -> None:
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: _motivation_result("fait absent"))
    with pytest.raises(ValueError, match="élément du profil"):
        adapters.motivation_message(_candidate(), {"title": "Développeuse Python", "company": "Acme"}, "", "fr", 300)
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: _motivation_result(message="Acme. Poste. Oui."))
    with pytest.raises(ValueError, match="3 à 5 phrases"):
        adapters.motivation_message(_candidate(), {"title": "Poste", "company": "Acme"}, "", "fr", 300)
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: _motivation_result() | {"claims": []})
    with pytest.raises(ValueError, match="élément du profil"):
        adapters.motivation_message(_candidate(), {"title": "Développeuse Python", "company": "Acme"}, "", "fr", 300)


def test_motivation_rejects_invented_number_and_retries_once_with_reason(monkeypatch) -> None:
    invented = ("Je souhaite rejoindre Acme au poste de Développeuse Python. "
                "Mes 5 ans de pratique de Python correspondent aux missions proposées. "
                "Je serais heureuse de contribuer avec sérieux à votre équipe.")
    answers = [_motivation_result(message=invented), _motivation_result()]
    prompts = []
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda prompt, *args: prompts.append(prompt) or answers.pop(0))
    message = adapters.motivation_message(_candidate(), {"title": "Développeuse Python", "company": "Acme"},
                                          "", "fr", 300)
    assert "5 ans" not in message and len(prompts) == 2 and "nombre absent" in prompts[1]


def test_motivation_accepts_real_world_offer_title(monkeypatch) -> None:
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: _motivation_result())
    offer = {"title": "Stage - Développeuse Python H/F (6 mois)", "company": "Acme SAS"}
    assert adapters.motivation_message(_candidate(), offer, "", "fr", 300).startswith("Je souhaite")


def test_factual_answer_requires_confidence_source_and_existing_option(monkeypatch) -> None:
    pending = {"selector": "#notice", "label": "Préavis", "kind": "missing_required", "input_kind": "select",
               "options": ["Un mois", "Deux mois"], "classification": "factual", "maxlength": None}
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: {"answers": [{"selector": "#notice",
        "answer": "Un mois", "confidence": .95, "profile_source": "Un mois"}]})
    result = adapters.ai_plan({"values": [], "interventions": [pending], "ready": False}, _candidate(), {})
    assert result["ready"] and result["values"][0]["source"] == "ia:profil"
    monkeypatch.setattr(adapters.ai, "ask_ai", lambda *args: {"answers": [{"selector": "#notice",
        "answer": "Trois mois", "confidence": .99, "profile_source": "Un mois"}]})
    assert not adapters.ai_plan({"values": [], "interventions": [pending], "ready": False}, _candidate(), {})["ready"]


def test_memory_ignores_sensitive_and_existing_answers(data_dir) -> None:
    current = profile.empty_profile()
    current["standard_answers"] = {"old": {"question": "Préavis", "answer": "Deux mois"}}
    profile.save_profile(current)
    profile.remember_form_answers([{"label": "Préavis", "value": "Un mois"},
                                   {"label": "Handicap", "value": "Non", "sensitive": True}])
    saved = profile.load_profile()["standard_answers"]
    assert len(saved) == 1 and saved["old"]["answer"] == "Deux mois"


def test_credentials_match_only_exact_host_or_subdomain(monkeypatch) -> None:
    index = {"example.com": {"username": "alice", "password": "secret", "backend": "file"},
             "acme.wd3.myworkdayjobs.com": {"username": "bob", "password": "tenant", "backend": "file"}}
    monkeypatch.setattr(login_credentials, "_index", lambda: index)
    assert login_credentials.for_host("careers.example.com") == ("alice", "secret")
    assert login_credentials.for_host("evil-example.com") is None
    assert login_credentials.for_host("acme.wd3.myworkdayjobs.com") == ("bob", "tenant")
    # Un autre client du même hébergeur ne reçoit jamais l'identifiant.
    assert login_credentials.for_host("rival.wd5.myworkdayjobs.com") is None


def test_login_api_never_returns_password(client) -> None:
    response = client.post("/api/settings/logins", json={
        "domain": "login.example.com", "username": "alice", "password": "top-secret"})
    assert response.status_code == 200
    view = client.get("/api/settings/browser")
    assert view.status_code == 200
    assert "top-secret" not in view.text
    assert view.json()["accounts"] == [{"domain": "login.example.com", "username": "alice",
                                         "password_set": True}]
    assert client.delete("/api/settings/logins/login.example.com").status_code == 200


def test_system_context_connects_without_closing_user_browser(monkeypatch, tmp_path) -> None:
    class Browser:
        contexts = [object()]
    class Chromium:
        def connect_over_cdp(self, endpoint):
            assert endpoint.endswith(":9444")
            return Browser()
    monkeypatch.setattr(config, "browser_settings", lambda: {"engine": "system", "executable": "", "cdp_port": 9444})
    monkeypatch.setattr(config, "find_system_browser", lambda value: {"key": "brave", "name": "Brave", "path": "/brave"})
    monkeypatch.setattr(application_browser, "_port_open", lambda port: True)
    with application_browser._browser_context(type("P", (), {"chromium": Chromium()})()) as context:
        assert context is Browser.contexts[0]


def test_open_browser_button_starts_system_browser_once_and_reports_missing_one(client, monkeypatch, tmp_path) -> None:
    started = []
    monkeypatch.setattr(config, "browser_settings", lambda: {"engine": "system", "executable": "", "cdp_port": 9444})
    monkeypatch.setattr(config, "system_profile_root", lambda: tmp_path)
    monkeypatch.setattr(config, "find_system_browser",
                        lambda value: {"key": "chrome", "name": "Google Chrome", "path": "/chrome"})
    monkeypatch.setattr(application_browser.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(application_browser, "_port_open", lambda port: bool(started))
    assert client.post("/api/settings/browser/open").json() == {"ok": True, "name": "Google Chrome"}
    assert client.post("/api/settings/browser/open").status_code == 200
    assert len(started) == 1 and started[0][0] == "/chrome"
    assert f"--user-data-dir={tmp_path / 'chrome'}" in started[0] and "--remote-debugging-port=9444" in started[0]
    monkeypatch.setattr(config, "find_system_browser", lambda value: None)
    monkeypatch.setattr(application_browser, "sync_playwright", _no_bundled_chromium)
    response = client.post("/api/settings/browser/open")
    assert response.status_code == 409 and "Aucun navigateur compatible" in response.json()["detail"]


def _no_bundled_chromium():
    class Started:
        chromium = type("C", (), {"executable_path": "/introuvable/chrome"})()
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    return Started()


@pytest.mark.parametrize(("raw", "expected"), [
    ("brave_brave.desktop", "brave"), ("google-chrome.desktop", "chrome"), ("MSEdgeHTM", "edge"),
    ("ChromeHTML", "chrome"), ("com.microsoft.edgemac", "edge"), ("com.google.chrome", "chrome"),
    ("FirefoxURL-308046B0AF4A39CB", "firefox"), ("com.apple.safari", "safari"), ("", None)])
def test_default_browser_is_recognized_on_every_system(monkeypatch, raw, expected) -> None:
    monkeypatch.setattr(config, "_default_browser_id", lambda: raw)
    assert config.default_browser() == expected


def test_firefox_default_falls_back_to_installed_chromium_browser(monkeypatch) -> None:
    monkeypatch.setattr(config, "default_browser", lambda: "firefox")
    monkeypatch.setattr(config, "_installed", lambda key: "/usr/bin/microsoft-edge" if key == "edge" else None)
    assert config.find_system_browser() == {"key": "edge", "name": "Microsoft Edge",
                                            "path": "/usr/bin/microsoft-edge", "is_default": False}
    monkeypatch.setattr(config, "default_browser", lambda: "brave")
    monkeypatch.setattr(config, "_installed", lambda key: f"/bin/{key}")
    assert config.find_system_browser()["key"] == "brave"


def test_legacy_brave_engine_becomes_system_browser() -> None:
    assert config.browser_settings({"browser": {"engine": "brave"}})["engine"] == "system"
    assert config.browser_settings({"browser": {}})["engine"] == "system"


def test_login_is_attempted_once_by_session_logic(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(application_browser.login_credentials, "for_host", lambda host: calls.append(host) or None)
    class Locator:
        def count(self): return 1
    class Page:
        url = "https://login.example.com/signin"
        def locator(self, selector): return Locator()
    assert application_browser._try_login(Page()) is False
    assert calls == ["login.example.com"]


def test_browser_unavailable_gets_three_retries_only_before_submit(monkeypatch) -> None:
    assert application_queue._may_retry_browser({"intervention": None, "submit_clicked_at_utc": None})
    assert application_queue._may_retry_browser(
        {"intervention": {"browser_retry_count": 1}, "submit_clicked_at_utc": None})
    assert application_queue._may_retry_browser(
        {"intervention": {"browser_retry_count": 2}, "submit_clicked_at_utc": None})
    assert not application_queue._may_retry_browser(
        {"intervention": {"browser_retry_count": 3}, "submit_clicked_at_utc": None})
    assert not application_queue._may_retry_browser(
        {"intervention": None, "submit_clicked_at_utc": "2026-10-20T08:00:00Z"})
