"""Runtime configuration (env-overridable) and the game registry."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Lock stuck-ness defaults (see hosts.py). A fresh heartbeat whose phase has not moved for longer
# than the threshold reads STUCK; a per-phase override wins (e.g. {'compile': 7200} lets a legitimate
# long compile run before it is flagged).
DEFAULT_STUCK_SEC = 1800

# Built-in per-phase thresholds (code default, not env-only: an env override is lost on every server
# restart). 'compile:lighting' bakes silently for most of a ~38 min compile, so the global 1800 s would
# read a healthy bake as STUCK; 7200 s matches the driver's own hang threshold in tools/build/compile_map.py
# (3 x 2400 s). S2DOCS_STUCK_PHASES merges over these per key — it does not replace the dict.
DEFAULT_STUCK_PHASES: dict[str, int] = {"compile:lighting": 7200}


@dataclass(frozen=True, slots=True)
class Game:
    id: str
    name: str
    app_id: int
    mod_dir: str  # folder under game/ (csgo, citadel)
    repo: str  # SteamTracking GameTracking repository
    script_api: str | None = None  # official scripting API shipped as .d.ts, if any
    # Valve hand-written FGD entry points: root label -> file name, resolved against game/<mod>, then game/core.
    hammer_fgd_roots: dict[str, str] = field(default_factory=dict)

    @property
    def fgd_search_dirs(self) -> list[str]:
        return [f"game/{self.mod_dir}", "game/core"]

    @property
    def sparse_patterns(self) -> list[str]:
        """Non-cone sparse-checkout patterns: only what the ingesters read."""
        patterns = [
            "/DumpSource2/",
            f"/game/{self.mod_dir}/steam.inf",
            "/game/core/pak01_dir/resource/*.gameevents",
            f"/game/{self.mod_dir}/pak01_dir/resource/*.gameevents",
        ]
        if self.hammer_fgd_roots:
            patterns += [f"/game/{self.mod_dir}/**/*.fgd", "/game/core/**/*.fgd"]
        if self.script_api == "cs_script":
            patterns.append("/content/csgo_addons/cs_script_demo/maps/scripts/")
        return patterns


GAMES: dict[str, Game] = {
    "cs2": Game(
        id="cs2",
        name="Counter-Strike 2",
        app_id=730,
        mod_dir="csgo",
        repo="https://github.com/SteamTracking/GameTracking-CS2.git",
        script_api="cs_script",
        hammer_fgd_roots={
            "hammer": "csgo.fgd",
            "modeldoc": "models_gamedata.fgd",
            "cs2_item_editor": "tools/cs2_item_editor/cs2_item_editor.fgd",
            "addoninfo": "tools/workshop_addoninfo.fgd",
        },
    ),
    "deadlock": Game(
        id="deadlock",
        name="Deadlock",
        app_id=1422450,
        mod_dir="citadel",
        repo="https://github.com/SteamTracking/GameTracking-Deadlock.git",
    ),
}

# Community documentation sources (cloned shallowly next to the GameTracking checkouts).
SOURCE2WIKI_REPO = "https://github.com/Source2Wiki/Source2Wiki.git"
SOURCE2WIKI_SITE = "https://www.source2.wiki"
SOURCE2WIKI_LICENSE = "CC BY-SA 4.0 (Source2Wiki contributors)"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    db_path: Path
    sources_dir: Path
    workspace_dir: Path  # local addon sources (scaffolded/written by the build tools)
    stuck_sec: int = DEFAULT_STUCK_SEC  # phase age (s) after which a fresh lock reads STUCK
    stuck_phases: dict[str, int] = field(default_factory=dict)  # per-phase overrides, merged over DEFAULT_STUCK_PHASES


def _stuck_phases() -> dict[str, int]:
    """Per-phase stuck thresholds: the built-in defaults merged with S2DOCS_STUCK_PHASES (env wins per key).

    Fail-soft on purpose: a bad env value must never take down the read paths that compute stuck-ness.
    """
    raw = (os.environ.get("S2DOCS_STUCK_PHASES") or "").strip()
    if not raw:
        return dict(DEFAULT_STUCK_PHASES)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_STUCK_PHASES)
    if not isinstance(data, dict):
        return dict(DEFAULT_STUCK_PHASES)
    out: dict[str, int] = dict(DEFAULT_STUCK_PHASES)
    for key, value in data.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _stuck_sec() -> int:
    try:
        return int(os.environ.get("S2DOCS_STUCK_SEC") or DEFAULT_STUCK_SEC)
    except ValueError:
        return DEFAULT_STUCK_SEC


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("S2DOCS_DATA_DIR", Path.cwd() / "data")).expanduser().resolve()
    db_path = Path(os.environ.get("S2DOCS_DB", data_dir / "s2docs.db")).expanduser().resolve()
    workspace = Path(os.environ.get("S2DOCS_WORKSPACE", data_dir / "workspace")).expanduser().resolve()
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        sources_dir=data_dir / "sources",
        workspace_dir=workspace,
        stuck_sec=_stuck_sec(),
        stuck_phases=_stuck_phases(),
    )


def get_game(game_id: str) -> Game:
    try:
        return GAMES[game_id]
    except KeyError:
        raise KeyError(f"unknown game {game_id!r}; known: {', '.join(GAMES)}") from None
