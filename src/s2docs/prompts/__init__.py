"""Prompt registry shared by the MCP server and the HTTP API.

A prompt = metadata (name, args) + a Markdown template in `templates/` +
an optional `context` function that pulls grounding data from the DB.
Templates use `string.Template` syntax (`$task`, `${game_name}`), so they can be
edited without touching code. Missing optional args render as empty strings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from string import Template

from s2docs.config import GAMES
from s2docs.db.repository import NotFound, Repository


@dataclass(frozen=True, slots=True)
class PromptArg:
    name: str
    description: str
    required: bool = True


ContextFn = Callable[[Repository, dict[str, str]], dict[str, str]]


@dataclass(frozen=True, slots=True)
class PromptSpec:
    name: str
    title: str
    description: str
    template: str  # file name in templates/
    arguments: list[PromptArg] = field(default_factory=list)
    context: ContextFn | None = None


class PromptError(ValueError):
    pass


# ---------------------------------------------------------------- context fns
def _game_ctx(repo: Repository, args: dict[str, str]) -> dict[str, str]:
    gid = args.get("game") or "cs2"
    if gid not in GAMES:
        raise PromptError(f"unknown game {gid!r}; known: {', '.join(GAMES)}")
    game = GAMES[gid]
    build = next((g for g in repo.games() if g.id == gid), None)
    build_txt = (
        f"build {build.current_build} (patch {build.patch_version})"
        if build and build.current_build
        else "not ingested"
    )
    return {"game": gid, "game_name": game.name, "build": build_txt}


def _cs_script_ctx(repo: Repository, args: dict[str, str]) -> dict[str, str]:
    ctx = _game_ctx(repo, {**args, "game": "cs2"})
    try:
        domain = repo.get_script_type("cs2", "Domain", inherited=False)
        types = repo.list_script_types("cs2")
    except NotFound:
        ctx["api_index"] = "(cs_script API not ingested; run `s2docs sync --game cs2`)"
        return ctx
    lines = ["Instance (Domain) methods:"]
    seen: set[str] = set()
    for mem in domain.members:
        if mem.deprecated or mem.name in seen:
            continue
        seen.add(mem.name)
        first = (mem.doc or "").split("\n", 1)[0]
        lines.append(f"- {mem.name}: {first}" if first else f"- {mem.name}")
    groups: dict[str, list[str]] = {}
    for t in types:
        if t.name != "Domain" and t.kind != "const":
            groups.setdefault(t.kind, []).append(t.name + (f" extends {t.extends}" if t.extends else ""))
    for kind, names in groups.items():
        lines.append(f"\n{kind.capitalize()}s: " + ", ".join(names))
    ctx["api_index"] = "\n".join(lines)
    return ctx


def _search_ctx(repo: Repository, args: dict[str, str]) -> dict[str, str]:
    ctx = _game_ctx(repo, args)
    query = args.get("query", "")
    hits = repo.search(query, game=ctx["game"], limit=10).items if query else []
    ctx["hits"] = "\n".join(f"- [{h.kind}] {h.key} — {h.title}" for h in hits) or "(no index hits)"
    return ctx


def _orchestrate_ctx(repo: Repository, args: dict[str, str]) -> dict[str, str]:
    from s2docs.orchestration.workflows import WORKFLOWS

    ctx = _game_ctx(repo, args)
    lines = []
    for w in WORKFLOWS.values():
        if ctx["game"] in w.games:
            inputs = ", ".join(f"{i.name} (default: {i.default})" for i in w.inputs) or "none"
            lines.append(f"- `{w.id}`: {w.title}. {w.description} Steps: {len(w.steps)}. Inputs: {inputs}.")
    ctx["workflows"] = "\n".join(lines) + "\n- `custom`: your own steps."
    return ctx


def _cs_map_rebuild_ctx(repo: Repository, args: dict[str, str]) -> dict[str, str]:
    ctx = _game_ctx(repo, {**args, "game": "cs2"})
    ctx["map"] = args.get("map") or "(the addon's own map)"
    return ctx


# ------------------------------------------------------------------ registry
PROMPTS: dict[str, PromptSpec] = {
    p.name: p
    for p in [
        PromptSpec(
            name="cs2_script",
            title="Write a CS2 cs_script (point_script) script",
            description="Author a JavaScript cs_script for a CS2 map, grounded in the current point_script.d.ts API.",
            template="cs2_script.md",
            arguments=[
                PromptArg("task", "What the script should do"),
                PromptArg("map_context", "Relevant entities/targetnames already in the map", required=False),
            ],
            context=_cs_script_ctx,
        ),
        PromptSpec(
            name="hammer_io_logic",
            title="Design Hammer entity I/O logic",
            description="Build map logic from entities, keyvalues and input/output connections (no scripting).",
            template="hammer_io_logic.md",
            arguments=[
                PromptArg("goal", "The gameplay behaviour to build"),
                PromptArg("game", "cs2 or deadlock", required=False),
            ],
            context=_game_ctx,
        ),
        PromptSpec(
            name="server_config",
            title="Write a server/game-mode config",
            description="Produce a .cfg of convars/commands for a goal, verified against the convar index.",
            template="server_config.md",
            arguments=[
                PromptArg("goal", "e.g. '1v1 aim practice, infinite money, no buy time limit'"),
                PromptArg("game", "cs2 or deadlock", required=False),
            ],
            context=_game_ctx,
        ),
        PromptSpec(
            name="deadlock_mod",
            title="Plan a Deadlock mod",
            description="Plan a Deadlock addon (VPK) or server plugin using schema/VData and entity data.",
            template="deadlock_mod.md",
            arguments=[PromptArg("goal", "What the mod should change")],
            context=lambda repo, args: _game_ctx(repo, {**args, "game": "deadlock"}),
        ),
        PromptSpec(
            name="orchestrate",
            title="Orchestrate a multi-step build",
            description="Kick off a persistent, gated project chaining the research, prompt, build and validate tools.",
            template="orchestrate.md",
            arguments=[
                PromptArg("goal", "What to build, e.g. 'an isometric Diablo-style spectator view addon'"),
                PromptArg("game", "cs2 or deadlock", required=False),
            ],
            context=_orchestrate_ctx,
        ),
        PromptSpec(
            name="cs2_map_rebuild",
            title="Decompile + rebuild a CS2 map",
            description="A pre-flight checklist for decompile/rebuild work, baking in reserved paths, "
            "nodraw physics-surface materials and the material guard.",
            template="cs2_map_rebuild.md",
            arguments=[
                PromptArg("task", "What the rebuild should achieve, e.g. 'fix the missing materials on my iso view'"),
                PromptArg("map", "The target map name, e.g. 'de_dust2_iso'", required=False),
            ],
            context=_cs_map_rebuild_ctx,
        ),
        PromptSpec(
            name="explain_symbol",
            title="Explain an engine symbol",
            description="Explain a convar, entity, schema class, event or script API symbol with sources.",
            template="explain_symbol.md",
            arguments=[
                PromptArg("query", "Name or phrase to look up"),
                PromptArg("game", "cs2 or deadlock", required=False),
            ],
            context=_search_ctx,
        ),
    ]
}


def _template_text(filename: str) -> str:
    return (resources.files("s2docs.prompts") / "templates" / filename).read_text(encoding="utf-8")


def render_prompt(repo: Repository, name: str, args: dict[str, str] | None = None) -> str:
    spec = PROMPTS.get(name)
    if spec is None:
        raise PromptError(f"unknown prompt {name!r}; known: {', '.join(PROMPTS)}")
    args = {k: v for k, v in (args or {}).items() if v is not None}
    allowed = {a.name for a in spec.arguments}
    unknown = set(args) - allowed
    if unknown:
        raise PromptError(f"unknown arguments for {name}: {sorted(unknown)}")
    missing = [a.name for a in spec.arguments if a.required and not args.get(a.name)]
    if missing:
        raise PromptError(f"missing required arguments for {name}: {missing}")

    values = {a.name: "" for a in spec.arguments}
    values.update(args)
    if spec.context:
        values.update(spec.context(repo, values))
    return Template(_template_text(spec.template)).safe_substitute(values).strip()
