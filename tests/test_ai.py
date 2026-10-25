"""Tests des adaptateurs IA, sans lancer Claude Code ni Codex."""

import json
import subprocess
from pathlib import Path

import pytest

from jobradar import ai


def test_strict_schema_est_recursif_et_ne_modifie_pas_la_source() -> None:
    source = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"nom": {"type": "string"}}},
            },
        },
    }

    resultat = ai.strict_schema(source)

    assert resultat["additionalProperties"] is False
    assert "minimum" not in resultat["properties"]["score"]
    assert resultat["properties"]["items"]["items"]["additionalProperties"] is False
    assert source["properties"]["score"]["minimum"] == 0


@pytest.mark.parametrize("message", ["Rate limit reached", "HTTP 429", "Quota exceeded"])
def test_fail_identifie_les_erreurs_de_quota(message: str) -> None:
    with pytest.raises(ai.QuotaError, match="Quota IA atteint"):
        ai._fail(message)


def test_fail_limite_le_message_dune_erreur_generique() -> None:
    with pytest.raises(ai.AIError) as erreur:
        ai._fail("x" * 800)
    assert len(str(erreur.value)) == 500


def test_bin_refuse_une_commande_absente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai.shutil, "which", lambda _name: None)
    with pytest.raises(ai.AIError, match="introuvable"):
        ai._bin("assistant-inexistant")


def test_run_convertit_un_timeout_en_erreur_metier(monkeypatch: pytest.MonkeyPatch) -> None:
    def expire(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["assistant"], timeout=1)

    monkeypatch.setattr(ai.subprocess, "run", expire)
    with pytest.raises(ai.AIError, match="n'a pas répondu à temps"):
        ai._run(["assistant"], "question", 1, "Assistant")


def test_adaptateur_claude_retourne_uniquement_la_sortie_structuree(monkeypatch: pytest.MonkeyPatch) -> None:
    sortie = {"structured_output": {"score": 91}, "is_error": False}
    processus = subprocess.CompletedProcess([], 0, stdout=json.dumps(sortie), stderr="")
    monkeypatch.setattr(ai, "_bin", lambda _name: "/usr/bin/claude-factice")
    monkeypatch.setattr(ai, "_run", lambda *_args, **_kwargs: processus)

    resultat = ai._ask_claude(
        "question",
        {"type": "object", "properties": {"score": {"type": "integer"}}},
        "",
        {"claude_bin": "claude", "ai_timeout_s": 10},
    )

    assert resultat == {"score": 91}


def test_adaptateur_codex_utilise_schema_et_bac_a_sable(monkeypatch: pytest.MonkeyPatch) -> None:
    appels = []

    def faux_run(cmd: list[str], prompt: str, timeout: int, label: str):
        appels.append((cmd, prompt, timeout, label))
        Path(cmd[cmd.index("-o") + 1]).write_text('{"score": 91}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ai, "_bin", lambda _name: "/usr/bin/codex-factice")
    monkeypatch.setattr(ai, "_run", faux_run)
    resultat = ai._ask_codex(
        "question", {"type": "object", "properties": {"score": {"type": "integer"}}}, "",
        {"codex_bin": "codex", "ai_timeout_s": 10},
    )

    commande = appels[0][0]
    assert resultat == {"score": 91}
    assert commande[:2] == ["/usr/bin/codex-factice", "exec"]
    assert "--ephemeral" in commande
    assert commande[commande.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in commande
    assert commande[-1] == "-"


def test_ask_ai_selectionne_le_fournisseur_et_le_modele(monkeypatch: pytest.MonkeyPatch) -> None:
    appels = []

    def fournisseur(prompt: str, schema: dict, model: str, cfg: dict) -> dict:
        appels.append((prompt, schema, model, cfg))
        return {"ok": True}

    configuration = {"ai_provider": "test", "models": {"test": {"scoring": "modele-test"}}}
    monkeypatch.setattr(ai, "load_config", lambda: configuration)
    monkeypatch.setitem(ai.PROVIDERS, "test", fournisseur)

    assert ai.ask_ai("question", {"type": "object"}, "scoring") == {"ok": True}
    assert appels[0][2] == "modele-test"


def test_analyze_criteria_transmet_la_tache_attendue(monkeypatch: pytest.MonkeyPatch) -> None:
    appel = {}

    def faux_ask(prompt: str, schema: dict, task: str) -> dict:
        appel.update(prompt=prompt, schema=schema, task=task)
        return {"name": "test"}

    monkeypatch.setattr(ai, "ask_ai", faux_ask)
    assert ai.analyze_criteria({"contract": "stage"}, "cybersécurité") == {"name": "test"}
    assert appel["task"] == "criteria"
    assert "cybersécurité" in appel["prompt"]
    assert appel["schema"] is ai.CRITERIA_SCHEMA


def test_score_offers_limite_la_description_envoyee(monkeypatch: pytest.MonkeyPatch) -> None:
    appel = {}

    def faux_ask(prompt: str, schema: dict, task: str) -> dict:
        appel.update(prompt=prompt, schema=schema, task=task)
        return {"results": [{"id": 3}]}

    monkeypatch.setattr(ai, "ask_ai", faux_ask)
    offres = [{"id": 3, "title": "Stage SOC", "description": "x" * 5000}]

    assert ai.score_offers({}, offres) == [{"id": 3}]
    assert appel["task"] == "scoring"
    assert "x" * 4000 in appel["prompt"]
    assert "x" * 4001 not in appel["prompt"]
    assert appel["schema"] is ai.SCORING_SCHEMA
