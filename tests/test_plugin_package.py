"""The portable plugin package must stay in sync with the server it launches.

Hermes loads ``plugin/`` as an Agent Plugins v1 package: ``plugin.json`` declares the manifest and
the server's trust, ``mcp.json`` declares how the server is launched, and
``skills/s2docs-source2/references/tools.md`` is the tool table the agent reads. All three drift
silently — a tool renamed in ``mcp_server.py`` leaves a table naming a tool nobody can call — so
they are asserted here rather than trusted.

The heavy check (launch the pinned git ref and compare its live ``tools/list``) lives in
``scripts/plugin_tools_md.py --from-manifest``; it needs the network, so it is not in this suite.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugin"
SKILL_DIR = PLUGIN_DIR / "skills" / "s2docs-source2"
TABLE = SKILL_DIR / "references" / "tools.md"
MCP_SOURCE = REPO_ROOT / "src" / "s2docs" / "mcp_server.py"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _manifest() -> dict:
    return json.loads((PLUGIN_DIR / "plugin.json").read_text())


def _mcp() -> dict:
    return json.loads((PLUGIN_DIR / "mcp.json").read_text())


def test_plugin_json_is_agent_plugins_v1():
    manifest = _manifest()
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", manifest["name"])
    assert manifest["version"]
    assert manifest["description"]


def test_the_declared_server_exists_and_is_gated():
    """A declaration with no matching mcp.json server is a hard load error in Hermes."""
    manifest, mcp = _manifest(), _mcp()
    declared = manifest["extensions"]["com.nousresearch.hermes"]["servers"]
    assert set(declared) == set(mcp["mcpServers"]), "plugin.json and mcp.json must name the same servers"
    # every write-capable tool is behind the approval prompt
    assert declared["s2docs"]["trust"] == "untrusted"


def test_launch_is_pinned_and_offline_safe():
    """The catalog's trust model is an exact commit; a moving ref would install unreviewed code."""
    server = _mcp()["mcpServers"]["s2docs"]
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
    spec = server["args"][server["args"].index("--from") + 1]
    assert "@" in spec, "the package must be pinned to a ref"
    assert SHA_RE.fullmatch(spec.rsplit("@", 1)[1]), f"ref must be a full 40-hex commit sha: {spec!r}"
    # the launch only reaches stdio because these are the args after the requirement
    assert server["args"][-2:] == ["s2docs", "mcp"]


def test_settings_are_passed_as_expandable_refs():
    """Hermes expands ${VAR} through config.py; a bare path here would hard-code one machine."""
    env = _mcp()["mcpServers"]["s2docs"]["env"]
    assert env["S2DOCS_DATA_DIR"] == "${S2DOCS_DATA_DIR}"
    assert env["S2DOCS_PUBLIC_URL"] == "${S2DOCS_PUBLIC_URL}"
    schema = _manifest()["extensions"]["com.nousresearch.hermes"]["config_schema"]
    # every declared env ref has a settings-form entry that writes the same variable
    for key, spec in schema.items():
        assert spec["env"] in env, f"config_schema.{key} writes {spec['env']}, which mcp.json never passes"


def test_skill_frontmatter_matches_agent_skills_constraints():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md needs YAML frontmatter"
    frontmatter = text[3:text.index("\n---", 3)]
    assert f"\nname: {SKILL_DIR.name}\n" in "\n" + frontmatter + "\n"
    description = next(line.split(":", 1)[1].strip() for line in frontmatter.splitlines()
                       if line.startswith("description:"))
    assert 1 <= len(description.strip('"')) <= 1024


def _registered_tool_names() -> set[str]:
    """Every tool name the server registers — read from the AST, so no import or DB is needed."""
    tree = ast.parse(MCP_SOURCE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (isinstance(target, ast.Attribute) and target.attr == "tool"
                    and isinstance(target.value, ast.Name) and target.value.id == "server"):
                names.add(node.name)
    return names


def test_tool_table_covers_every_registered_tool():
    """A table naming a tool the server does not have is a lie the model acts on.

    The failure mode this must catch is a rename or a removal: the agent calls a tool from the table
    and gets "unknown tool". A tool that exists in un-released source but is not yet in the table is
    the normal state between a code commit and its pin bump (the table describes the PINNED server),
    so additions are allowed here and fixed by the release step, not by this test.
    """
    documented = set(re.findall(r"^\| `([a-z0-9_]+)` \|", TABLE.read_text(encoding="utf-8"), re.MULTILINE))
    registered = _registered_tool_names()
    assert registered, "no tools found in mcp_server.py — the AST scan is broken, not the server"
    assert not (documented - registered), (
        f"tools.md documents tools that do not exist: {sorted(documented - registered)}"
        " — regenerate with `python scripts/plugin_tools_md.py`"
    )
    unreleased = sorted(registered - documented)
    assert not any(name in TABLE.read_text(encoding="utf-8") for name in unreleased if len(name) > 3), (
        "a tool is mentioned in the table without a row"
    )


def test_tool_table_records_where_it_came_from():
    header = TABLE.read_text(encoding="utf-8").splitlines()[:6]
    joined = "\n".join(header)
    assert "live `tools/list`" in joined
    assert "plugin/mcp.json" in joined or "this checkout" in joined
    assert re.search(r"\b\d+ tools \(\d+ read-only", joined), "the table must state its sample"


def test_plugin_has_no_tracked_build_output():
    """Rule 17: no compiled payloads, no caches — only reviewable source ships."""
    banned = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".wasm", ".zip", ".tgz"}
    stray = [str(path.relative_to(PLUGIN_DIR)) for path in PLUGIN_DIR.rglob("*")
             if path.is_file() and path.suffix in banned]
    assert stray == [], f"unreviewable payloads in the plugin package: {stray}"
