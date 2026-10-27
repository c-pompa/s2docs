"""The app's named operations, and which surface (CLI, HTTP API, MCP) exposes each one.

`s2docs` has three hand-wired surfaces over the same domain modules: `cli.py` (argparse
subcommands), `api/routes.py` (FastAPI routes) and `mcp_server.py` (MCP tools). Nothing recorded
which operation is exposed where, so the surfaces drifted in silence — after the live-sandbox
merge the MCP tools were five behind the CLI and no test noticed. `live/service.py` already has
the right shape for *behaviour* (one `act()`/`status()` the three surfaces call); this module is
that shape applied to the *inventory*: one row per operation, the set of surfaces that expose it,
and `verify()` to prove the row against the code that is actually importable.

Read it as the answer to "where can I do X?". A row is a claim, and the claim is checked in both
directions: a declared surface that is missing is drift, and a surface that exposes the operation
without declaring it is drift too. `s2docs surfaces --gaps` and the `surfaces_list` MCP tool print
the same verdict, so a session that only has MCP can see what the server cannot do yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Surface = Literal["cli", "api", "mcp"]
SURFACES: tuple[Surface, ...] = ("cli", "api", "mcp")
ALL_SURFACES: frozenset[Surface] = frozenset(SURFACES)


@dataclass(frozen=True)
class Operation:
    """One named operation: what it is, the domain function behind it, and where it is exposed.

    `cli`, `api` and `mcp` are the operation's *canonical handle* on each surface, and they are
    filled in even for a surface `surfaces` does not declare. That is deliberate: "this capability
    is CLI-only" is only an honest claim if the route and the tool of that name really do not
    exist, so the verifier needs the spelling to check the absence. `surfaces` is the claim; the
    handles are how it is proven and disproven.

    `module`/`function` name the one domain implementation the surfaces are supposed to share
    (`s2docs.builds.start`, `s2docs.orchestration.engine.Orchestrator.start`); `function` may be a
    dotted attribute path. A surface that reimplements the operation instead of calling it is
    invisible here — the table records what the code *says*, and the tests pin the rest.
    """

    id: str
    summary: str
    module: str
    function: str
    surfaces: frozenset[Surface]
    cli: tuple[str, ...] = ()  # argv after `s2docs`, e.g. ("build", "start")
    api: tuple[str, str] | None = None  # (HTTP method, path), e.g. ("POST", "/api/builds")
    mcp: str | None = None  # registered MCP tool name
    note: str = ""

    def __post_init__(self) -> None:
        """Refuse a row that cannot be checked: every declared surface needs its handle.

        Without this a typo like `surfaces={"cli", "api", "mcpp"}` or a row that declares `mcp`
        but forgets the tool name would pass the verifier by being unverifiable.
        """
        unknown = {s for s in self.surfaces if s not in SURFACES}
        if unknown:
            raise ValueError(f"{self.id}: unknown surface(s) {sorted(unknown)}; use a subset of {SURFACES}")
        if not self.surfaces:
            raise ValueError(f"{self.id}: declares no surface at all — an operation nobody can reach is dead code")
        for surface, handle in (("cli", self.cli), ("api", self.api), ("mcp", self.mcp)):
            if surface in self.surfaces and not handle:
                raise ValueError(f"{self.id}: declares {surface!r} but carries no {surface} handle")

    def as_dict(self) -> dict:
        """JSON-ready row (schema `s2docs/v1`), with the handles spelled the way users type them."""
        return {
            "id": self.id,
            "summary": self.summary,
            "domain": f"{self.module}.{self.function}",
            "surfaces": [s for s in SURFACES if s in self.surfaces],
            "cli": " ".join(("s2docs", *self.cli)) or None,
            "api": f"{self.api[0]} {self.api[1]}" if self.api else None,
            "mcp": self.mcp,
            "note": self.note or None,
        }


# ---------------------------------------------------------------- the table
# One row per operation that an agent or a human is expected to reach from more than one door.
# Grouped by family; the ids are `<family>_<verb>` so a gap reads as "job_verdict is missing from
# the API" without a lookup. Add a row when a capability gains a surface — that is the point.
OPERATIONS: tuple[Operation, ...] = (
    # ------------------------------------------------------------------ builds
    Operation(
        id="build_start",
        summary="Start a server-owned build run on a host and return its record",
        module="s2docs.builds", function="start",
        surfaces=ALL_SURFACES,
        cli=("build", "start"), api=("POST", "/api/hosts/{host_id}/builds"), mcp="build_start",
        note="claims the host lease as build:<run-id>; --wait blocks instead of returning at once",
    ),
    Operation(
        id="build_status",
        summary="One build run's record: status, exit code, summary counts, material guard",
        module="s2docs.builds", function="get_run",
        surfaces=ALL_SURFACES,
        cli=("build", "status"), api=("GET", "/api/builds/{run_id}"), mcp="build_status",
    ),
    Operation(
        id="build_log",
        summary="Tail a run's log, decoding the UTF-16 resourcecompiler output",
        module="s2docs.builds", function="log_tail",
        surfaces=ALL_SURFACES,
        cli=("build", "log"), api=("GET", "/api/builds/{run_id}/log"), mcp="build_log",
        note="--follow (CLI only) streams until the run is terminal; it is a client-side loop, not a service",
    ),
    Operation(
        id="build_wait",
        summary="Block until a run finishes, then return its record",
        module="s2docs.builds", function="wait",
        surfaces=ALL_SURFACES,
        cli=("build", "wait"), api=("POST", "/api/builds/{run_id}/wait"), mcp="build_wait",
        note="server-side wait; --strict-materials turns material-guard warnings into exit 8",
    ),
    Operation(
        id="build_cancel",
        summary="Cancel a run and release its host lease",
        module="s2docs.builds", function="cancel",
        surfaces=ALL_SURFACES,
        cli=("build", "cancel"), api=("POST", "/api/builds/{run_id}/cancel"), mcp="build_cancel",
    ),
    Operation(
        id="build_list",
        summary="Recent build runs, newest first, optionally one host or active only",
        module="s2docs.builds", function="list_runs",
        surfaces=ALL_SURFACES,
        cli=("build", "list"), api=("GET", "/api/builds"), mcp="build_list",
    ),
    # ----------------------------------------------------------------- deploys
    Operation(
        id="deploy_push",
        summary="Copy files to a host and verify the MD5s of what landed",
        module="s2docs.deploys", function="push",
        surfaces=ALL_SURFACES,
        cli=("deploy", "push"), api=("POST", "/api/deploys"), mcp="deploy_push",
        note="takes the host lease for the copy+verify window (deploy:<id> when the host is free)",
    ),
    Operation(
        id="deploy_verify",
        summary="Re-check a recorded deployment's MD5s on the host",
        module="s2docs.deploys", function="verify",
        surfaces=ALL_SURFACES,
        cli=("deploy", "verify"), api=("POST", "/api/deploys/{deploy_id}/verify"), mcp="deploy_verify",
    ),
    Operation(
        id="deploy_list",
        summary="Recent deployments, newest first, optionally one host",
        module="s2docs.deploys", function="list_deployments",
        surfaces=ALL_SURFACES,
        cli=("deploy", "list"), api=("GET", "/api/deploys"), mcp="deploy_list",
    ),
    # ------------------------------------------------------------------- hosts
    Operation(
        id="host_register",
        summary="Register or update a test/build host",
        module="s2docs.hosts", function="register_host",
        surfaces=ALL_SURFACES,
        cli=("host", "register"), api=("PUT", "/api/hosts/{host_id}"), mcp="register_host",
        note="upsert: omitted fields keep their previous values",
    ),
    Operation(
        id="host_claim",
        summary="Claim exclusive use of a host (a lease with an owner and a TTL)",
        module="s2docs.hosts", function="claim_host",
        surfaces=ALL_SURFACES,
        cli=("host", "lock"), api=("POST", "/api/hosts/{host_id}/lock"), mcp="claim_host",
        note="the CLI spells it `host lock`; a stale lock is taken over, a fresh one is a conflict",
    ),
    Operation(
        id="host_release",
        summary="Release a host lease",
        module="s2docs.hosts", function="release_host",
        surfaces=ALL_SURFACES,
        cli=("host", "unlock"), api=("DELETE", "/api/hosts/{host_id}/lock"), mcp="release_host",
        note="the CLI spells it `host unlock`",
    ),
    Operation(
        id="host_probe",
        summary="SSH-probe a host (GPU, RAM, processes, CS2) and store the facts",
        module="s2docs.hosts", function="probe_host",
        surfaces=ALL_SURFACES,
        cli=("host", "probe"), api=("POST", "/api/hosts/{host_id}/probe"), mcp="probe_host",
    ),
    Operation(
        id="host_list",
        summary="List hosts with their facts and current lock state",
        module="s2docs.hosts", function="list_hosts",
        surfaces=ALL_SURFACES,
        cli=("host", "list"), api=("GET", "/api/hosts"), mcp="list_hosts",
        note="each lock carries phase, phase_age_sec and stuck — liveness is not progress",
    ),
    # -------------------------------------------------------------------- jobs
    Operation(
        id="job_enqueue",
        summary="Enqueue a job (validated intake -> queued)",
        module="s2docs.jobs", function="enqueue",
        surfaces=ALL_SURFACES,
        cli=("job", "enqueue"), api=("POST", "/api/jobs"), mcp="job_enqueue",
        note="the intake gate reports every missing field in one error; jobs never claim the host lease",
    ),
    Operation(
        id="job_next",
        summary="The head queued job for a host, when that host's lease is free",
        module="s2docs.jobs", function="next_job",
        surfaces=ALL_SURFACES,
        cli=("job", "next"), api=("GET", "/api/jobs/next"), mcp="job_next",
    ),
    Operation(
        id="job_start",
        summary="queued -> running, recording the worker",
        module="s2docs.jobs", function="start",
        surfaces=ALL_SURFACES,
        cli=("job", "start"), api=("POST", "/api/jobs/{job_id}/start"), mcp="job_start",
    ),
    Operation(
        id="job_event",
        summary="Append one timeline event, optionally setting the job's phase",
        module="s2docs.jobs", function="event",
        surfaces=ALL_SURFACES,
        cli=("job", "event"), api=("POST", "/api/jobs/{job_id}/event"), mcp="job_event",
    ),
    Operation(
        id="job_finish",
        summary="Close out a job (what, why, learned, next are all required)",
        module="s2docs.jobs", function="finish",
        surfaces=ALL_SURFACES,
        cli=("job", "finish"), api=("POST", "/api/jobs/{job_id}/finish"), mcp="job_finish",
    ),
    Operation(
        id="job_verdict",
        summary="Answer the running job's step: pass | fail | skip",
        module="s2docs.jobs", function="verdict",
        surfaces=ALL_SURFACES,
        cli=("job", "verdict"), api=("POST", "/api/jobs/{job_id}/verdict"), mcp="job_verdict",
        note="one path with POST /api/evidence: it stores a human-confirm row, so the push files fire",
    ),
    Operation(
        id="job_cancel",
        summary="queued/running -> cancelled, recording the reason",
        module="s2docs.jobs", function="cancel",
        surfaces=ALL_SURFACES,
        cli=("job", "cancel"), api=("POST", "/api/jobs/{job_id}/cancel"), mcp="job_cancel",
    ),
    Operation(
        id="job_list",
        summary="Jobs newest first, optionally one status or host",
        module="s2docs.jobs", function="list_jobs",
        surfaces=ALL_SURFACES,
        cli=("job", "list"), api=("GET", "/api/jobs"), mcp="job_list",
        note="status='active' means queued + running",
    ),
    # ---------------------------------------------------------------- evidence
    Operation(
        id="evidence_add",
        summary="Record one evidence row for a project step",
        module="s2docs.evidence", function="add",
        surfaces=ALL_SURFACES,
        cli=("evidence", "add"), api=("POST", "/api/evidence"), mcp="add_evidence",
        note="the `evidence` gate ignores kind=note: a step with that gate needs a real row",
    ),
    Operation(
        id="evidence_list",
        summary="List evidence rows, newest first, filtered by project, step or kind",
        module="s2docs.evidence", function="list_evidence",
        surfaces=ALL_SURFACES,
        cli=("evidence", "list"), api=("GET", "/api/evidence"), mcp="list_evidence",
    ),
    # --------------------------------------------------------------- test runs
    Operation(
        id="test_start",
        summary="Start a test session and claim the host lease",
        module="s2docs.test_runs", function="start",
        surfaces=ALL_SURFACES,
        cli=("test", "start"), api=("POST", "/api/test-runs"), mcp="test_start",
    ),
    Operation(
        id="test_step",
        summary="Set the running session's current step",
        module="s2docs.test_runs", function="step",
        surfaces=ALL_SURFACES,
        cli=("test", "step"), api=("POST", "/api/test-runs/{run_id}/step"), mcp="test_step",
        note="renews the lease's heartbeat, so a long step cannot be mistaken for a dead owner",
    ),
    Operation(
        id="test_finish",
        summary="Finish a running session pass/fail and release the lease",
        module="s2docs.test_runs", function="finish",
        surfaces=ALL_SURFACES,
        cli=("test", "finish"), api=("POST", "/api/test-runs/{run_id}/finish"), mcp="test_finish",
    ),
    Operation(
        id="test_status",
        summary="One test run's record, or the current active run",
        module="s2docs.test_runs", function="get",
        surfaces=ALL_SURFACES,
        cli=("test", "status"), api=("GET", "/api/test-runs/{run_id}"), mcp="test_status",
        note="the API also serves GET /api/test-runs/current when no id is given",
    ),
    # ---------------------------------------------------------------- projects
    Operation(
        id="project_start",
        summary="Start a persistent orchestration project and get its first ready step",
        module="s2docs.orchestration.engine", function="Orchestrator.start",
        surfaces=frozenset({"api", "mcp"}),
        cli=("project", "start"), api=("POST", "/api/projects"), mcp="start_project",
        note="no `s2docs project` family ships: a project is created and driven over HTTP/MCP",
    ),
    Operation(
        id="project_update_step",
        summary="Record progress on a step (marking done runs the step's gate)",
        module="s2docs.orchestration.engine", function="Orchestrator.update",
        surfaces=frozenset({"api", "mcp"}),
        cli=("project", "update"),
        api=("POST", "/api/projects/{project_id}/steps/{step_id}"), mcp="update_step",
        note="a failing gate comes back as the problems to fix, not as a stored 'done'",
    ),
    Operation(
        id="project_get",
        summary="Full project state: every step's status, notes, artifacts and events",
        module="s2docs.orchestration.engine", function="Orchestrator.get",
        surfaces=frozenset({"api", "mcp"}),
        cli=("project", "get"), api=("GET", "/api/projects/{project_id}"), mcp="get_project",
    ),
    Operation(
        id="project_list",
        summary="List orchestration projects, most recently updated first",
        module="s2docs.orchestration.engine", function="Orchestrator.list",
        surfaces=frozenset({"api", "mcp"}),
        cli=("project", "list"), api=("GET", "/api/projects"), mcp="list_projects",
    ),
    # ------------------------------------------------------------------ assets
    Operation(
        id="asset_promote",
        summary="Promote a finished chain into the asset catalog",
        module="s2docs.assets", function="promote",
        surfaces=frozenset({"api", "mcp"}),
        cli=("asset", "promote"), api=("POST", "/api/assets"), mcp="asset_promote",
        note="the CLI `asset` family authors .vmdl/.vmat documents; it never touches the catalog",
    ),
    Operation(
        id="asset_list",
        summary="The catalog, newest first, optionally by kind and/or one tag",
        module="s2docs.assets", function="list_assets",
        surfaces=frozenset({"api", "mcp"}),
        cli=("asset", "list"), api=("GET", "/api/assets"), mcp="asset_list",
        note="retired assets are hidden unless include_retired is set",
    ),
    Operation(
        id="asset_get",
        summary="One catalog entry by id or slug",
        module="s2docs.assets", function="resolve",
        surfaces=frozenset({"api", "mcp"}),
        cli=("asset", "get"), api=("GET", "/api/assets/{asset_id}"), mcp="asset_get",
    ),
    # -------------------------------------------------------------------- live
    Operation(
        id="live_act",
        summary="Run an IntentEnvelope on the host's running CS2 and judge it by readback",
        module="s2docs.live.service", function="act",
        surfaces=ALL_SURFACES,
        cli=("live", "act"), api=("POST", "/api/live/actions"), mcp="live_act",
        note="'verified' is the only success that was checked; 'refused' means the sandbox is not loaded",
    ),
    Operation(
        id="live_status",
        summary="Is CS2 up on the host, which map and spawngroups are loaded",
        module="s2docs.live.service", function="status",
        surfaces=ALL_SURFACES,
        cli=("live", "status"), api=("GET", "/api/live/status"), mcp="live_status",
    ),
    Operation(
        id="live_capabilities",
        summary="Which experimental capabilities were measured on the host's current CS2 build",
        module="s2docs.live.service", function="capability_report",
        surfaces=ALL_SURFACES,
        cli=("live", "caps"), api=("GET", "/api/live/capabilities"), mcp="live_capabilities",
        note="the CLI spells it `live caps`; `unknown` means not probed on that build",
    ),
    # ---------------------------------------------------------------- pipeline
    Operation(
        id="pipeline_run",
        summary="Run the asset chain (prompt -> mesh -> asset -> build) as one job",
        module="s2docs.pipeline", function="run_asset_build_slice",
        surfaces=frozenset({"cli"}),
        cli=("pipeline", "run"), api=("POST", "/api/pipeline/run"), mcp="pipeline_run",
        note="CLI-only by design: it drives a local multi-stage job, and no route or tool of that "
             "name exists (the verifier asserts the absence)",
    ),
    Operation(
        id="pipeline_verdict",
        summary="Record a human visual verdict on a closed asset-chain job (inconclusive -> passed/failed)",
        module="s2docs.pipeline", function="record_verdict",
        surfaces=frozenset({"cli"}),
        cli=("pipeline", "verdict"), api=("POST", "/api/pipeline/verdict"), mcp="pipeline_verdict",
        note="CLI-only like pipeline_run: a pipeline job is a local chain record, not a server-side run. "
             "It is not `job_verdict` — that one answers a running step and needs a linked test run; "
             "this one attaches the look to a job that already closed inconclusive.",
    ),
    Operation(
        id="pipeline_script",
        summary="Run the text -> cs_script chain (generate, validate, compile, deploy) as one job",
        module="s2docs.pipeline", function="run_script_slice",
        surfaces=frozenset({"cli"}),
        cli=("pipeline", "script"), api=("POST", "/api/pipeline/script"), mcp="pipeline_script",
        note="CLI-only by design, like pipeline_run; build/deploy are real with --compile/"
             "--deploy-host, and only the in-game verdict is still blocked",
    ),
)


# ------------------------------------------------------------- verification
@dataclass(frozen=True)
class Mismatch:
    """One disagreement between a row's claim and a surface's real inventory."""

    operation: str
    surface: str
    kind: Literal["missing", "undeclared"]
    detail: str

    def as_dict(self) -> dict:
        return {"operation": self.operation, "surface": self.surface, "kind": self.kind, "detail": self.detail}


def _handle(op: Operation, surface: Surface) -> str:
    """The surface's spelling of this operation ('' when the row records no canonical handle)."""
    if surface == "cli":
        return " ".join(("s2docs", *op.cli))
    if surface == "api":
        return f"{op.api[0]} {op.api[1]}" if op.api else ""
    return op.mcp or ""


def _present(op: Operation, surface: Surface, *, commands: set, routes: set, tools: set) -> bool | None:
    """Is the operation on that surface? None when the row has no handle to look up."""
    if surface == "cli":
        return (op.cli in commands) if op.cli else None
    if surface == "api":
        return (op.api in routes) if op.api else None
    return (op.mcp in tools) if op.mcp else None


def _cli_commands() -> set[tuple[str, ...]]:
    """Every leaf command path the argparse parser has, e.g. ('build', 'start').

    Walks `s2docs.cli.build_parser()` — the same parser `main()` parses argv with — so a subcommand
    that exists only in the docs, or one renamed without a row, cannot pass as exposed.
    """
    import argparse

    from s2docs import cli

    commands: set[tuple[str, ...]] = set()

    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subparsers:
            if prefix:
                commands.add(prefix)  # a leaf: a family such as `build` alone runs nothing
            return
        for name, child in subparsers[0].choices.items():
            walk(child, (*prefix, name))

    walk(cli.build_parser(), ())
    return commands


def _api_routes() -> set[tuple[str, str]]:
    """Every (method, path) the real FastAPI app serves, e.g. ('POST', '/api/builds/{run_id}/wait').

    Read from the app's OpenAPI schema, not `app.routes`: FastAPI >= 0.14 keeps an included router as
    one lazy `_IncludedRouter` entry, so the route list is no longer flat, while the schema is the
    same contract an HTTP client sees on every supported version. Built through
    `create_app(mount_mcp=False)` so a router that exists but is never included reads as missing;
    MCP is verified on its own surface.
    """
    from s2docs.api.app import create_app

    app = create_app(mount_mcp=False)
    methods = {"get", "post", "put", "delete", "patch"}
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.lower() in methods
    }


def _mcp_tools() -> set[str]:
    """Every tool name the MCP server registers.

    `MCPServer.list_tools()` is async (it materialises protocol objects), and `verify()` has to work
    inside a running MCP request — the `surfaces_list` tool calls it — where `anyio.run` would fail.
    The tool manager's sync list is the registration inventory the protocol list is built from.
    """
    import tempfile
    from pathlib import Path

    from s2docs.db.connection import Database
    from s2docs.mcp_server import create_mcp_server

    # A path that is never opened: registering tools touches no database.
    db = Database(Path(tempfile.gettempdir()) / "s2docs-surfaces-verify.db")
    return {tool.name for tool in create_mcp_server(db)._tool_manager.list_tools()}


def verify(operations: Sequence[Operation] | None = None) -> list[Mismatch]:
    """Prove every row against the three surfaces, in both directions. Read-only; opens no database.

    - declared but absent -> `missing`: the table promises a door that is not there (the drift that
      let MCP fall behind the CLI).
    - present but undeclared -> `undeclared`: the capability leaked onto a surface without a row, so
      the table (and every reader of it) is now wrong about where it lives.

    Imports are lazy (`_cli_commands`, `_api_routes`, `_mcp_tools`) so `import s2docs.surfaces` stays
    cheap and this module can be read by a CLI that must not drag in FastAPI or the MCP SDK.
    """
    rows = OPERATIONS if operations is None else tuple(operations)
    commands, routes, tools = _cli_commands(), _api_routes(), _mcp_tools()
    mismatches: list[Mismatch] = []
    for op in rows:
        for surface in SURFACES:
            present = _present(op, surface, commands=commands, routes=routes, tools=tools)
            if present is None:
                continue
            declared = surface in op.surfaces
            if declared and not present:
                mismatches.append(Mismatch(op.id, surface, "missing",
                                           f"{_handle(op, surface)} is declared but does not exist"))
            elif present and not declared:
                mismatches.append(Mismatch(op.id, surface, "undeclared",
                                           f"{_handle(op, surface)} exists but the table does not declare it"))
    return mismatches


def report(operations: Sequence[Operation] | None = None) -> dict:
    """The table plus the verifier's verdict, JSON-ready: `s2docs surfaces --json`, `surfaces_list`."""
    rows = OPERATIONS if operations is None else tuple(operations)
    return {
        "operations": [op.as_dict() for op in rows],
        "mismatches": [m.as_dict() for m in verify(rows)],
    }
