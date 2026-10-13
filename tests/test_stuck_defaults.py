"""Built-in STUCK defaults for long compile phases (the silent lighting bake).

`compile_map.py` reports phases as `compile:<phase>` and the lighting bake is silent for most of a
~38 min compile; its own hang threshold is 3 x 2400 s = 7200 s. The global default (1800 s) would read
a healthy lighting bake as STUCK, and an env-only override is lost whenever the server restarts, so
`compile:lighting` needs a **code** default. `S2DOCS_STUCK_PHASES` merges over it per key.

No host/network is touched; these are pure settings + threshold lookups.
"""

from __future__ import annotations

import pytest

from s2docs import hosts
from s2docs.config import DEFAULT_STUCK_SEC, get_settings


@pytest.fixture(autouse=True)
def _fresh_settings():
    """get_settings is lru_cached and thresholds come from env — never leak one test into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lighting_bake_has_a_builtin_default_with_no_env():
    assert hosts.stuck_threshold("compile:lighting") == 7200
    # every other phase still falls back to the global default
    assert hosts.stuck_threshold("compile:vis") == DEFAULT_STUCK_SEC == 1800
    assert hosts.stuck_threshold("compile") == 1800
    assert get_settings().stuck_phases == {"compile:lighting": 7200}


def test_env_override_wins_for_the_same_key(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", '{"compile:lighting": 9000}')
    assert hosts.stuck_threshold("compile:lighting") == 9000


def test_env_keys_merge_over_the_builtin_default(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", '{"test": 600}')
    assert hosts.stuck_threshold("test") == 600
    # the built-in lighting default survives an unrelated env entry (merge, not replace)
    assert hosts.stuck_threshold("compile:lighting") == 7200
    assert get_settings().stuck_phases == {"compile:lighting": 7200, "test": 600}


def test_malformed_env_leaves_the_builtin_default_intact(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", "{not json")
    monkeypatch.setenv("S2DOCS_STUCK_SEC", "not-a-number")
    settings = get_settings()
    assert settings.stuck_phases == {"compile:lighting": 7200}
    assert settings.stuck_sec == 1800
    assert hosts.stuck_threshold("compile:lighting") == 7200
