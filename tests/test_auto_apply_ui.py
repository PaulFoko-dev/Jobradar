from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_auto_apply_page_has_live_text_search() -> None:
    index = (ROOT / "jobradar/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "jobradar/static/app.js").read_text(encoding="utf-8")

    assert 'id="aaQuery" type="search"' in index
    assert '$("#aaQuery").addEventListener("input", renderAutoApplyList)' in script
    assert "matchesAutoApplyQuery(job, query)" in script
    assert "autoApplyResult(job)" in script
