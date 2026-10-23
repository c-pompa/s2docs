"""Runtime configuration (env-overridable) and the game registry."""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

VERSION = "0.1.1"
# Process start time, captured once at import — surfaces in the DB-identity payload (see server_identity).
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# The public base URL of the web UI. The S2DOCS_PUBLIC_URL setting wins outright; with no request to
# derive from (MCP tools, the CLI, the director's reporters) this is the fallback — the LAN address
# the user opens the UI from.
DEFAULT_PUBLIC_URL = "http://127.0.0.1:8812"

# Lock stuck-ness defaults (see hosts.py). A fresh heartbeat whose phase has not moved for longer
# than the threshold reads STUCK; a per-phase override wins (e.g. {'compile': 7200} lets a legitimate
# long compile run before it is flagged).
DEFAULT_STUCK_SEC = 1800

# Built-in per-phase thresholds (code default, not env-only: an env override is lost on every server
# restart). The long silent stretch of a CS2 compile is the *visibility* ray tracing, not lighting: a
# live run (2026-10-13, t-f763024a) measured CBoundaryPointsRayGenerator at 1353.8 s and the LOS scan at
# 1409 s, while the lighting bake is only ~1m47s. compile_map.py reports these as 'compile:vis_rays' and
# 'compile:vis_los', each with its own hang threshold of 3 x 1800 s = 5400 s. Because the driver's log is
# buffered, the server can see 'compile:vis_rays' unchanged for ~25-30 min on a *healthy* run — over the
# 1800 s global default — and raise a false STUCK. Lighting is ~2 min, so it needs no override.
# S2DOCS_STUCK_PHASES merges over these per key — it does not replace the dict.
DEFAULT_STUCK_PHASES: dict[str, int] = {"compile:vis_rays": 5400, "compile:vis_los": 5400}


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
    auth_token: str | None = None  # S2DOCS_AUTH_TOKEN; None = auth disabled (default)
    public_url: str | None = None  # S2DOCS_PUBLIC_URL; None = derive from the request / LAN fallback

    @property
    def auth_required(self) -> bool:
        return bool(self.auth_token)


def _names_this_project(text: str) -> bool:
    """True when the pyproject text names this project in its ``[project]`` table.

    Scoped to ``[project].name`` (parsed with stdlib ``tomllib``) so a stray ``name = "s2docs"`` in
    another table can't make an unrelated repo look like our checkout. Any parse error means 'no'.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == "s2docs"


def _source_checkout() -> Path | None:
    """The repo root when this package runs from a source checkout (editable install), else None.

    Detected by src layout: ``<root>/src/s2docs/config.py`` → ``parents[2]`` is ``<root>``, which must
    hold a ``pyproject.toml`` naming this project in ``[project]``. A non-editable installed copy has
    no such file, so the default falls back to a per-user directory instead of guessing from the cwd.
    """
    root = Path(__file__).resolve().parents[2]
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    return root if _names_this_project(text) else None


def _platform_data_dir() -> Path:
    """A fixed, cwd-independent per-user data dir (Windows: %LOCALAPPDATA%, else XDG-style)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "s2docs"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "s2docs"


def default_data_dir() -> Path:
    """Where the data dir lives when ``S2DOCS_DATA_DIR`` is unset — *never* relative to the cwd.

    A source checkout (editable install) uses ``<checkout>/data`` so a dev server and its MCP clients
    share the checkout no matter where each was launched; anything else uses a fixed per-user dir, so a
    mis-spawned MCP client can never create a shadow DB beside an unrelated working directory.
    """
    checkout = _source_checkout()
    if checkout is not None:
        return checkout / "data"
    return _platform_data_dir()


def _env_ref(name: str) -> str | None:
    """Read an env setting, treating an UNEXPANDED ``${REF}`` placeholder as unset.

    A portable Agent Plugins package passes settings through ``mcp.json`` as
    ``"S2DOCS_DATA_DIR": "${S2DOCS_DATA_DIR}"``. When the profile does not define the variable,
    Hermes keeps the literal placeholder (it warns and assigns the raw ``${...}`` string), so a
    naive read here would resolve a data dir literally named ``${S2DOCS_DATA_DIR}`` and quietly
    create a shadow store beside the launched process. Anything still carrying a placeholder is a
    launcher bug, not a value: warn loudly and fall back to the documented default.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    if "${" in raw:
        print(
            f"WARNING: {name} arrived unexpanded ({raw!r}) — treating it as unset. "
            "Define it in the profile (hermes config set) or in mcp_servers.<name>.env.",
            file=sys.stderr,
        )
        return None
    return raw


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
    raw_data_dir = _env_ref("S2DOCS_DATA_DIR")
    data_dir = Path(raw_data_dir if raw_data_dir else default_data_dir()).expanduser().resolve()
    db_path = Path(_env_ref("S2DOCS_DB") or data_dir / "s2docs.db").expanduser().resolve()
    workspace = Path(_env_ref("S2DOCS_WORKSPACE") or data_dir / "workspace").expanduser().resolve()
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        sources_dir=data_dir / "sources",
        workspace_dir=workspace,
        stuck_sec=_stuck_sec(),
        stuck_phases=_stuck_phases(),
        auth_token=_env_ref("S2DOCS_AUTH_TOKEN") or None,
        public_url=(_env_ref("S2DOCS_PUBLIC_URL") or "").rstrip("/") or None,
    )


def server_identity() -> dict:
    """The DB-identity payload: which store this process serves, and which process it is.

    Clients compare this (REST /api/health, MCP server_info) before writing, so the split-database
    class of incident — two servers pointed at different data dirs — is at least detectable.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "db_path": str(settings.db_path.resolve()),
        "data_dir": str(settings.data_dir.resolve()),
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "version": VERSION,
        "auth_required": settings.auth_required,
        "public_url": settings.public_url or DEFAULT_PUBLIC_URL,
        # The confirm push channel: files a waiting agent watches instead of polling /api/evidence.
        "confirms_jsonl": str((settings.data_dir / "confirms.jsonl").resolve()),
        "latest_confirm": str((settings.data_dir / "latest_confirm.json").resolve()),
        # The job-event push channel: every job event appends a line here (data_dir/job_events.jsonl).
        "job_events_jsonl": str((settings.data_dir / "job_events.jsonl").resolve()),
    }


def get_game(game_id: str) -> Game:
    try:
        return GAMES[game_id]
    except KeyError:
        raise KeyError(f"unknown game {game_id!r}; known: {', '.join(GAMES)}") from None
