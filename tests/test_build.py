"""Phase B: script/cfg validation and the addon workspace."""

import anyio
import pytest
from mcp import Client

from s2docs.build import workspace as ws
from s2docs.build.validate_cfg import validate_cfg
from s2docs.build.validate_script import ToolchainMissing, find_tsc, validate_cs_script
from s2docs.db.repository import Repository
from s2docs.mcp_server import create_mcp_server


def _have_tsc() -> bool:
    try:
        find_tsc()
        return True
    except ToolchainMissing:
        return False


needs_tsc = pytest.mark.skipif(not _have_tsc(), reason="TypeScript not installed (npm ci --prefix tools/tsc)")

GOOD = """import { Instance } from "cs_script/point_script";
Instance.OnPlayerJump((event) => { Instance.Msg(event.player.GetHealth()); });
"""

BAD = """import { Instance } from "cs_script/point_script";
Instance.TeleportEveryone();
const hit = Instance.TraceLine({ start: { x: 0, y: 0, z: 0 }, end: { x: 1, y: 0, z: 0 } }).hitEntity;
if (hit) { Instance.Msg(hit.GetHealth()); }
Instance.OnReload(() => {});
"""


# ------------------------------------------------------------- cs_script


@needs_tsc
def test_valid_script_passes(repo: Repository):
    r = validate_cs_script(repo, GOOD, filename="good.js")
    assert r.ok and r.errors == 0 and r.instance_methods_used == ["Msg", "OnPlayerJump"]
    assert r.checker.startswith("tsc ")


@needs_tsc
def test_invalid_script_categories(repo: Repository):
    r = validate_cs_script(repo, BAD, filename="bad.js")
    cats = {(d.line, d.category) for d in r.diagnostics}
    assert (2, "unknown_api") in cats  # invented method
    assert (4, "needs_type_narrowing") in cats  # GetHealth is on CSPlayerPawn, hitEntity is Entity
    assert (5, "deprecated_api") in cats
    narrowing = next(d for d in r.diagnostics if d.category == "needs_type_narrowing")
    assert "CSPlayerPawn" in narrowing.hint
    assert not r.ok and r.errors == 1


def test_script_filename_validated(repo: Repository):
    with pytest.raises(ValueError):
        validate_cs_script(repo, GOOD, filename="../../etc/passwd")


# ------------------------------------------------------------------- cfg


def test_cfg_checks(repo: Repository):
    cfg = """// practice config
mp_roundtime 100          // out of range
sv_multiline 1; hostname "My Server; with semicolon"
sv_cheats 1
sv_multiline 1
mp_roundtme 3
+lookatweapon
mp_roundtime
mp_roundtime abc
"""
    r = validate_cfg(repo, cfg, game="cs2", context="server")
    by_line = {}
    for d in r.diagnostics:
        by_line.setdefault(d.line, set()).add(d.category)
    assert by_line[2] == {"out_of_range"}
    assert "requires_cheats" in by_line[3]  # before sv_cheats 1
    assert 5 not in by_line  # after sv_cheats 1, cheat is fine
    assert by_line[6] == {"unknown_command"}
    unknown = next(d for d in r.diagnostics if d.category == "unknown_command")
    assert "mp_roundtime" in (unknown.hint or "")
    assert "wrong_context" in by_line[7]  # +lookatweapon is clientdll
    assert by_line[8] == {"no_value"} and by_line[9] == {"type_mismatch"}
    assert r.commands == 9 and not r.ok


def test_cfg_clean(repo: Repository):
    r = validate_cfg(repo, "mp_roundtime 3\nbot_kick\n", game="cs2")
    assert r.ok and r.diagnostics == []


# ------------------------------------------------------------- workspace


def test_scaffold_and_file_safety(repo: Repository, tmp_path):
    addon = ws.scaffold(tmp_path, repo, game="cs2", name="jump_arena")
    paths = {f.path for f in addon.files}
    assert {"README.md", "scripts/jump_arena.js", "scripts/point_script.d.ts", "scripts/tsconfig.json"} <= paths
    assert addon.root.endswith("cs2/content/csgo_addons/jump_arena")
    with pytest.raises(ws.WorkspaceError):
        ws.scaffold(tmp_path, repo, game="cs2", name="jump_arena")  # exists
    for bad_name in ("Jump", "../x", "a", "with space"):
        with pytest.raises(ws.WorkspaceError):
            ws.addon_root(tmp_path, "cs2", bad_name)
    for bad_path in ("../escape.js", "/etc/passwd", "scripts/../../x.js", ""):
        with pytest.raises(ws.WorkspaceError):
            ws.write_file(tmp_path, "cs2", "jump_arena", bad_path, "x")
    with pytest.raises(ws.WorkspaceError):
        ws.write_file(tmp_path, "cs2", "jump_arena", "maps/arena.vmap_c", "binary")  # compiled asset
    with pytest.raises(ws.WorkspaceError):
        ws.write_file(tmp_path, "cs2", "jump_arena", "scripts/jump_arena.js", "x")  # no overwrite
    f = ws.write_file(tmp_path, "cs2", "jump_arena", "scripts/jump_arena.js", "// v2", overwrite=True)
    assert f.size == 5 and ws.read_file(tmp_path, "cs2", "jump_arena", "scripts/jump_arena.js") == "// v2"
    with pytest.raises(ws.WorkspaceError):
        ws.scaffold(tmp_path, repo, game="deadlock", name="nope")  # no workshop tools for deadlock
    plain = ws.scaffold(tmp_path, repo, game="cs2", name="plain_addon", with_script=False)
    assert not any(f.path.endswith(".js") for f in plain.files)
    assert "cs_script" not in ws.read_file(tmp_path, "cs2", "plain_addon", "README.md").replace("(no cs_script)", "")


@needs_tsc
def test_scaffolded_script_type_checks(repo: Repository, tmp_path):
    ws.scaffold(tmp_path, repo, game="cs2", name="starter")
    code = ws.read_file(tmp_path, "cs2", "starter", "scripts/starter.js")
    # The fixture d.ts is a subset; the template only uses methods present in the real API.
    real_methods = {"OnActivate", "Msg", "GetMapName", "OnRoundStart", "OnScriptInput"}
    assert set(validate_cs_script(repo, code, filename="starter.js").instance_methods_used) == real_methods


@needs_tsc
def test_mcp_build_flow(db, tmp_path):
    server = create_mcp_server(db, workspace=tmp_path)

    async def main():
        async with Client(server) as c:
            made = await c.call_tool("scaffold_addon", {"name": "mcp_addon"})
            wrote = await c.call_tool(
                "write_addon_file", {"name": "mcp_addon", "path": "scripts/extra.js", "content": BAD}
            )
            checked = await c.call_tool("validate_cs_script", {"addon": "mcp_addon", "path": "scripts/extra.js"})
            escape = await c.call_tool(
                "write_addon_file", {"name": "mcp_addon", "path": "../../evil.js", "content": "x"}
            )
            listed = await c.call_tool("list_addons", {})
            return made, wrote, checked, escape, listed

    made, wrote, checked, escape, listed = anyio.run(main)
    assert not made.is_error and not wrote.is_error
    assert checked.structured_content["errors"] == 1
    assert escape.is_error and "invalid relative path" in escape.content[0].text
    assert listed.structured_content["result"] == ["mcp_addon"]
