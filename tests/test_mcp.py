import json

import anyio
import pytest
from mcp import Client

from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def server(db):
    return create_mcp_server(db)


def run(coro_fn):
    return anyio.run(coro_fn)


def test_tools_are_listed_read_only(server):
    async def main():
        async with Client(server) as c:
            return (await c.list_tools()).tools

    tools = run(main)
    names = {t.name for t in tools}
    assert {"search_docs", "get_convar", "get_entity", "get_schema_class", "get_script_api", "diff_builds"} <= names
    writers = {
        "scaffold_addon",
        "build_model",
        "write_addon_file",
        "start_project",
        "update_step",
        "register_host",
        "probe_host",
        "claim_host",
        "heartbeat_host",
        "release_host",
        "build_start",
        "build_cancel",
        "deploy_push",
        "deploy_verify",
        "add_evidence",
        "test_start",
        "test_step",
        "test_finish",
        "test_clear",
        "job_enqueue",
        "job_assign",
        "job_start",
        "job_event",
        "job_attach",
        "job_request_screenshot",
        "job_link",
        "job_verdict",
        "job_finish",
        "job_restatus",
        "job_cancel",
        "asset_promote",
        "live_act",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools if t.name not in writers)
    # Non-idempotent writers must NOT advertise idempotency: a retry would launch a second compile,
    # copy the files again, start a second test session or record a duplicate evidence row.
    non_idempotent = {
        "build_start",
        "deploy_push",
        "test_start",
        "add_evidence",
        "job_enqueue",
        "job_start",
        "job_event",
        "job_attach",
        "job_request_screenshot",
        "job_verdict",
        "job_finish",
        "job_restatus",
        "job_cancel",
    }
    assert non_idempotent <= writers
    for t in tools:
        if t.name in non_idempotent:
            assert t.annotations is not None and t.annotations.idempotent_hint is False, t.name
    # Tools that act on a remote host over SSH are open-world (and the destructive ones say so).
    remote = {"build_start", "deploy_push", "build_cancel", "deploy_verify", "probe_host"}
    for t in tools:
        if t.name in remote:
            assert t.annotations is not None and t.annotations.open_world_hint is True, t.name
    for t in tools:
        if t.name in writers and t.name not in remote:
            assert t.annotations is not None and t.annotations.destructive_hint is False, t.name


def test_tool_calls(server):
    async def main():
        async with Client(server) as c:
            search = await c.call_tool("search_docs", {"query": "roundtime", "game": "cs2"})
            ent = await c.call_tool("get_entity", {"game": "cs2", "classname": "server:trigger_multiple"})
            api = await c.call_tool("get_script_api", {"symbol": "Instance"})
            missing = await c.call_tool("get_convar", {"game": "cs2", "name": "nope"})
            bad_game = await c.call_tool("get_convar", {"game": "tf2", "name": "x"})
            return search, ent, api, missing, bad_game

    search, ent, api, missing, bad_game = run(main)
    assert search.structured_content["items"][0]["key"] == "mp_roundtime"
    assert ent.structured_content["inheritance"] == ["trigger", "CBaseEntity", "root"]
    assert api.structured_content["result"]["name"] == "Domain"
    assert missing.is_error and "nope" in missing.content[0].text
    assert bad_game.is_error and "unknown game" in bad_game.content[0].text


def test_prompts_and_resources(server):
    async def main():
        async with Client(server) as c:
            prompts = (await c.list_prompts()).prompts
            rendered = await c.get_prompt("hammer_io_logic", {"goal": "open a door when a button is pressed"})
            resource = await c.read_resource("s2docs://cs2/cs_script/point_script.d.ts")
            return prompts, rendered, resource

    prompts, rendered, resource = run(main)
    assert {p.name for p in prompts} == {
        "cs2_script",
        "hammer_io_logic",
        "server_config",
        "deadlock_mod",
        "cs2_map_rebuild",
        "explain_symbol",
        "orchestrate",
    }
    assert "open a door" in rendered.messages[0].content.text
    assert "declare module" in resource.contents[0].text


def test_build_model_exposes_the_collision_parameters(server):
    """The one tool that writes a model must reach the merged collision emitter, documented as optional."""

    async def main():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}

    tool = run(main)["build_model"]
    schema = tool.input_schema
    props = schema["properties"]
    assert {"collision_surface", "collision_mesh"} <= set(props)
    required = schema.get("required", [])
    assert "collision_surface" not in required and "collision_mesh" not in required
    # each parameter says what its absence does, which is the whole contract for a CLI/MCP caller
    for name in ("collision_surface", "collision_mesh"):
        description = (props[name].get("description") or "").lower()
        assert "absent" in description or "omit" in description, f"{name} does not say what absence does"


def test_mcp_build_model_writes_the_physics_node_on_request(db, tmp_path):
    server = create_mcp_server(db, workspace=tmp_path)

    async def main():
        async with Client(server) as c:
            await c.call_tool("scaffold_addon", {"name": "mcp_phys"})
            with_collision = await c.call_tool("build_model", {
                "addon": "mcp_phys", "name": "probe", "mesh": "models/probe/probe.fbx",
                "collision_surface": "metal"})
            without = await c.call_tool("build_model", {
                "addon": "mcp_phys", "name": "probe2", "mesh": "models/probe2/probe2.fbx"})
            hull_only = await c.call_tool("build_model", {
                "addon": "mcp_phys", "name": "probe3", "mesh": "models/probe3/probe3.fbx",
                "collision_mesh": "models/probe3/probe3_hull.fbx"})
            return with_collision, without, hull_only

    with_collision, without, hull_only = anyio.run(main)
    root = tmp_path / "cs2" / "content" / "csgo_addons" / "mcp_phys"
    phys = (root / "models" / "probe" / "probe.vmdl").read_text(encoding="utf-8")
    assert '_class = "PhysicsShapeList"' in phys
    assert 'surface_prop = "metal"' in phys
    assert 'name = "probe_collision"' in phys
    plain = (root / "models" / "probe2" / "probe2.vmdl").read_text(encoding="utf-8")
    assert "Physics" not in plain
    assert hull_only.is_error


def test_live_tools(server):
    async def main():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            spawnables = await c.call_tool("live_spawnables", {})
            envelope = {"intents": [{"action": "bots", "team_size": 5, "me": "t"}]}
            plan = await c.call_tool("live_plan", {"envelope": envelope})
            return names, spawnables, plan

    names, spawnables, plan = run(main)
    assert {"live_spawnables", "live_plan", "live_act", "live_status"} <= names
    assert spawnables.structured_content["result"][0]["id"] == "chicken"
    # a plain `dict` return carries no output schema, so the payload arrives as the text content
    assert json.loads(plan.content[0].text)["lines"].count("bot_add_t") == 4
