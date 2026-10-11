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
    writers = {"scaffold_addon", "write_addon_file", "start_project", "update_step"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools if t.name not in writers)
    assert all(t.annotations and t.annotations.destructive_hint is False for t in tools if t.name in writers)


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
        "explain_symbol",
        "orchestrate",
    }
    assert "open a door" in rendered.messages[0].content.text
    assert "declare module" in resource.contents[0].text
