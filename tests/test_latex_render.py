"""Étape 6 : LaTeX généré, échappé et compilé en mode non fiable."""
import json
from pathlib import Path
from types import SimpleNamespace

from jobradar.render import latex
from jobradar.render.renderer import list_templates

FIXTURES = Path(__file__).parent / "fixtures" / "render"


def load(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        path = FIXTURES / "compositions" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_latex_template_is_publicly_selectable() -> None:
    template = next(item for item in list_templates() if item["id"] == "latex-ats")
    assert template["kind"] == "latex"
    assert template["ats"] is True


def test_every_untrusted_character_is_escaped() -> None:
    value = r"\input{/etc/passwd} & 100% #1_$HOME~^"
    escaped = latex.escape(value)
    assert r"\input" not in escaped
    assert r"\textbackslash{}input\{/etc/passwd\}" in escaped
    assert r"\&" in escaped and r"100\%" in escaped and r"\#1\_\$HOME" in escaped


def test_cv_source_never_injects_user_commands() -> None:
    composition, identity = load("stage_cyber"), load("identity")
    composition["summary"] = r"Profil \input{/etc/passwd} & sécurité"
    source = latex.cv_source(composition, identity, title="CV", author="Camille")
    assert r"Profil \input" not in source
    assert r"Profil \textbackslash{}input\{/etc/passwd\} \& sécurité" in source
    assert source.endswith("\\end{document}\n")


def test_tectonic_absent_requests_clean_fallback(monkeypatch) -> None:
    monkeypatch.setattr(latex.shutil, "which", lambda _name: None)
    assert latex.compile_pdf("safe") is None


def test_tectonic_is_confined_and_secrets_are_not_forwarded(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(latex.shutil, "which", lambda _name: "/usr/bin/tectonic")
    monkeypatch.setenv("JOBRADAR_SMTP_PASSWORD", "secret-never-forwarded")

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        Path(command[command.index("--outdir") + 1], "document.pdf").write_bytes(b"%PDF-1.7\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex.subprocess, "run", run)
    assert latex.compile_pdf("safe") == b"%PDF-1.7\n"
    assert "--untrusted" in captured["command"] and "--only-cached" in captured["command"]
    assert captured["kwargs"]["env"]["TECTONIC_UNTRUSTED_MODE"] == "1"
    assert "JOBRADAR_SMTP_PASSWORD" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["timeout"] == 90
