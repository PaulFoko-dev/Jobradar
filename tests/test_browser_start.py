import os

import pytest

from jobradar import application_browser


def test_orphan_profile_lock_is_removed_but_a_live_one_is_kept(tmp_path) -> None:
    os.symlink("machine-999999999", tmp_path / "SingletonLock")
    (tmp_path / "SingletonCookie").write_text("x")
    assert application_browser._stale_profile_lock(tmp_path)
    assert not (tmp_path / "SingletonLock").is_symlink() and not (tmp_path / "SingletonCookie").exists()

    os.symlink(f"machine-{os.getpid()}", tmp_path / "SingletonLock")
    assert not application_browser._stale_profile_lock(tmp_path)
    assert (tmp_path / "SingletonLock").is_symlink()


def test_a_browser_handing_over_to_an_open_window_gives_a_clear_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(application_browser.config, "system_profile_dir", lambda _key: tmp_path)
    monkeypatch.setattr(application_browser, "_port_open", lambda _port: False)
    # « true » rend la main aussitôt, comme Brave qui confie l'ouverture à une fenêtre existante.
    browser = {"key": "brave", "name": "Brave", "path": "/bin/true"}
    with pytest.raises(application_browser.BrowserUnavailable, match="déjà ouverte sans port"):
        application_browser._start_browser(browser, 9333)
