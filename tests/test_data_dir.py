"""Item 1: the default data dir must never depend on cwd (the split-DB incident's root cause).

A server and a mis-spawned MCP client launched from different working directories used to read/write
two different stores when S2DOCS_DATA_DIR was unset. The default is now a *fixed* path: the source
checkout's ``./data`` when running from a checkout, else a per-user directory.
"""

from __future__ import annotations

from pathlib import Path

from s2docs.config import _platform_data_dir, _source_checkout, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_checkout_is_detected():
    # tests run from an editable install of this repo, so the checkout is found
    assert _source_checkout() == REPO_ROOT


def test_project_name_check_is_scoped_to_the_project_table():
    """The name must come from [project].name, not any `name = ...` anywhere in the file."""
    from s2docs.config import _names_this_project

    assert _names_this_project('[project]\nname = "s2docs"\n') is True
    # a `name = "s2docs"` under another table is not the project name
    assert _names_this_project('[tool.foo]\nname = "s2docs"\n') is False
    assert _names_this_project('[project]\nname = "other-project"\n') is False
    assert _names_this_project("[project]\nversion = \"1\"\n") is False
    assert _names_this_project("this is not : toml [\n") is False


def test_default_data_dir_is_the_checkout_not_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("S2DOCS_DATA_DIR", raising=False)
    monkeypatch.delenv("S2DOCS_DB", raising=False)
    monkeypatch.chdir(tmp_path)  # a different cwd, as a mis-spawned MCP client would have
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.data_dir == (REPO_ROOT / "data").resolve()
        assert settings.db_path == (REPO_ROOT / "data" / "s2docs.db").resolve()
        # moving cwd again changes nothing — the default is cwd-independent
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        get_settings.cache_clear()
        assert get_settings().db_path == (REPO_ROOT / "data" / "s2docs.db").resolve()
    finally:
        get_settings.cache_clear()


def test_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "custom"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().data_dir == (tmp_path / "custom").resolve()
        assert get_settings().db_path == (tmp_path / "custom" / "s2docs.db").resolve()
    finally:
        get_settings.cache_clear()


def test_non_checkout_uses_a_fixed_per_user_dir(monkeypatch):
    """An installed (non-editable) copy has no checkout, so the default is a per-user dir — never cwd."""
    monkeypatch.delenv("S2DOCS_DATA_DIR", raising=False)
    monkeypatch.setattr("s2docs.config._source_checkout", lambda: None)
    get_settings.cache_clear()
    try:
        expected = _platform_data_dir().resolve()
        assert expected.is_absolute()
        assert get_settings().data_dir == expected
    finally:
        get_settings.cache_clear()
