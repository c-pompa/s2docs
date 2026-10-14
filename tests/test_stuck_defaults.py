"""Built-in STUCK defaults for the long, silent compile phases (the visibility ray trace).

A live CS2 compile (2026-10-13, run t-f763024a) showed the long silent stretch is the *visibility*
ray tracing, not lighting: `CBoundaryPointsRayGenerator` alone ran 1353.8 s and the LOS scan totalled
1409 s, while the lighting bake is only ~1m47s. `compile_map.py` reports these phases as
`compile:vis_rays` and `compile:vis_los`, each with its own hang threshold of 3 x 1800 s = 5400 s. The
driver's log is buffered, so on a *healthy* run the server can see `compile:vis_rays` unchanged for
~25-30 min — over the 1800 s global default — and flag a false STUCK. An env-only override is lost
whenever the server restarts, so these phases need a **code** default. `S2DOCS_STUCK_PHASES` merges
over it per key.

No host/network is touched; these are pure settings + threshold lookups.
"""

from __future__ import annotations

import pytest

from s2docs import hosts
from s2docs.config import DEFAULT_STUCK_SEC, get_settings

# The measured long compile phases, each 3 x the driver's 1800 s per-phase poll baseline.
_VIS_DEFAULTS = {"compile:vis_rays": 5400, "compile:vis_los": 5400}


@pytest.fixture(autouse=True)
def _fresh_settings():
    """get_settings is lru_cached and thresholds come from env — never leak one test into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_visibility_phases_have_a_builtin_default_with_no_env():
    assert hosts.stuck_threshold("compile:vis_rays") == 5400
    assert hosts.stuck_threshold("compile:vis_los") == 5400
    # lighting bakes in ~2 min, so it needs no override and still falls back to the global default
    assert hosts.stuck_threshold("compile:lighting") == DEFAULT_STUCK_SEC == 1800
    assert hosts.stuck_threshold("compile") == 1800
    assert get_settings().stuck_phases == _VIS_DEFAULTS


def test_env_override_wins_for_the_same_key(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", '{"compile:vis_rays": 9000}')
    assert hosts.stuck_threshold("compile:vis_rays") == 9000


def test_env_keys_merge_over_the_builtin_default(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", '{"test": 600}')
    assert hosts.stuck_threshold("test") == 600
    # the built-in defaults survive an unrelated env entry (merge, not replace)
    assert hosts.stuck_threshold("compile:vis_rays") == 5400
    assert hosts.stuck_threshold("compile:vis_los") == 5400
    assert get_settings().stuck_phases == {**_VIS_DEFAULTS, "test": 600}


def test_malformed_env_leaves_the_builtin_default_intact(monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", "{not json")
    monkeypatch.setenv("S2DOCS_STUCK_SEC", "not-a-number")
    settings = get_settings()
    assert settings.stuck_phases == _VIS_DEFAULTS
    assert settings.stuck_sec == 1800
    assert hosts.stuck_threshold("compile:vis_rays") == 5400
