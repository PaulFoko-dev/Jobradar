from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_applied_offers_can_be_dimmed_and_hidden() -> None:
    index = (ROOT / "jobradar/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "jobradar/static/app.js").read_text(encoding="utf-8")

    assert 'id="qHideApplied" type="checkbox"' in index
    assert "function isAlreadyApplied(o)" in script
    assert "p.hideApplied && isAlreadyApplied(o)" in script
    assert 'isAlreadyApplied(o) ? "dim"' in script
    assert '"#qHideApplied"' in script
