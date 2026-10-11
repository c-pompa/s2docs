"""Runtime configuration (env-overridable) and the game registry."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Game:
    id: str
    name: str
    app_id: int
    mod_dir: str  # folder under game/ (csgo, citadel)
    repo: str  # SteamTracking GameTracking repository
    script_api: str | None = None  # official scripting API shipped as .d.ts, if any

    @property
    def sparse_patterns(self) -> list[str]:
        """Non-cone sparse-checkout patterns: only what the ingesters read."""
        patterns = [
            "/DumpSource2/",
            f"/game/{self.mod_dir}/steam.inf",
            "/game/core/pak01_dir/resource/*.gameevents",
            f"/game/{self.mod_dir}/pak01_dir/resource/*.gameevents",
        ]
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
    ),
    "deadlock": Game(
        id="deadlock",
        name="Deadlock",
        app_id=1422450,
        mod_dir="citadel",
        repo="https://github.com/SteamTracking/GameTracking-Deadlock.git",
    ),
}


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    db_path: Path
    sources_dir: Path


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("S2DOCS_DATA_DIR", Path.cwd() / "data")).expanduser().resolve()
    db_path = Path(os.environ.get("S2DOCS_DB", data_dir / "s2docs.db")).expanduser().resolve()
    return Settings(data_dir=data_dir, db_path=db_path, sources_dir=data_dir / "sources")


def get_game(game_id: str) -> Game:
    try:
        return GAMES[game_id]
    except KeyError:
        raise KeyError(f"unknown game {game_id!r}; known: {', '.join(GAMES)}") from None
