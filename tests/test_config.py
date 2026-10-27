"""Tests de la configuration : dossier de données déplaçable et réglages métier."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobradar import config


def test_dossier_par_defaut_du_projet() -> None:
    assert config.resolve_data_dir({}) == config.ROOT / "data"
    assert config.resolve_data_dir({"JOBRADAR_DATA_DIR": "  "}) == config.ROOT / "data"


def test_dossier_surcharge_par_variable_absolue(tmp_path: Path) -> None:
    assert config.resolve_data_dir({"JOBRADAR_DATA_DIR": str(tmp_path)}) == tmp_path


def test_dossier_relatif_refuse() -> None:
    with pytest.raises(RuntimeError, match="chemin absolu"):
        config.resolve_data_dir({"JOBRADAR_DATA_DIR": "donnees"})


def test_les_tests_ne_pointent_jamais_vers_la_base_reelle() -> None:
    assert os.environ["JOBRADAR_DATA_DIR"]
    assert config.resolve_data_dir() != config.ROOT / "data"


def test_tous_les_chemins_derivent_de_la_variable(tmp_path: Path) -> None:
    code = "from jobradar import config; print(config.DB_PATH); print(config.CONFIG_PATH); print(config.CLAUDE_CWD)"
    env = os.environ | {"JOBRADAR_DATA_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=config.ROOT, check=True).stdout.split()
    assert out == [str(tmp_path / "jobradar.db"), str(tmp_path / "config.json"), str(tmp_path / "claude_cwd")]


def test_reglages_par_defaut(data_dir: Path) -> None:
    st = config.settings()
    assert (st.timezone, st.follow_up_delay_days, st.deadline_urgent_days) == ("Europe/Paris", 10, 7)
    assert (st.alert_min_score, st.alert_desktop) == (85, True)
    written = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert written["alerts"] == {"min_score": 85, "desktop": True}
    assert config.browser_settings()["keep_open"] is True
    assert config.capture_profile_dir() != config.browser_profile_dir()


def test_reglages_invalides_remplaces_par_les_valeurs_par_defaut(write_config) -> None:
    write_config(timezone="Mars/Olympus", follow_up_delay_days=0, deadline_urgent_days="7",
                 alerts={"min_score": 140, "desktop": False})
    st = config.settings()
    assert (st.timezone, st.follow_up_delay_days, st.deadline_urgent_days) == ("Europe/Paris", 10, 7)
    assert st.alert_min_score == 85
    assert st.alert_desktop is False


def test_api_info_expose_les_reglages(client, write_config) -> None:
    write_config(timezone="America/Montreal", follow_up_delay_days=14, alerts={"min_score": 90})
    body = client.get("/api/info").json()
    assert body["timezone"] == "America/Montreal"
    assert body["follow_up_delay_days"] == 14
    assert body["alert_min_score"] == 90
    assert body["provider"] == "claude"


def test_api_permet_de_choisir_codex(client, write_config, monkeypatch) -> None:
    from jobradar import ai

    write_config(ai_provider="claude")
    monkeypatch.setattr(ai, "provider_availability", lambda _cfg=None: {"claude": True, "codex": True})

    response = client.put("/api/settings/ai-provider", json={"provider": "codex"})

    assert response.status_code == 200
    assert response.json()["label"] == "Codex"
    assert config.load_config()["ai_provider"] == "codex"


def test_analyse_des_criteres_utilise_codex_apres_selection(client, write_config, monkeypatch) -> None:
    from jobradar import ai

    appels = []

    def faux_codex(prompt: str, schema: dict, model: str, cfg: dict) -> dict:
        appels.append({"prompt": prompt, "schema": schema, "model": model, "provider": cfg["ai_provider"]})
        return {"name": "Recherche Codex"}

    write_config(ai_provider="claude")
    monkeypatch.setattr(ai, "provider_availability", lambda _cfg=None: {"claude": True, "codex": True})
    monkeypatch.setitem(ai.PROVIDERS, "codex", faux_codex)

    assert client.put("/api/settings/ai-provider", json={"provider": "codex"}).status_code == 200
    response = client.post("/api/analyze", json={"prompt": "stage cybersécurité", "form": {}})

    assert response.status_code == 200
    assert response.json()["name"] == "Recherche Codex"
    assert appels[0]["provider"] == "codex"
    assert "stage cybersécurité" in appels[0]["prompt"]


def test_api_refuse_un_assistant_absent(client, write_config, monkeypatch) -> None:
    from jobradar import ai

    write_config(ai_provider="claude")
    monkeypatch.setattr(ai, "provider_availability", lambda _cfg=None: {"claude": True, "codex": False})

    response = client.put("/api/settings/ai-provider", json={"provider": "codex"})

    assert response.status_code == 409
    assert config.load_config()["ai_provider"] == "claude"
