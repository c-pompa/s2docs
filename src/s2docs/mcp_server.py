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

from s2docs import models as m
from s2docs.build import workspace as ws
from s2docs.build.validate_cfg import validate_cfg as _validate_cfg
from s2docs.build.validate_script import ToolchainMissing
from s2docs.build.validate_script import validate_cs_script as _validate_cs_script
from s2docs.config import GAMES, get_settings
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

INSTRUCTIONS = """\
Reference data for Valve's Source 2 engine games (Counter-Strike 2 = `cs2`, Deadlock = `deadlock`),
extracted from each game build: console variables/commands, Hammer entities (keyvalues, inputs, outputs,
merged with Valve's hand-written Hammer FGD labels/help/defaults), the engine schema (C++ classes, fields,
enums, VData), game events, Pulse bindings, CS2's official cs_script (JavaScript) API from point_script.d.ts,
Valve's cs_script sample scripts, and community docs from source2.wiki (CC BY-SA 4.0 — cite the doc URL).

Workflow: start with `search_docs`, then fetch exact details with the matching get_* tool. Treat these
results as the source of truth for names and signatures. Do not invent convars, entity I/O or API
methods that the tools do not return. The prompts (`cs2_script`, `hammer_io_logic`, `server_config`,
`deadlock_mod`, `explain_symbol`) are guided workflows for common tasks.
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
        """Show what changed between two ingested builds: convars, entities, schema, events and script API.
        Use it after a game patch to find code or configs that might break."""
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
