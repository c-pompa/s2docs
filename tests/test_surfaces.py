"""The surface registry must be an accurate map of what exists, not a wish list.

`src/s2docs` has three hand-wired surfaces over the same services (argparse CLI, FastAPI routes,
MCP tools). The registry in `s2docs/surfaces.py` is the one place that says which operation lives
where, and `verify()` proves it against the code. The real gate here is
`test_no_row_declares_a_surface_it_does_not_have`: it must pass for the WHOLE table, so a capability
that ships on the CLI but not the API/MCP (or the reverse) fails this file instead of drifting.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re

import anyio
import pytest
from mcp import Client
from mcp.types import TextContent

from s2docs import surfaces

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

# The operations the task and docs/CLI_PLAN.md call out as the ones an agent or a human reaches from
# more than one door. Pinned so a row cannot be deleted to make the verifier quiet.
REQUIRED = {
    "build": ("start", "status", "log", "wait", "cancel", "list"),
    "deploy": ("push", "verify", "list"),
    "host": ("register", "claim", "release", "probe", "list"),
    "job": ("enqueue", "next", "start", "event", "finish", "verdict", "cancel", "list"),
    "evidence": ("add", "list"),
    "test": ("start", "step", "finish", "status"),
    "project": ("start", "update_step", "get", "list"),
    "asset": ("promote", "list", "get"),
    "live": ("act", "status", "capabilities"),
    "pipeline": ("run", "script"),
}


def _row(operation_id: str) -> surfaces.Operation:
    return next(op for op in surfaces.OPERATIONS if op.id == operation_id)


def test_the_table_parses() -> None:
    """Every row is complete and addressable: ids, summaries, surfaces and handles."""
    assert len(surfaces.OPERATIONS) >= 40, "the table lost rows — the whole point is coverage of the real services"
    ids = [op.id for op in surfaces.OPERATIONS]
    assert len(ids) == len(set(ids)), f"duplicate operation ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    for op in surfaces.OPERATIONS:
        assert SNAKE_CASE.fullmatch(op.id), f"{op.id!r} is not lowercase snake_case"
        assert op.summary and "\n" not in op.summary, f"{op.id}: the summary must be one line"
        assert 20 <= len(op.summary) <= 200, f"{op.id}: summary should be a sentence, got {len(op.summary)} chars"
        assert op.surfaces, f"{op.id}: an operation no surface exposes is dead code"
        assert op.surfaces <= set(surfaces.SURFACES), f"{op.id}: unknown surface in {sorted(op.surfaces)}"
        assert op.module.startswith("s2docs."), f"{op.id}: {op.module!r} is not a domain module"
        assert "\n" not in op.note, f"{op.id}: the note must be one line"


def test_the_table_covers_every_operation_it_promises() -> None:
    """Coverage is a claim too: the families the plan lists must all be rows."""
    ids = {op.id for op in surfaces.OPERATIONS}
    missing = sorted(f"{family}_{verb}" for family, verbs in REQUIRED.items() for verb in verbs
                     if f"{family}_{verb}" not in ids)
    assert not missing, f"operations the table must cover but does not: {missing}"


def test_every_row_names_a_domain_entry_that_exists() -> None:
    """`module.function` is where the surfaces are supposed to meet — it must be real and callable."""
    for op in surfaces.OPERATIONS:
        target = importlib.import_module(op.module)
        for part in op.function.split("."):
            target = getattr(target, part)  # AttributeError names the row's lie
        assert callable(target), f"{op.id}: {op.module}.{op.function} is not callable"


def test_no_row_declares_a_surface_it_does_not_have() -> None:
    """THE gate: the declared surfaces are the real ones, for every row, in both directions."""
    mismatches = surfaces.verify()
    assert mismatches == [], "the surface table and the code disagree: " + "; ".join(
        f"{m.operation} on {m.surface} is {m.kind} ({m.detail})" for m in mismatches
    )


def test_pipeline_is_honestly_cli_only() -> None:
    """The one family that must NOT be mirrored: no route, no tool — and the table says so."""
    for operation_id in ("pipeline_run", "pipeline_script"):
        op = _row(operation_id)
        assert op.surfaces == frozenset({"cli"}), f"{operation_id} grew a surface without a table change"
        assert not any(m.operation == operation_id for m in surfaces.verify()), (
            f"{operation_id} is declared CLI-only but a route or tool of that name exists"
        )


def test_a_wrong_entry_is_detected_missing() -> None:
    """A row that promises a surface the code does not have is drift — the verifier must say so."""
    good = _row("build_start")
    wrong = dataclasses.replace(good, mcp="no_such_tool")
    mismatches = surfaces.verify([wrong])
    assert [(m.operation, m.surface, m.kind) for m in mismatches] == [("build_start", "mcp", "missing")]


def test_a_wrong_entry_is_detected_undeclared() -> None:
    """The reverse direction: a surface that exposes the operation must be declared."""
    good = _row("build_start")
    wrong = dataclasses.replace(good, surfaces=frozenset({"cli", "api"}))  # the tool really exists
    mismatches = surfaces.verify([wrong])
    assert [(m.operation, m.surface, m.kind) for m in mismatches] == [("build_start", "mcp", "undeclared")]


def test_a_cli_only_row_that_gains_a_surface_is_detected() -> None:
    """Marking a CLI-only row as API-exposed claims a route that is not there."""
    wrong = dataclasses.replace(_row("pipeline_run"), surfaces=frozenset({"cli", "api"}))
    mismatches = surfaces.verify([wrong])
    assert [(m.operation, m.surface, m.kind) for m in mismatches] == [("pipeline_run", "api", "missing")]


def test_a_row_that_declares_a_surface_without_a_handle_is_refused() -> None:
    """An unverifiable row is worse than a missing one: it would pass the gate by being unchecked."""
    good = _row("build_start")
    with pytest.raises(ValueError, match="carries no mcp handle"):
        dataclasses.replace(good, mcp=None)
    with pytest.raises(ValueError, match="unknown surface"):
        dataclasses.replace(good, surfaces=frozenset({"cli", "grpc"}))


def test_report_is_json_ready() -> None:
    """`s2docs surfaces --json` and `surfaces_list` return the same shape."""
    payload = surfaces.report()
    assert len(payload["operations"]) == len(surfaces.OPERATIONS)
    assert payload["mismatches"] == []
    row = next(r for r in payload["operations"] if r["id"] == "host_claim")
    assert row["surfaces"] == ["cli", "api", "mcp"]
    assert row["cli"] == "s2docs host lock"  # the CLI spells claim as lock
    assert row["api"] == "POST /api/hosts/{host_id}/lock"
    assert row["domain"] == "s2docs.hosts.claim_host"


def test_cli_lists_the_table_and_exits_1_on_gaps(monkeypatch, capsys) -> None:
    """`s2docs surfaces` is a listing; `--gaps` is the CI gate and must fail loudly."""
    from s2docs import cli

    assert cli.main(["surfaces"]) == 0
    listing = capsys.readouterr().out
    assert "build_start" in listing and "pipeline_run" in listing and "host lock" in listing

    assert cli.main(["surfaces", "--gaps"]) == 0
    assert "no gaps" in capsys.readouterr().out

    gap = surfaces.Mismatch(operation="build_start", surface="api", kind="missing", detail="POST /api/x does not exist")
    monkeypatch.setattr(surfaces, "verify", lambda *a, **k: [gap])
    assert cli.main(["surfaces", "--gaps"]) == 1
    reported = capsys.readouterr().out
    assert "build_start" in reported and "missing" in reported

    assert cli.main(["surfaces", "--gaps", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "s2docs/v1" and payload["gaps"][0]["surface"] == "api"


def test_cli_json_flag_before_the_subcommand_also_works(capsys) -> None:
    """The global `--json` is the documented spelling; the subcommand must not reset it."""
    from s2docs import cli

    assert cli.main(["--json", "surfaces"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "s2docs/v1"
    assert {row["id"] for row in payload["operations"]} >= {"build_start", "pipeline_run"}


def test_mcp_surfaces_list_is_read_only_and_returns_the_table(tmp_path) -> None:
    """An MCP-only session must be able to see the map, and it must be a read tool."""
    from s2docs.db.connection import Database
    from s2docs.mcp_server import create_mcp_server

    server = create_mcp_server(Database(tmp_path / "no-db.db"))

    async def main():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            result = await client.call_tool("surfaces_list", {})
            return tools, result

    tools, result = anyio.run(main)
    assert "surfaces_list" in tools
    annotations = tools["surfaces_list"].annotations
    assert annotations is not None and annotations.read_only_hint is True
    # A dict-returning tool (like live_plan) answers as JSON text, not as structured content.
    payload = result.structured_content
    if payload is None:
        block = result.content[0]
        assert isinstance(block, TextContent), f"unexpected content block: {type(block).__name__}"
        payload = json.loads(block.text)
    assert {row["id"] for row in payload["operations"]} == {op.id for op in surfaces.OPERATIONS}
    assert payload["mismatches"] == []
