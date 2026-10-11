"""MCP server exposing the Source 2 documentation index to coding agents.

Run over stdio (`s2docs mcp`) for desktop clients, or mounted into the FastAPI
app at `/mcp` (streamable HTTP) by `s2docs serve`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument
from mcp_types import ToolAnnotations
from pydantic import Field

from s2docs import models as m
from s2docs.config import GAMES
from s2docs.db.connection import Database, DatabaseNotInitialized
from s2docs.db.repository import NotFound, Repository
from s2docs.prompts import PROMPTS, PromptError, render_prompt

T = TypeVar("T")

GameId = Annotated[str, Field(description=f"Game id: one of {', '.join(GAMES)}")]
Side = Literal["server", "client"]

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


def create_mcp_server(db: Database) -> MCPServer:
    server = MCPServer(name="s2docs", title="Source 2 Docs", instructions=INSTRUCTIONS, version="0.1.0")

    def run(fn: Callable[[Repository], T]) -> T:
        """Open a read-only connection, run `fn`, map domain errors to tool errors."""
        try:
            with db.reader() as conn:
                return fn(Repository(conn))
        except (NotFound, DatabaseNotInitialized, ValueError) as exc:
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
