"""MCP server exposing the Source 2 documentation index to coding agents.

Run over stdio (`s2docs mcp`) for desktop clients, or mounted into the FastAPI
app at `/mcp` (streamable HTTP) by `s2docs serve`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument
from mcp_types import ToolAnnotations
from pydantic import Field

from s2docs import builds, deploys, evidence, hosts, jobs, test_runs
from s2docs import models as m
from s2docs.build import workspace as ws
from s2docs.build.validate_cfg import validate_cfg as _validate_cfg
from s2docs.build.validate_script import ToolchainMissing
from s2docs.build.validate_script import validate_cs_script as _validate_cs_script
from s2docs.config import GAMES, get_settings, server_identity
from s2docs.db.connection import Database, DatabaseNotInitialized
from s2docs.db.repository import NotFound, Repository
from s2docs.orchestration.engine import OrchestrationError, Orchestrator, workflow_info
from s2docs.orchestration.workflows import WORKFLOWS
from s2docs.prompts import PROMPTS, PromptError, render_prompt

T = TypeVar("T")

GameId = Annotated[str, Field(description=f"Game id: one of {', '.join(GAMES)}")]
Side = Literal["server", "client"]
AddonName = Annotated[str, Field(description="Addon name: lowercase [a-z0-9_], e.g. 'jump_arena'")]
ProjectId = Annotated[str, Field(description="Project id from start_project, e.g. 'p-1a2b3c4d'")]
RelPath = Annotated[str, Field(description="Path inside the addon, e.g. 'scripts/jump_arena.js'")]
HostId = Annotated[str, Field(description="Host id from list_hosts, e.g. 'desktop-pc'")]
Owner = Annotated[
    str,
    Field(description="Who is asking: workstream/session id, e.g. 'ws/occluder-wire', 'p-1a2b3c4d', 'coord-session'"),
]
JobId = Annotated[str, Field(description="Job id from job_enqueue/job_list, e.g. 'j-1a2b3c4d'")]
JobLevel = Literal["info", "warn", "error", "crash", "screenshot", "milestone", "learning"]

INSTRUCTIONS = """\
Reference data for Valve's Source 2 engine games (Counter-Strike 2 = `cs2`, Deadlock = `deadlock`),
extracted from each game build: console variables/commands, Hammer entities (keyvalues, inputs, outputs,
merged with Valve's hand-written Hammer FGD labels/help/defaults), the engine schema (C++ classes, fields,
enums, VData), game events, Pulse bindings, CS2's official cs_script (JavaScript) API from point_script.d.ts,
Valve's cs_script sample scripts, and community docs from source2.wiki (CC BY-SA 4.0 — cite the doc URL).

Workflow: start with `search_docs`, then fetch exact details with the matching get_* tool. Treat these
results as the source of truth for names and signatures. Do not invent convars, entity I/O or API
methods that the tools do not return. The prompts (`cs2_script`, `hammer_io_logic`, `server_config`,
`deadlock_mod`, `cs2_map_rebuild`, `explain_symbol`) are guided workflows for common tasks.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITES_WORKSPACE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def create_mcp_server(db: Database, workspace: Path | None = None) -> MCPServer:
    workspace = workspace or get_settings().workspace_dir
    server = MCPServer(name="s2docs", title="Source 2 Docs", instructions=INSTRUCTIONS, version="0.1.0")

    def run(fn: Callable[[Repository], T]) -> T:
        """Open a read-only connection, run `fn`, map domain errors to tool errors."""
        try:
            with db.reader() as conn:
                return fn(Repository(conn))
        except (NotFound, DatabaseNotInitialized, ValueError, ToolchainMissing) as exc:
            raise ToolError(str(exc)) from exc

    def _ws(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (ws.WorkspaceError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    def check_game(game: str) -> None:
        if game not in GAMES:
            raise ToolError(f"unknown game {game!r}; use one of: {', '.join(GAMES)}")

    # ------------------------------------------------------------------ tools
    @server.tool(annotations=READ_ONLY)
    def list_games() -> list[m.GameInfo]:
        """List supported games with their currently indexed build and patch version."""
        return run(lambda r: r.games())

    @server.tool(annotations=READ_ONLY)
    def server_info() -> m.ServerInfo:
        """This server's DB identity: db_path, data_dir, pid, started_at and version, plus status.

        Compare it against the other server's `GET /api/health` before writing so two servers pointed
        at different data dirs (a split store) are caught instead of silently diverging."""
        return m.ServerInfo(**server_identity())

    @server.tool(annotations=READ_ONLY)
    def search_docs(
        query: Annotated[str, Field(description="Free text or identifier, e.g. 'round time', 'OnTrigger'")],
        game: Annotated[str | None, Field(description="Restrict to one game id")] = None,
        kinds: Annotated[
            list[str] | None, Field(description=f"Restrict result kinds: {', '.join(m.SEARCH_KINDS)}")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 15,
    ) -> m.Page[m.SearchHit]:
        """Full-text search across everything. Each hit's `key` feeds the detail tool for its `kind`:
        convar/command→get_convar(name), entity→get_entity('side:classname'), schema_class/schema_enum→
        get_schema_class('module:Name'), event→get_game_event, script→get_script_api, pulse→search_pulse,
        example→get_example(key), doc→get_doc(key). With `game` set, general docs are included too."""
        if game:
            check_game(game)
        return run(lambda r: r.search(query, game=game, kinds=kinds, limit=limit))

    @server.tool(annotations=READ_ONLY)
    def get_convar(game: GameId, name: Annotated[str, Field(description="Exact convar/command name")]) -> m.ConVar:
        """Get a console variable or command: default, min/max, flags (cheat, replicated, ...) and help text."""
        check_game(game)
        return run(lambda r: r.get_convar(game, name))

    @server.tool(annotations=READ_ONLY)
    def list_convars(
        game: GameId,
        query: Annotated[str | None, Field(description="Substring of name or description")] = None,
        kind: Literal["convar", "command"] | None = None,
        flag: Annotated[str | None, Field(description="Only entries with this flag, e.g. 'cheat'")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> m.Page[m.ConVar]:
        """List console variables/commands, filtered by substring and/or flag."""
        check_game(game)
        return run(lambda r: r.list_convars(game, q=query, kind=kind, flag=flag, limit=limit, offset=offset))

    @server.tool(annotations=READ_ONLY)
    def get_entity(
        game: GameId,
        classname: Annotated[str, Field(description="Hammer classname, or 'side:classname' from a search key")],
        side: Side = "server",
        include_inherited: bool = True,
    ) -> m.Entity:
        """Get a Hammer entity: keyvalues (with choices), inputs and outputs, including inherited ones.
        `defined_in` shows which base declares each member."""
        check_game(game)
        if ":" in classname:
            side_part, classname = classname.split(":", 1)
            if side_part in ("server", "client"):
                side = side_part  # type: ignore[assignment]
        return run(lambda r: r.get_entity(game, classname, side=side, inherited=include_inherited))

    @server.tool(annotations=READ_ONLY)
    def find_entities_with_io(
        game: GameId,
        name: Annotated[str, Field(description="Input or output name, e.g. 'OnTrigger', 'SetHealth'")],
        side: Side = "server",
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> m.Page[m.EntitySummary]:
        """Find entity classes (including base classes) that declare an input or output with this name.
        Subclasses inherit it too, so check candidates with get_entity."""
        check_game(game)
        return run(lambda r: r.list_entities(game, side=side, io=name, include_base=True, limit=limit))

    @server.tool(annotations=READ_ONLY)
    def get_schema_class(
        game: GameId,
        name: Annotated[str, Field(description="Class/enum name, or 'module:Name' from a search key")],
        module: Annotated[str | None, Field(description="Schema module, e.g. server, client")] = None,
        include_inherited: bool = True,
    ) -> m.SchemaClass:
        """Get an engine schema class or enum: fields with types, defaults and metadata, the inheritance chain
        and direct subclasses. Use it for networked fields, VData keys (Deadlock abilities) and plugin offsets."""
        check_game(game)
        if ":" in name and module is None and "::" not in name.split(":", 1)[0]:
            module, name = name.split(":", 1)
        return run(lambda r: r.get_schema_class(game, name, module=module, inherited=include_inherited))

    @server.tool(annotations=READ_ONLY)
    def list_schema_classes(
        game: GameId,
        query: Annotated[str | None, Field(description="Substring of the class name")] = None,
        module: str | None = None,
        kind: Literal["class", "enum"] | None = None,
        has_field: Annotated[str | None, Field(description="Only classes that declare this field")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> m.Page[m.SchemaClassSummary]:
        """List schema classes/enums by name substring, module, kind or declared field."""
        check_game(game)
        return run(
            lambda r: r.list_schema(
                game, q=query, module=module, kind=kind, field=has_field, limit=limit, offset=offset
            )
        )

    @server.tool(annotations=READ_ONLY)
    def get_game_event(
        game: GameId, name: Annotated[str, Field(description="Event name, e.g. player_death")]
    ) -> m.GameEvent:
        """Get a game event's fields and types (the effective definition after core/game/mod overrides)."""
        check_game(game)
        return run(lambda r: r.get_event(game, name))

    @server.tool(annotations=READ_ONLY)
    def list_game_events(
        game: GameId,
        query: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> m.Page[m.GameEvent]:
        """List game events, optionally filtered by a substring of the name or description."""
        check_game(game)
        return run(lambda r: r.list_events(game, q=query, limit=limit, offset=offset))

    @server.tool(annotations=READ_ONLY)
    def get_script_api(
        symbol: Annotated[
            str,
            Field(description="Type ('CSPlayerPawn'), member ('Domain.TraceLine'), or bare member name ('TraceLine')"),
        ],
        game: GameId = "cs2",
    ) -> m.ScriptType | list[m.ScriptSymbol]:
        """Look up the CS2 cs_script (point_script) JavaScript API. A type name returns the type with all
        members, including inherited ones. Anything else returns the matching members with signature and docs.
        `Instance` is the `Domain` type."""
        check_game(game)
        if symbol == "Instance":
            symbol = "Domain"

        def lookup(r: Repository) -> m.ScriptType | list[m.ScriptSymbol]:
            if "." not in symbol:
                try:
                    return r.get_script_type(game, symbol)
                except NotFound:
                    pass
            found = r.find_script_symbols(game, symbol)
            if not found:
                raise NotFound(f"no cs_script symbol matching {symbol!r}")
            return found

        return run(lookup)

    @server.tool(annotations=READ_ONLY)
    def list_script_types(game: GameId = "cs2") -> list[m.ScriptSymbol]:
        """List every top-level cs_script type: the Instance const, classes, interfaces, enums and type aliases."""
        check_game(game)
        return run(lambda r: r.list_script_types(game))

    @server.tool(annotations=READ_ONLY)
    def search_pulse(
        game: GameId,
        query: Annotated[str | None, Field(description="Substring of binding name, library or friendly name")] = None,
        library: Annotated[str | None, Field(description="Exact library, e.g. CBaseEntity_API")] = None,
        kind: Literal["input", "output", "function", "class"] | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> m.Page[m.PulseBinding]:
        """Search Pulse (Valve's visual scripting) node bindings: entity inputs/outputs and functions."""
        check_game(game)
        return run(lambda r: r.list_pulse(game, q=query, library=library, kind=kind, limit=limit))

    @server.tool(annotations=READ_ONLY)
    def diff_builds(
        game: GameId,
        from_build: Annotated[int | None, Field(description="Older build number; default = previous build")] = None,
        to_build: Annotated[int | None, Field(description="Newer build number; default = current build")] = None,
    ) -> m.BuildDiff:
        """Show what changed between two ingested builds, at member granularity: one row per added/
        removed/changed schema field, entity member, game-event field, script symbol, convar, Pulse
        binding or Hammer class. Each entry carries `group` (the parent class/entity/event/library) so
        members cluster under it; `before`/`after` hold only that member's own value. `counts` gives the
        exact per-section added/removed/changed breakdown ({section: {added, removed, changed}}), taken
        BEFORE the per-section `limit` cap, so it stays true even when `sections` is truncated to the
        first `limit` entries; `totals[section]` is the sum of that section's three counts. Use it after
        a game patch to find code or configs that might break."""
        check_game(game)
        return run(lambda r: r.diff_builds(game, from_build, to_build))

    @server.tool(annotations=READ_ONLY)
    def list_examples(game: GameId = "cs2") -> list[m.Example]:
        """List example scripts (Valve's cs_script_demo samples) with the API each one uses."""
        check_game(game)
        return run(lambda r: r.list_examples(game))

    @server.tool(annotations=READ_ONLY)
    def get_example(
        name: Annotated[str, Field(description="Example name, e.g. 'trace.js' or 'cs_script/trace.js'")],
        game: GameId = "cs2",
    ) -> m.Example:
        """Get the full source of an example script. Prefer adapting these Valve-written patterns."""
        check_game(game)
        return run(lambda r: r.get_example(game, name))

    @server.tool(annotations=READ_ONLY)
    def get_doc(
        key: Annotated[str, Field(description="Doc key from search_docs (e.g. 's2wiki:/Basics') or URL")],
    ) -> m.Doc:
        """Get a community documentation page (source2.wiki) as Markdown, with its URL and license for citation."""
        return run(lambda r: r.get_doc(key))

    @server.tool(annotations=READ_ONLY)
    def list_docs(
        game: Annotated[str | None, Field(description="Game id; general Source 2 pages are always included")] = None,
        query: Annotated[str | None, Field(description="Substring of title or path")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> m.Page[m.Doc]:
        """Browse community documentation pages (titles, URLs) without bodies."""
        if game:
            check_game(game)
        return run(lambda r: r.list_docs(game=game, q=query, limit=limit))

    # ------------------------------------------------------------ build tools

    @server.tool(annotations=READ_ONLY)
    def validate_cs_script(
        code: Annotated[str | None, Field(description="JavaScript source to check")] = None,
        addon: Annotated[str | None, Field(description="Or: addon whose file to check")] = None,
        path: Annotated[str | None, Field(description="Path of the .js file inside the addon")] = None,
        game: GameId = "cs2",
    ) -> m.ScriptValidation:
        """Type-check a cs_script with the TypeScript compiler against this build's Valve point_script.d.ts.
        Errors with category `unknown_api` mean the method/property does not exist — fix before shipping.
        `needs_type_narrowing` gives the subclass to cast to. ALWAYS run this before handing a script to the user."""
        check_game(game)
        if code is None:
            if not (addon and path):
                raise ToolError("pass `code`, or `addon` + `path`")
            code = ws.read_file(workspace, game, addon, path)
        filename = Path(path).name if path else "script.js"
        return run(lambda r: _validate_cs_script(r, code, game=game, filename=filename))

    @server.tool(annotations=READ_ONLY)
    def validate_cfg(
        text: Annotated[str, Field(description="Contents of a .cfg file")],
        game: GameId,
        context: Annotated[
            Literal["server", "client", "any"], Field(description="Where the cfg runs (affects warnings)")
        ] = "server",
    ) -> m.CfgValidation:
        """Check every command in a .cfg against the indexed convars/commands: unknown names (with suggestions),
        out-of-range values, cheat/devonly flags, and client-vs-server context."""
        check_game(game)
        return run(lambda r: _validate_cfg(r, text, game=game, context=context))

    @server.tool(annotations=WRITES_WORKSPACE)
    def scaffold_addon(name: AddonName, game: GameId = "cs2", with_script: bool = True) -> m.Addon:
        """Create a new addon in the local workspace (content/csgo_addons/<name>/ with scripts/, maps/,
        materials/, ...), a starter cs_script, Valve's point_script.d.ts + tsconfig.json, and a README with
        the Hammer setup steps. Fails if the addon already exists."""
        check_game(game)
        return run(lambda r: ws.scaffold(workspace, r, game=game, name=name, with_script=with_script))

    @server.tool(annotations=READ_ONLY)
    def list_addons(game: GameId = "cs2") -> list[str]:
        """List addons in the local workspace."""
        check_game(game)
        return _ws(lambda: ws.list_addons(workspace, game))

    @server.tool(annotations=READ_ONLY)
    def get_addon(name: AddonName, game: GameId = "cs2") -> m.Addon:
        """Show an addon's local root and file list."""
        check_game(game)
        return _ws(lambda: ws.describe(workspace, game, name))

    @server.tool(annotations=READ_ONLY)
    def read_addon_file(name: AddonName, path: RelPath, game: GameId = "cs2") -> str:
        """Read a text file from an addon in the workspace."""
        check_game(game)
        return _ws(lambda: ws.read_file(workspace, game, name, path))

    @server.tool(annotations=WRITES_WORKSPACE)
    def write_addon_file(
        name: AddonName, path: RelPath, content: str, game: GameId = "cs2", overwrite: bool = False
    ) -> m.AddonFile:
        """Write a text source file (.js, .cfg, .vmat, .vdata, panorama .xml/.css, ...) into an addon.
        Binary/compiled assets are produced by Valve's tools, not written here. Use overwrite=true to replace."""
        check_game(game)
        return _ws(lambda: ws.write_file(workspace, game, name, path, content, overwrite=overwrite))

    # ----------------------------------------------------------- orchestration
    orch = Orchestrator(db, workspace)

    def _orch(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (OrchestrationError, NotFound, ws.WorkspaceError, ToolchainMissing) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def list_workflows(
        game: Annotated[str | None, Field(description="Filter by game id")] = None,
    ) -> list[m.WorkflowInfo]:
        """List orchestration workflows: multi-step recipes (research → design → build → validate → test →
        document) that chain this server's tools and prompts. Start one with start_project."""
        return [workflow_info(w) for w in WORKFLOWS.values() if game is None or game in w.games]

    @server.tool(annotations=WRITES_WORKSPACE)
    def start_project(
        workflow: Annotated[str, Field(description="Workflow id from list_workflows, or 'custom'")],
        goal: Annotated[str, Field(description="What to build, in the user's words")],
        game: GameId = "cs2",
        addon: Annotated[str | None, Field(description="Addon name (default: derived from goal)")] = None,
        inputs: Annotated[dict[str, str] | None, Field(description="Workflow inputs (see list_workflows)")] = None,
        steps: Annotated[list[m.CustomStep] | None, Field(description="Steps for workflow='custom'")] = None,
        title: str | None = None,
    ) -> m.ProjectBrief:
        """Start a persistent orchestration project and get the first ready step(s). Progress is stored on the
        server, so any later session can continue with next_step(project_id)."""
        return _orch(
            lambda: orch.start(workflow, goal, game=game, addon=addon, inputs=inputs, custom_steps=steps, title=title)
        )

    @server.tool(annotations=READ_ONLY)
    def next_step(project_id: ProjectId) -> m.ProjectBrief:
        """Get the ready step(s) of a project: instructions, tools to use, rendered prompt text and the gate the
        server will check when the step is marked done."""
        return _orch(lambda: orch.brief(project_id))

    @server.tool(annotations=WRITES_WORKSPACE)
    def update_step(
        project_id: ProjectId,
        step_id: str,
        status: Literal["in_progress", "done", "skipped", "blocked", "pending"],
        notes: Annotated[str | None, Field(description="What was done / why skipped or blocked")] = None,
        artifacts: Annotated[list[str] | None, Field(description="File paths, URLs, key findings")] = None,
    ) -> m.ProjectBrief:
        """Record progress on a step and get the next brief. Marking 'done' runs the step's gate (e.g. all addon
        scripts must type-check); if it fails you get the problems back — fix them and retry."""
        return _orch(lambda: orch.update(project_id, step_id, status, notes=notes, artifacts=artifacts))

    @server.tool(annotations=READ_ONLY)
    def get_project(project_id: ProjectId) -> m.Project:
        """Full project state: every step's status, notes, artifacts and recent events."""
        return _orch(lambda: orch.get(project_id))

    @server.tool(annotations=READ_ONLY)
    def list_projects(status: Literal["active", "done", "abandoned"] | None = None) -> list[m.ProjectSummary]:
        """List orchestration projects (most recently updated first)."""
        return _orch(lambda: orch.list(status))

    @server.tool(annotations=READ_ONLY)
    def get_prompt_text(
        name: Annotated[str, Field(description=f"Prompt name: {', '.join(PROMPTS)}")],
        arguments: Annotated[dict[str, str] | None, Field(description="Prompt arguments")] = None,
    ) -> str:
        """Render one of this server's prompts as text, for clients that can't fetch MCP prompts mid-task."""
        return _orch(lambda: orch.prompt_text(name, arguments or {}))

    # ------------------------------------------------------------------ hosts
    # Local lock/registry mutations: DB-only writes that are safe to repeat (idempotent upserts).
    HOSTS_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
    # Writers whose retry duplicates a side effect (a second evidence row / a second test session).
    NON_IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
    # Tools that act on a remote host over SSH are open-world; the ones that overwrite/terminate there
    # are destructive. build_start/deploy_push are non-idempotent: a retry launches/copies again.
    REMOTE_WRITE = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    )
    # build_cancel terminates a remote process (destructive) but re-cancelling is idempotent.
    REMOTE_ACT = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    )
    # deploy_verify re-reads MD5s over SSH: open-world, idempotent, non-destructive.
    REMOTE_CHECK = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )
    HOST_PROBE = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )

    def _hosts(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except hosts.HostError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def list_hosts() -> list[m.Host]:
        """List registered test/build hosts with their facts (shell, GPU, paths) and current lock state.
        CHECK THIS before driving any host: a fresh lock means another session/workstream owns it.
        Each lock carries `phase` (what the owner is doing), `phase_age_sec` and `stuck` — the SERVER
        marks a lock STUCK when its heartbeat is fresh but the phase has not moved past the configured
        threshold (liveness is not progress). Treat `lock.stuck=true` as "the owner may be hung"."""
        return _hosts(lambda: hosts.list_hosts(db))

    @server.tool(annotations=READ_ONLY)
    def get_host(host_id: HostId) -> m.Host:
        """One host's registry entry: ssh target, shell flavor, probed facts, and lock."""
        return _hosts(lambda: hosts.get_host(db, host_id))

    @server.tool(annotations=HOSTS_WRITE)
    def register_host(
        host_id: Annotated[str, Field(description="Short id: lowercase [a-z0-9-], e.g. 'desktop-pc'")],
        ssh_target: Annotated[
            str, Field(description="ssh alias or user@host, e.g. 'pomps-pc' or 'pompa@100.64.130.72'")
        ],
        hostname: Annotated[str | None, Field(description="Machine name, e.g. 'DESKTOP-39NF657'")] = None,
        ssh_key: Annotated[str | None, Field(description="Identity file path if not the ssh default")] = None,
        shell: Annotated[
            Literal["cmd", "powershell"] | None, Field(description="Login shell flavor on the host")
        ] = None,
        os: Annotated[str | None, Field(description="e.g. 'windows'")] = None,
        notes: str | None = None,
    ) -> m.Host:
        """Register (or update) a test/build host. Upsert: omitted fields keep their previous values."""
        return _hosts(
            lambda: hosts.register_host(
                db, host_id, ssh_target, hostname=hostname, ssh_key=ssh_key, shell=shell, os=os, notes=notes
            )
        )

    @server.tool(annotations=HOST_PROBE)
    def probe_host(host_id: HostId) -> m.Host:
        """SSH to the host, run the probe script (GPU, RAM, processes, CS2 path/build) and store the facts.
        Use before planning work on a host — e.g. to see if a game or a heavy GPU process is running."""
        return _hosts(lambda: hosts.probe_host(db, host_id))

    @server.tool(annotations=HOSTS_WRITE)
    def claim_host(
        host_id: HostId,
        owner: Owner,
        ttl_sec: Annotated[int, Field(description="Lock lifetime without heartbeat, seconds (min 60)")] = 1800,
        force: Annotated[bool, Field(description="Take over even if another owner's lock is still fresh")] = False,
        phase: Annotated[
            str | None, Field(description="What you are doing, e.g. 'compile', 'test' — powers stuck detection")
        ] = None,
        note: Annotated[str | None, Field(description="Free text for the dashboard, e.g. the current step")] = None,
    ) -> m.HostLock:
        """Claim exclusive use of a host. Fails if another owner holds a fresh lock — a stale lock
        (heartbeat older than its TTL, e.g. from a crashed worker) is taken over automatically.
        Always report a `phase` (what you are doing); the server flags a lock STUCK when its heartbeat
        is fresh but the phase has not moved for too long, so keep the phase current (pass a new phase
        to heartbeat_host) as you progress. Heartbeat while you work; release with release_host when done."""
        return _hosts(
            lambda: hosts.claim_host(db, host_id, owner, ttl_sec=ttl_sec, force=force, phase=phase, note=note)
        )

    @server.tool(annotations=HOSTS_WRITE)
    def heartbeat_host(
        host_id: HostId,
        owner: Owner,
        phase: Annotated[
            str | None, Field(description="New phase; pass the same value to keep it, a new one to reset the clock")
        ] = None,
        note: Annotated[str | None, Field(description="Free text for the dashboard")] = None,
    ) -> m.HostLock:
        """Refresh your lock's heartbeat so it doesn't go stale mid-task. Heartbeating the SAME phase
        does not clear stuck-ness (that is the point); report a new `phase` as your work moves on."""
        return _hosts(lambda: hosts.heartbeat_host(db, host_id, owner, phase=phase, note=note))

    @server.tool(annotations=HOSTS_WRITE)
    def release_host(
        host_id: HostId,
        owner: Owner,
        force: Annotated[bool, Field(description="Release another owner's lock (cleanup of dead owners)")] = False,
    ) -> str:
        """Release a host lock. Always call this when finished with a host."""
        return _hosts(lambda: hosts.release_host(db, host_id, owner, force=force))

    # --------------------------------------------------------------- builds
    def _runs(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (builds.BuildError, deploys.DeployError, hosts.HostError, NotFound) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=REMOTE_WRITE)
    def build_start(
        host_id: HostId,
        command: Annotated[
            str, Field(description="Shell command to run on the host over SSH (detaching/hiding is its job)")
        ],
        kind: Annotated[str, Field(description="Label for the run, e.g. 'build', 'resourcecompiler'")] = "build",
        log_path: Annotated[str | None, Field(description="Server-local path for the captured log")] = None,
        notes: str | None = None,
    ) -> m.BuildRun:
        """Start a server-owned build run on a host: acquires the host lease, runs the command over SSH and
        returns a run id immediately. Do NOT poll with sleeps — use build_wait(run_id) or build_status. The
        host lease is released automatically when the run reaches a terminal status. The lease's phase is
        the run `kind` (e.g. 'compile'), so a run whose compile hangs while its heartbeat stays fresh reads
        STUCK on list_hosts — the server, not the client, decides that."""
        return _runs(lambda: builds.start(db, host_id, command, kind=kind, log_path=log_path, notes=notes))

    @server.tool(annotations=READ_ONLY)
    def build_status(
        run_id: Annotated[str, Field(description="Run id from build_start, e.g. 'b-1a2b3c4d'")],
    ) -> m.BuildRun:
        """One build run's record: status, exit code, parsed summary counts, material guard and artifacts.

        `warnings` carries the MATERIAL GUARD findings when the log referenced missing/reserved materials
        (Valve's `_vrf/` subtree, or another map's `maps/<other>/` subtree): counts per kind plus the first
        few offending paths. Such a run can still be 'succeeded' — the resourcecompiler exits 0 and the map
        then error-renders as a red/black checkerboard, so treat a non-None `warnings` as NOT clean."""
        return _runs(lambda: builds.get_run(db, run_id))

    @server.tool(annotations=READ_ONLY)
    def build_log(
        run_id: str,
        lines: Annotated[int, Field(ge=1, le=5000, description="Tail size in lines")] = 200,
    ) -> m.BuildLogTail:
        """Tail a build run's log. The log is UTF-16 (resourcecompiler) — this decodes it for you,
        returning the detected `encoding`, whether the tail was truncated, and any MATERIAL GUARD
        `warnings` found in the full log (see build_status)."""
        return _runs(lambda: builds.log_tail(db, run_id, lines=lines))

    @server.tool(annotations=READ_ONLY)
    def build_wait(
        run_id: str,
        timeout_sec: Annotated[int, Field(ge=1, description="Give up after this many seconds (exit 7)")] = 3600,
        strict_materials: Annotated[
            bool, Field(description="When true, fail the call if the material guard flagged warnings")
        ] = False,
    ) -> m.BuildRun:
        """Block (server-side, no client sleep loop) until the run finishes, then return its record.
        Raises if it is still running after timeout_sec. With strict_materials, a run whose log has
        material-guard warnings is an error even though its status is 'succeeded'."""
        run = builds.wait(db, run_id, timeout_sec=timeout_sec)
        guard = run.warnings
        if strict_materials and guard is not None and not guard.clean:
            counts = ", ".join(f"{k}={v}" for k, v in guard.counts.items() if v)
            raise ToolError(f"material guard: {counts} — rebuild is not clean (missing/reserved materials)")
        return run

    @server.tool(annotations=REMOTE_ACT)
    def build_cancel(run_id: str) -> m.BuildRun:
        """Cancel a run: mark it cancelled and release its host lease (terminates the process when this
        server owns it)."""
        return _runs(lambda: builds.cancel(db, run_id))

    @server.tool(annotations=READ_ONLY)
    def build_list(
        host_id: Annotated[str | None, Field(description="Restrict to one host")] = None,
        active: Annotated[bool, Field(description="Only queued/running runs")] = False,
    ) -> list[m.BuildRun]:
        """Recent build runs (newest first), optionally one host and/or active only."""
        return _runs(lambda: builds.list_runs(db, host_id=host_id, active=active))

    # -------------------------------------------------------------- deploys
    @server.tool(annotations=REMOTE_WRITE)
    def deploy_push(
        host_id: HostId,
        dest_root: Annotated[str, Field(description="Remote destination root, e.g. 'C:/iso_build/addons'")],
        files: Annotated[list[m.DeployFile], Field(description="Manifest: {path, local} entries to copy")],
        addon: str | None = None,
        parallel: Annotated[int, Field(ge=1, le=16)] = 1,
        dry_run: bool = False,
        verify: Annotated[bool, Field(description="Re-check MD5s on the host after copying")] = True,
        owner: Annotated[
            str | None,
            Field(
                description=(
                    "Your host-lock owner if you already hold the lease (the push reuses it and leaves it "
                    "locked). Inside an orchestration project pass 'project:<project_id>' — the project holds "
                    "the lease for the whole run"
                )
            ),
        ] = None,
    ) -> m.Deployment:
        """Copy files to a host and verify what landed (Get-FileHash over SSH). Returns a deploy id whose
        entries carry the server-checked md5 + verified flags. Call deploy_verify later to re-check.

        The push takes the host lease for the copy+verify window: a host held by another owner is a
        conflict (pass that owner if it is you), a free host is claimed as 'deploy:<id>' and released
        when the copy finishes. Inside an orchestration project the project already holds the lease as
        'project:<project_id>', so pass that as `owner` (the `cs2_live_test` deploy step does)."""
        return _runs(
            lambda: deploys.push(
                db,
                host_id,
                dest_root,
                files,
                addon=addon,
                parallel=parallel,
                dry_run=dry_run,
                verify=verify,
                owner=owner,
            )
        )

    @server.tool(annotations=REMOTE_CHECK)
    def deploy_verify(deploy_id: Annotated[str, Field(description="Deploy id from deploy_push")]) -> m.Deployment:
        """Re-check a recorded deployment's MD5s on the host and update its status."""
        return _runs(lambda: deploys.verify(db, deploy_id))

    @server.tool(annotations=READ_ONLY)
    def deploy_list(
        host_id: Annotated[str | None, Field(description="Restrict to one host")] = None,
    ) -> list[m.Deployment]:
        """Recent deployments (newest first), optionally one host."""
        return _runs(lambda: deploys.list_deployments(db, host_id=host_id))

    # -------------------------------------------------------------- evidence
    def _ev(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (evidence.EvidenceError, test_runs.TestRunError, hosts.HostError, NotFound) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=NON_IDEMPOTENT)
    def add_evidence(
        project_id: ProjectId,
        step_id: Annotated[str, Field(description="Project step the evidence proves (e.g. 'research')")],
        kind: m.EvidenceKind,
        ref: Annotated[str | None, Field(description="Path / url / command the evidence points at")] = None,
        data: Annotated[
            dict | None,
            Field(description="Structured detail: e.g. {'pixel_diff_pct': 1.2, 'noise_floor': 0.5} or {'md5': '...'}"),
        ] = None,
    ) -> m.Evidence:
        """Record structured evidence for a step. This is what the `evidence` gate checks — a step with an
        evidence gate CANNOT be marked done until at least one non-note row exists for it. Use kind='note'
        only for a supplementary remark (it does not satisfy the gate)."""
        return _ev(lambda: evidence.add(db, kind=kind, project_id=project_id, step_id=step_id, ref=ref, data=data))

    @server.tool(annotations=READ_ONLY)
    def list_evidence(
        project_id: Annotated[str | None, Field(description="Restrict to one project")] = None,
        step_id: Annotated[str | None, Field(description="Restrict to one step")] = None,
        kind: m.EvidenceKind | None = None,
    ) -> list[m.Evidence]:
        """List evidence rows (newest first), optionally filtered by project, step and/or kind."""
        return _ev(lambda: evidence.list_evidence(db, project_id=project_id, step_id=step_id, kind=kind))

    # ------------------------------------------------------------- test runs
    @server.tool(annotations=NON_IDEMPOTENT)
    def test_start(
        name: Annotated[str, Field(description="Label shown on the HUD strip, e.g. 'iso_test'")],
        host_id: Annotated[str | None, Field(description="Host to claim for the session (optional)")] = None,
        project_id: Annotated[str | None, Field(description="Project this session belongs to (optional)")] = None,
    ) -> m.TestRun:
        """Start a server-side test session (state=running). Claims the host lease when a host is given.
        Mirror the state onto the in-game TEST STATUS HUD yourself via `iso_test start` over netcon."""
        return _ev(lambda: test_runs.start(db, name, host_id=host_id, project_id=project_id))

    @server.tool(annotations=HOSTS_WRITE)
    def test_step(
        run_id: Annotated[str, Field(description="Run id from test_start, e.g. 't-1a2b3c4d'")],
        text: Annotated[str, Field(description="Current step label, e.g. 'wall-occlusion probe'")],
    ) -> m.TestRun:
        """Set the running session's current step (the HUD strip's `iso_test step`). Running runs only."""
        return _ev(lambda: test_runs.step(db, run_id, text))

    @server.tool(annotations=HOSTS_WRITE)
    def test_finish(
        run_id: str,
        result: Literal["pass", "fail"],
        note: Annotated[str | None, Field(description="What happened / the failure")] = None,
    ) -> m.TestRun:
        """Finish a running session pass/fail (the HUD's `iso_test pass|fail`): releases the host lease and
        records a console-transcript evidence row on the linked project when there is one."""
        return _ev(lambda: test_runs.finish(db, run_id, result, note=note))

    @server.tool(annotations=HOSTS_WRITE)
    def test_clear(run_id: str) -> m.TestRun:
        """Dismiss a session (`iso_test off`): state=idle, releasing a still-held host lease."""
        return _ev(lambda: test_runs.clear(db, run_id))

    @server.tool(annotations=READ_ONLY)
    def test_status(
        run_id: Annotated[
            str | None, Field(description="Run id; omit for the current (most recent active) run")
        ] = None,
    ) -> m.TestRun:
        """One test run's record, or the current active run when no id is given."""

        def _status() -> m.TestRun:
            run = test_runs.get(db, run_id) if run_id else test_runs.current(db)
            if run is None:
                raise test_runs.TestRunError("no active test run")
            return run

        return _ev(_status)

    @server.tool(annotations=READ_ONLY)
    def list_test_runs(
        state: Annotated[m.TestRunState | None, Field(description="Restrict to one state")] = None,
    ) -> list[m.TestRun]:
        """Recent test sessions (newest first), optionally filtered by state."""
        return _ev(lambda: test_runs.list_runs(db, state=state))

    # ------------------------------------------------------------------ jobs
    # A job is the one record for a requested test/demo/final run: intake, timeline, artifacts and a
    # mandatory close-out summary. Jobs never claim the host lease — the canonical tools do.
    # Seed/order writes (assign, link) are idempotent upserts; everything that appends a record or
    # launches work is NON_IDEMPOTENT, so a retry is never silently a second job/event/artifact.
    JOBS_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

    def _jobs(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (jobs.JobError, hosts.HostError, NotFound, OSError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY)
    def job_list(
        status: Annotated[str | None, Field(description="A status, or 'active' = queued+running")] = None,
        host_id: Annotated[str | None, Field(description="Restrict to one host")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[m.Job]:
        """Jobs newest first. `status='active'` means queued + running. Each row carries error/crash counts."""
        return _jobs(lambda: jobs.list_jobs(db, status=status, host_id=host_id, limit=limit))

    @server.tool(annotations=READ_ONLY)
    def job_get(job_id: JobId) -> m.Job:
        """One job with its full timeline (events), links to test runs/builds/deploys/evidence and artifacts."""
        return _jobs(lambda: jobs.get(db, job_id))

    @server.tool(annotations=READ_ONLY)
    def job_screenshots(
        job_id: JobId,
        step: Annotated[str | None, Field(description="Filter by step id ('s4'); 'unassigned' = untagged")] = None,
        reason: Annotated[str | None, Field(description="ready | periodic | verdict | request | verify")] = None,
    ) -> list[m.JobScreenshot]:
        """A job's screenshots tied to their step and verdict (seq, step, reason, url, web_url, verdict,
        dimensions) — the image context a reviewer pulls after a run. Filter by `step`/`reason`."""
        return _jobs(lambda: jobs.screenshots(db, job_id, step=step, reason=reason))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_enqueue(
        kind: Annotated[str, Field(description="test | demo | final")],
        title: Annotated[str, Field(description="Human name (the HUD strip / banner label)")],
        target: Annotated[str, Field(description="What is under test: feature/test name + branch@sha")],
        purpose: Annotated[str, Field(description="One sentence: why this run exists")],
        expected: Annotated[str, Field(description="Pass criterion (an observable outcome)")],
        host_id: Annotated[str | None, Field(description="Host to queue on, or set host_choice='ask'")] = None,
        host_choice: Annotated[
            str | None, Field(description="'ask' = stays queued + unassigned until assigned")
        ] = None,
        max_minutes: Annotated[int | None, Field(description="Cap; test/demo default 5, larger needs `reason`")] = None,
        reason: Annotated[str | None, Field(description="Why the run may exceed the 5-minute default")] = None,
        needs_confirm: Annotated[
            bool, Field(description="True = the human iso_confirm gate decides pass/fail")
        ] = False,
        screenshots: Annotated[str, Field(description="each_step (default) | on_request | none")] = "each_step",
        watching: Annotated[bool, Field(description="The user is watching this run live")] = False,
        branch: Annotated[str | None, Field(description="Branch under test")] = None,
        commit: Annotated[str | None, Field(description="Commit sha under test")] = None,
        cleanup: Annotated[list[str] | None, Field(description="Things that must be gone at the end")] = None,
        requested_by: Annotated[str | None, Field(description="Coordinator session id")] = None,
        project_id: Annotated[str | None, Field(description="Project this job belongs to")] = None,
        origin: Annotated[
            str | None,
            Field(
                description=(
                    "Who asked for this run: human | agent | driver | eval | probe | warmup "
                    "(default agent). The learning loop reads the ledger by origin, so tag synthetic "
                    "runs (driver/eval/probe/warmup) — an untagged repeat inflates a training artifact."
                )
            ),
        ] = None,
    ) -> m.Job:
        """Enqueue a job (validated intake -> queued). The intake is the gate: every missing required field
        comes back in one error with a field list. Set `host_choice='ask'` to queue unassigned."""
        return _jobs(
            lambda: jobs.enqueue(
                db,
                kind=kind,
                title=title,
                target=target,
                purpose=purpose,
                expected=expected,
                host_id=host_id,
                host_choice=host_choice,
                max_minutes=max_minutes,
                reason=reason,
                needs_confirm=needs_confirm,
                screenshots=screenshots,
                watching=watching,
                branch=branch,
                commit=commit,
                cleanup=cleanup,
                requested_by=requested_by,
                project_id=project_id,
                origin=origin,
            )
        )

    @server.tool(annotations=READ_ONLY)
    def job_next(host_id: HostId) -> m.Job:
        """The head queued job for a host, only when that host's lease is free. Errors (with the holder)
        when a fresh lock blocks it, and when the host's queue is empty."""
        return _jobs(lambda: jobs.next_job(db, host_id))

    @server.tool(annotations=JOBS_WRITE)
    def job_assign(
        job_id: JobId,
        host_id: HostId,
        position: Annotated[int | None, Field(description="Queue order (ascending); omit to keep the current")] = None,
    ) -> m.Job:
        """Set (or move) a queued job's host and queue order."""
        return _jobs(lambda: jobs.assign(db, job_id, host_id, position=position))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_start(
        job_id: JobId,
        worker: Annotated[str, Field(description="Subagent id / branch running it, e.g. 'ws/occluder-wire'")],
    ) -> m.Job:
        """queued -> running, recording the worker and started_at. A second call is a conflict."""
        return _jobs(lambda: jobs.start(db, job_id, worker=worker))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_event(
        job_id: JobId,
        level: JobLevel,
        message: Annotated[str, Field(description="What happened (the timeline line)")],
        phase: Annotated[str | None, Field(description="Sets the job's current phase when given")] = None,
        data: Annotated[
            dict | None,
            Field(description="JSON detail: log_tail (capped 64 KB) for error/crash, exit_code, url, md5, test_run_id"),
        ] = None,
    ) -> m.JobEvent:
        """Append one timeline event. `phase` updates the job's current phase; `error`/`crash` require a
        message and SHOULD carry `data.log_tail` so the job page can expand the failure in a <pre>."""
        return _jobs(lambda: jobs.event(db, job_id, level=level, message=message, phase=phase, data=data))

    def _attach(job_id: str, path: str, name: str | None) -> m.JobArtifact:
        src = Path(path).expanduser()
        if not src.is_file():
            raise jobs.JobValidationError(f"no such file {path!r}", ["path"])
        ctype = jobs.content_type_for(src.name)
        if ctype is None:
            raise jobs.JobValidationError(
                f"unsupported file type {src.suffix!r}; use a png/jpg/txt/log/json/zip/dmp/mdmp/evtx file", ["path"]
            )
        return jobs.save_artifact(db, job_id, name or src.name, src.read_bytes(), ctype)

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_attach(
        job_id: JobId,
        path: Annotated[str, Field(description="Local (Mac) file to copy into the job's artifacts")],
        name: Annotated[str | None, Field(description="Stored name; default = the file's basename")] = None,
    ) -> m.JobArtifact:
        """Copy a local file into the job's artifacts (MCP runs on the Mac). Append-only: no overwrite.
        Images also record a `screenshot` event automatically."""
        return _jobs(lambda: _attach(job_id, path, name))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_request_screenshot(
        job_id: JobId,
        note: Annotated[str | None, Field(description="What to capture, e.g. 'the HUD after the wall test'")] = None,
    ) -> m.Job:
        """Ask the runner for a screenshot: an info event plus `params.pending_shot=true`. The runner
        polls the job and answers by uploading an artifact, which clears the flag."""
        return _jobs(lambda: jobs.request_screenshot(db, job_id, note=note))

    @server.tool(annotations=JOBS_WRITE)
    def job_link(
        job_id: JobId,
        kind: Annotated[str, Field(description="test_run | build | deploy | evidence | project")],
        ref_id: Annotated[str, Field(description="Id of the test run / build / deploy / evidence / project")],
    ) -> m.Job:
        """Attach a canonical record the job is about (a test run, build, deploy, evidence row or project)."""
        return _jobs(lambda: jobs.link(db, job_id, kind=kind, ref_id=ref_id))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_verdict(
        job_id: JobId,
        verdict: m.Verdict,
        notes: Annotated[str | None, Field(description="Free-text note recorded with the verdict")] = None,
        step: Annotated[str | None, Field(description="Step id/phase the verdict answers (optional)")] = None,
        source: Annotated[
            str,
            Field(
                description=(
                    "Where the verdict came from: 'coordinator' when relaying one the user typed in the "
                    "Hermes chat, else chat | console | key | web"
                )
            ),
        ] = "web",
    ) -> m.Job:
        """Answer the running job's current step with a human verdict (pass | fail | skip). Writes a
        human-confirm evidence row through the same path as `POST /api/evidence` (so the confirm push
        files fire) and appends a `milestone` event. The job must be running and link a test run, else
        the call is a conflict."""
        return _jobs(lambda: jobs.verdict(db, job_id, verdict=verdict, notes=notes, step=step, source=source))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_finish(
        job_id: JobId,
        status: Annotated[
            str, Field(description="passed | failed | error | cancelled | inconclusive")
        ],
        summary: Annotated[
            dict,
            Field(description="Close-out summary, ALL FOUR required and <=600 chars each: {what, why, learned, next}"),
        ],
    ) -> m.Job:
        """Close out a running/queued job. Gate: `summary` must carry what, why, learned and next, each
        non-empty and <=600 chars; otherwise the call is rejected with the offending field names.
        `status='inconclusive'` is the honest close-out for a run that ended without a pass/fail verdict
        (a step-gated demo the user stopped answering) — it is NOT a pass."""
        return _jobs(lambda: jobs.finish(db, job_id, status=status, summary=summary))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_restatus(
        job_id: JobId,
        status: Annotated[
            str, Field(description="passed | failed | error | cancelled | inconclusive")
        ],
        reason: Annotated[str, Field(description="Why the ledger is being corrected (recorded on the timeline)")],
    ) -> m.Job:
        """Correct a FINISHED job's terminal status (terminal -> terminal), for a mis-recorded close-out
        — e.g. a run that finished 'passed' but never got a verdict. `reason` is mandatory and lands as a
        `milestone` timeline event. The job must already be terminal (a queued/running job finishes or is
        cancelled) and the target must differ from the current status, else the call is a conflict."""
        return _jobs(lambda: jobs.restatus(db, job_id, status=status, reason=reason))

    @server.tool(annotations=NON_IDEMPOTENT)
    def job_cancel(
        job_id: JobId,
        reason: Annotated[str | None, Field(description="Why it was cancelled (recorded on the timeline)")] = None,
    ) -> m.Job:
        """queued/running -> cancelled; the reason is recorded as a timeline event."""
        return _jobs(lambda: jobs.cancel(db, job_id, reason=reason))

    # -------------------------------------------------------------- resources
    @server.resource(
        "s2docs://cs2/cs_script/point_script.d.ts",
        name="point_script.d.ts",
        description="Valve's TypeScript declarations for cs_script. Copy next to your scripts for IntelliSense.",
        mime_type="application/typescript",
    )
    def point_script_dts() -> str:
        return run(lambda r: r.raw_file("cs2", "cs_script/point_script.d.ts"))

    @server.resource(
        "s2docs://cs2/cs_script/tsconfig.json",
        name="cs_script tsconfig.json",
        description="Valve's tsconfig.json for cs_script editing (target ES version used by CS2).",
        mime_type="application/json",
    )
    def cs_script_tsconfig() -> str:
        return run(lambda r: r.raw_file("cs2", "cs_script/tsconfig.json"))

    @server.resource(
        "s2docs://cs2/cs_script/examples/{name}",
        name="cs_script example",
        description="Valve's cs_script sample scripts (hello.js, trace.js, input.js, ...).",
        mime_type="text/javascript",
    )
    def cs_script_example(name: str) -> str:
        return run(lambda r: r.get_example("cs2", name).content or "")

    # ---------------------------------------------------------------- prompts
    for spec in PROMPTS.values():
        server.add_prompt(
            Prompt(
                name=spec.name,
                title=spec.title,
                description=spec.description,
                arguments=[
                    PromptArgument(name=a.name, description=a.description, required=a.required) for a in spec.arguments
                ],
                fn=_prompt_fn(db, spec.name),
                context_kwarg=None,
            )
        )

    return server


def _prompt_fn(db: Database, name: str) -> Callable[..., str]:
    def render(**kwargs: str) -> str:
        try:
            with db.reader() as conn:
                return render_prompt(Repository(conn), name, kwargs)
        except (PromptError, DatabaseNotInitialized) as exc:
            raise ValueError(str(exc)) from exc

    return render
