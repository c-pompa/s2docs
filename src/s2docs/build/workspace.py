"""Local addon workspace: scaffold CS2 addons and read/write their source files safely.

Layout mirrors the game install so a later push is a straight copy:
    <workspace>/cs2/content/csgo_addons/<addon>/   (sources Hammer and resourcecompiler read)

Safety: addon names are restricted to [a-z0-9_]; every path is resolved and must stay
inside the addon root; only text source formats can be written; overwrites are opt-in.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from s2docs import models as m
from s2docs.db.repository import Repository

ADDON_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
# Text formats an agent may author. Binary/compiled assets (.vmap, *_c, .vpk) are produced by Valve tools.
WRITABLE_SUFFIXES = {
    ".js",
    ".ts",
    ".json",
    ".cfg",
    ".txt",
    ".md",
    ".xml",
    ".css",
    ".vcss",
    ".vxml",
    ".vmat",
    ".vdata",
    ".kv3",
    ".vsndevts",
    ".vpcf",
    ".vmdl",
    ".vsnd",
    ".vts",
    ".gitkeep",
}
MAX_FILE_BYTES = 2 * 1024 * 1024
CONTENT_SUBDIRS = ("scripts", "maps", "materials", "models", "sounds", "panorama", "cfg")

GAME_CONTENT_DIRS = {"cs2": "content/csgo_addons"}


class WorkspaceError(ValueError):
    pass


def _check_game(game: str) -> str:
    if game not in GAME_CONTENT_DIRS:
        raise WorkspaceError(f"addon workspace supports: {', '.join(GAME_CONTENT_DIRS)} (got {game!r})")
    return GAME_CONTENT_DIRS[game]


def addon_root(workspace: Path, game: str, name: str) -> Path:
    if not ADDON_NAME_RE.match(name):
        raise WorkspaceError("addon name must match [a-z][a-z0-9_]{1,62} (lowercase, Hammer-safe)")
    return workspace / game / _check_game(game) / name


def _safe_path(root: Path, rel: str) -> Path:
    pure = PurePosixPath(rel.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise WorkspaceError(f"invalid relative path {rel!r}")
    path = (root / Path(*pure.parts)).resolve()
    if root.resolve() not in path.parents:
        raise WorkspaceError(f"path {rel!r} escapes the addon root")
    return path


def describe(workspace: Path, game: str, name: str) -> m.Addon:
    root = addon_root(workspace, game, name)
    if not root.is_dir():
        raise WorkspaceError(f"addon {name!r} does not exist; scaffold it first")
    files = [
        m.AddonFile(path=p.relative_to(root).as_posix(), size=p.stat().st_size)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    return m.Addon(name=name, game=game, root=str(root), files=files)


def list_addons(workspace: Path, game: str) -> list[str]:
    base = workspace / game / _check_game(game)
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def write_file(
    workspace: Path, game: str, name: str, rel: str, content: str, *, overwrite: bool = False
) -> m.AddonFile:
    root = addon_root(workspace, game, name)
    if not root.is_dir():
        raise WorkspaceError(f"addon {name!r} does not exist; scaffold it first")
    path = _safe_path(root, rel)
    if path.suffix.lower() not in WRITABLE_SUFFIXES:
        raise WorkspaceError(
            f"refusing to write {path.suffix or 'extensionless'} files; allowed: {', '.join(sorted(WRITABLE_SUFFIXES))}"
        )
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceError("file too large (2 MB limit)")
    if path.exists() and not overwrite:
        raise WorkspaceError(f"{rel} exists; pass overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return m.AddonFile(path=path.relative_to(root).as_posix(), size=len(data))


def read_file(workspace: Path, game: str, name: str, rel: str) -> str:
    root = addon_root(workspace, game, name)
    path = _safe_path(root, rel)
    if not path.is_file():
        raise WorkspaceError(f"{rel} not found in addon {name!r}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError("file too large to read (2 MB limit)")
    return path.read_text(encoding="utf-8", errors="replace")


_SCRIPT_TEMPLATE = """\
import {{ Instance }} from "cs_script/point_script";

// {name}: attached to a point_script entity (keyvalue cs_script = scripts/{name}.vjs).
// Top-level code runs when the point_script spawns; register callbacks for everything else.

Instance.OnActivate(() => {{
    Instance.Msg("[{name}] activated on " + Instance.GetMapName());
}});

Instance.OnRoundStart(() => {{
    Instance.Msg("[{name}] round start");
}});

// Fired by `ent_fire <point_script name> RunScriptInput ping` or a map output.
Instance.OnScriptInput("ping", () => {{
    Instance.Msg("[{name}] pong");
}});
"""

_README_TEMPLATE = """\
# {name}

CS2 addon scaffolded by s2docs.

## Layout
- `scripts/{name}.js` — cs_script entry point (compiled by the tools to `scripts/{name}.vjs_c`)
- `scripts/point_script.d.ts`, `scripts/tsconfig.json` — Valve's API declarations (editor IntelliSense)
- `maps/` — save your `.vmap` here from Hammer
- `materials/ models/ sounds/ panorama/ cfg/` — other content

## Hammer setup
1. Launch **Counter-Strike 2 Workshop Tools**, pick addon `{name}`, open Hammer.
2. Place a `point_script` entity (Entity Tool, Shift+E), name it `{name}_script`.
3. Set its `cs_script` keyvalue to `scripts/{name}.vjs` (Asset Browser filter: scripts).
4. Build/run the map (F9). The console shows `[{name}] activated on ...`.

## Iterating
In tools mode, saving the `.js` recompiles it and re-runs the top-level code.
Test the input hook: `ent_fire {name}_script RunScriptInput ping`.
"""


_README_PLAIN_TEMPLATE = """\
# {name}

CS2 addon scaffolded by s2docs (no cs_script).

## Layout
- `cfg/` — console configs (validate with `validate_cfg`)
- `maps/ materials/ models/ sounds/ panorama/ scripts/` — other content
"""


def script_template(name: str) -> str:
    """The cs_script skeleton ``scaffold`` writes, as a string.

    Public because the pipeline's *generate* stage must produce the same artifact the scaffold
    produces — a second, drifting copy of this template is how the two silently disagree.
    """
    return _SCRIPT_TEMPLATE.format(name=name)


def scaffold(workspace: Path, repo: Repository, *, game: str, name: str, with_script: bool = True) -> m.Addon:
    root = addon_root(workspace, game, name)
    if root.exists():
        raise WorkspaceError(f"addon {name!r} already exists at {root}")
    for sub in CONTENT_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / ".gitkeep").touch()
    readme = _README_TEMPLATE if with_script else _README_PLAIN_TEMPLATE
    (root / "README.md").write_text(readme.format(name=name), encoding="utf-8")
    if with_script:
        (root / "scripts" / f"{name}.js").write_text(_SCRIPT_TEMPLATE.format(name=name), encoding="utf-8")
        for f in ("point_script.d.ts", "tsconfig.json"):
            (root / "scripts" / f).write_text(repo.raw_file(game, f"cs_script/{f}"), encoding="utf-8")
    return describe(workspace, game, name)
