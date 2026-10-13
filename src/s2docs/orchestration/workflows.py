"""Workflow registry: reusable, multi-step recipes that chain the MCP's tools and prompts.

Instructions are `string.Template` text; available variables: $goal, $game, $game_name, $addon,
plus any workflow-specific inputs. A step can name a prompt (rendered into the brief so the agent
gets it as tool output) and a `gate` the server checks before the step may be marked done.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Gates the engine knows how to check (see engine._check_gate).
GATES = {
    "addon_exists": "the addon has been scaffolded in the workspace",
    "scripts_typecheck": "every scripts/**/*.js file in the addon passes validate_cs_script with 0 errors",
    "cfgs_valid": "every cfg/**/*.cfg file in the addon passes validate_cfg with 0 errors",
    "evidence": "at least one non-note evidence row for the step must be recorded via add_evidence "
    "(console_transcript / file_hash / pixel_diff / screenshot / other) — notes alone don't pass",
}


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    name: str
    description: str
    required: bool = False
    default: str | None = None


@dataclass(frozen=True, slots=True)
class StepSpec:
    id: str
    title: str
    kind: str  # research | design | build | validate | test | document
    instructions: str
    tools: tuple[str, ...] = ()
    prompt: dict | None = None  # {"name": prompt_name, "args": {arg: template}}
    depends_on: tuple[str, ...] = ()
    gate: str | None = None
    done_when: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    id: str
    title: str
    description: str
    games: tuple[str, ...]
    steps: tuple[StepSpec, ...]
    inputs: tuple[WorkflowInput, ...] = field(default_factory=tuple)
    needs_addon: bool = True


# ------------------------------------------------------------- shared steps
def _scaffold(depends: tuple[str, ...]) -> StepSpec:
    return StepSpec(
        id="scaffold",
        title="Scaffold the addon",
        kind="build",
        instructions=(
            "Create the addon `$addon` with `scaffold_addon` (game `$game`). If it already exists, inspect it "
            "with `get_addon` instead and continue from its current files."
        ),
        tools=("scaffold_addon", "get_addon"),
        depends_on=depends,
        gate="addon_exists",
    )


def _validate(depends: tuple[str, ...]) -> StepSpec:
    return StepSpec(
        id="validate",
        title="Validate scripts and configs",
        kind="validate",
        instructions=(
            "Run `validate_cs_script` (addon `$addon`, each .js path) and `validate_cfg` for each .cfg. Fix every "
            "`unknown_api`, `syntax`, `type_error` and `possibly_undefined` error and re-run until clean. Resolve "
            "`needs_type_narrowing` warnings with the suggested cast. The server re-checks all files when you mark "
            "this step done."
        ),
        tools=("validate_cs_script", "validate_cfg", "read_addon_file", "write_addon_file", "get_script_api"),
        depends_on=depends,
        gate="scripts_typecheck",
    )


def _test(depends: tuple[str, ...]) -> StepSpec:
    return StepSpec(
        id="test_in_game",
        title="Test in CS2 Workshop Tools",
        kind="test",
        instructions=(
            "Test on a Windows host with CS2 Workshop Tools. At the start of a test session, ask the user which "
            "host to use, and recommend the idle one (no LM Studio or games running, low GPU use). Steps: copy "
            "`content/csgo_addons/$addon` to the host's CS2 install, launch Workshop Tools with addon `$addon`, "
            "open/build the map, and watch the console for `[$addon]` messages and script errors. Record exactly "
            "what you observed. If no host is available yet, give the user the exact manual steps and mark this "
            "step `blocked` with the reason."
        ),
        tools=("add_evidence",),
        depends_on=depends,
        gate="evidence",
        done_when=(
            "The behaviour was observed in game, or a concrete failure was logged and fed back into the build step."
        ),
    )


def _document(depends: tuple[str, ...]) -> StepSpec:
    return StepSpec(
        id="document",
        title="Document the addon",
        kind="document",
        instructions=(
            "Update the addon README (`write_addon_file` README.md, overwrite=true) with: what it does, the Hammer "
            "setup (entities, targetnames, keyvalues), console commands and inputs, known limitations (mark any "
            "@experimental API used), and the build number it was validated against (`list_games`)."
        ),
        tools=("write_addon_file", "list_games", "add_evidence"),
        depends_on=depends,
        gate="evidence",
    )


# ---------------------------------------------------------------- workflows
WORKFLOWS: dict[str, WorkflowSpec] = {}


def _register(w: WorkflowSpec) -> None:
    ids = [s.id for s in w.steps]
    assert len(ids) == len(set(ids)), f"duplicate step ids in {w.id}"
    for s in w.steps:
        assert set(s.depends_on) <= set(ids), f"{w.id}.{s.id}: unknown dependency"
        assert s.gate is None or s.gate in GATES, f"{w.id}.{s.id}: unknown gate {s.gate}"
    WORKFLOWS[w.id] = w


_register(
    WorkflowSpec(
        id="cs2_script_feature",
        title="CS2 gameplay script (cs_script)",
        description="Plan, build, validate and test a cs_script feature for a CS2 map.",
        games=("cs2",),
        steps=(
            StepSpec(
                id="research",
                title="Research the API surface",
                kind="research",
                instructions=(
                    "Goal: $goal\n\nFind every API piece you need before designing anything. Use `search_docs` "
                    "(kinds script, example, entity, event, doc) and `get_script_api` on each candidate type. Read "
                    "the closest Valve sample with `get_example`. List the exact methods, events and entities you "
                    "will use, and anything the goal needs that the API does NOT offer."
                ),
                tools=(
                    "search_docs",
                    "get_script_api",
                    "list_script_types",
                    "list_examples",
                    "get_example",
                    "get_entity",
                    "get_doc",
                    "add_evidence",
                ),
                gate="evidence",
            ),
            StepSpec(
                id="design",
                title="Design the script",
                kind="design",
                instructions=(
                    "Design the script(s) for: $goal. Keep to the API found in research. Define the file layout "
                    "(scripts/*.js), the callbacks, the state you keep, the map entities and targetnames, and the "
                    "inputs or commands for testing. The prompt below holds the authoring rules and the live API index."
                ),
                prompt={"name": "cs2_script", "args": {"task": "$goal"}},
                tools=("add_evidence",),
                depends_on=("research",),
                gate="evidence",
            ),
            _scaffold(("design",)),
            StepSpec(
                id="implement",
                title="Implement",
                kind="build",
                instructions=(
                    "Write the scripts into addon `$addon` with `write_addon_file` (scripts/*.js; overwrite=true "
                    "for the scaffolded starter). After each file, run `validate_cs_script` on it and fix it "
                    "right away."
                ),
                tools=("write_addon_file", "validate_cs_script", "get_script_api"),
                depends_on=("scaffold",),
            ),
            _validate(("implement",)),
            StepSpec(
                id="hammer_setup",
                title="Hammer setup",
                kind="design",
                instructions=(
                    "List the exact Hammer setup: point_script entities (targetname, `cs_script` = scripts/<file>.vjs) "
                    "and every other entity the script references. Confirm the keyvalues and I/O with `get_entity`. "
                    "Write this into the README."
                ),
                prompt={"name": "hammer_io_logic", "args": {"goal": "Map setup for: $goal", "game": "$game"}},
                tools=("get_entity", "find_entities_with_io", "write_addon_file", "add_evidence"),
                depends_on=("validate",),
                gate="evidence",
            ),
            _test(("hammer_setup",)),
            _document(("test_in_game",)),
        ),
    )
)

_register(
    WorkflowSpec(
        id="cs2_camera_addon",
        title="CS2 custom camera / viewer addon",
        description=(
            "A cs_script addon that drives a player's view through CustomPlayerCamera (follow / controlled "
            "modes), e.g. an isometric, top-down or cinematic spectator view of the match."
        ),
        games=("cs2",),
        inputs=(
            WorkflowInput(
                "view_style", "Camera style to aim for", default="isometric (ARPG-like, ~55° pitch, 45° yaw)"
            ),
            WorkflowInput("subject", "What the camera follows", default="a chosen player or bot, switchable"),
        ),
        steps=(
            StepSpec(
                id="research",
                title="Research the camera API",
                kind="research",
                instructions=(
                    "Goal: $goal (style: $view_style; subject: $subject).\n\nStart from `get_script_api` on "
                    "`CustomPlayerCamera`, `CameraFollowConfig`, `CustomCameraMode` and `CSPlayerPawn` (see "
                    "`GetCustomCamera`, `SetEyeAngles`). Note that the camera API is @experimental. Check which view "
                    "modes give fixed angles (CONTROLLED needs a camera entity whose origin and angles you set; "
                    "FOLLOW_POSITION leaves the angles to the player). Also look up `Teleport`, `GetEyeAngles`, "
                    "`SetThink`/`SetNextThink` for per-tick updates, `OnScriptInput`/`OnPlayerChat` for controls, and "
                    "`PointTemplate.ForceSpawn` for spawning a camera entity. Note that observers (CSObserverPawn) "
                    "have no custom camera. Search docs for 'camera'. Report the rig options and their trade-offs."
                ),
                tools=(
                    "get_script_api",
                    "search_docs",
                    "get_entity",
                    "list_examples",
                    "get_example",
                    "get_doc",
                    "add_evidence",
                ),
                gate="evidence",
            ),
            StepSpec(
                id="design_rig",
                title="Design the camera rig",
                kind="design",
                instructions=(
                    "Pick the rig and pin down the numbers: mode, pitch, yaw, distance, height, smoothing (lerp per "
                    "think), clipping, how the viewer pawn is kept out of the way (e.g. frozen/hidden or kept "
                    "alive away from play), how the subject is chosen and switched, and the zoom/rotate controls "
                    "(chat commands, `RunScriptInput` names). State your fallbacks for when the subject dies or "
                    "disconnects."
                ),
                prompt={"name": "cs2_script", "args": {"task": "$goal — camera rig: $view_style, following $subject"}},
                tools=("add_evidence",),
                depends_on=("research",),
                gate="evidence",
            ),
            _scaffold(("design_rig",)),
            StepSpec(
                id="implement_camera",
                title="Implement the camera script",
                kind="build",
                instructions=(
                    "Write scripts/$addon.js: get the viewer's camera via GetCustomCamera(), set the mode and follow "
                    "config (or drive a camera entity each think for fixed iso angles), apply smoothing, and handle "
                    "subject loss. Validate after every write."
                ),
                tools=("write_addon_file", "validate_cs_script", "get_script_api"),
                depends_on=("scaffold",),
            ),
            StepSpec(
                id="implement_controls",
                title="Implement viewer controls",
                kind="build",
                instructions=(
                    "Add the controls: next/previous subject, zoom in/out, rotate 90°, and toggle the camera off/on. "
                    "Use OnPlayerChat commands (e.g. '!cam next') and RunScriptInput names, and mention them in a "
                    "Msg on activate. Validate."
                ),
                tools=("write_addon_file", "validate_cs_script", "get_script_api"),
                depends_on=("implement_camera",),
            ),
            StepSpec(
                id="config",
                title="Test config",
                kind="build",
                instructions=(
                    "Write cfg/$addon.cfg for testing: warmup/offline settings and bots (bot_add, bot_quota), "
                    "infinite warmup, and anything else the viewer needs. Validate it with `validate_cfg` "
                    "(context server)."
                ),
                prompt={"name": "server_config", "args": {"goal": "Local test setup for $goal", "game": "$game"}},
                tools=("write_addon_file", "validate_cfg", "list_convars", "get_convar"),
                depends_on=("scaffold",),
                gate="cfgs_valid",
            ),
            _validate(("implement_controls", "config")),
            StepSpec(
                id="hammer_setup",
                title="Hammer setup",
                kind="design",
                instructions=(
                    "Describe the map setup: the point_script entity (cs_script = scripts/$addon.vjs), any camera "
                    "or point_template entities, and spawn points. Confirm each with `get_entity` and write it into "
                    "the README."
                ),
                tools=("get_entity", "write_addon_file", "add_evidence"),
                depends_on=("validate",),
                gate="evidence",
            ),
            _test(("hammer_setup",)),
            _document(("test_in_game",)),
        ),
    )
)

_register(
    WorkflowSpec(
        id="cs2_game_template",
        title="CS2 game-mode template",
        description=(
            "A reusable addon template for a custom game built on CS2: camera/view, control scheme, rules "
            "config, HUD (custom_hud_layout), map blockout and the script architecture. Use it as a base for "
            "new game modes, e.g. an isometric ARPG-style mode."
        ),
        games=("cs2",),
        inputs=(
            WorkflowInput("view_style", "Camera/view of the game", default="isometric"),
            WorkflowInput("core_loop", "One-sentence core gameplay loop", default="clear waves of bots in an arena"),
        ),
        steps=(
            StepSpec(
                id="research",
                title="Research engine limits",
                kind="research",
                instructions=(
                    "Template for: $goal (view: $view_style; loop: $core_loop). Map out what cs_script and the map "
                    "entities can do for: camera (CustomPlayerCamera), input (CSInputs, IsInputPressed-style APIs, "
                    "custom_hud_layout input capture), HUD (custom_hud_layout + Panorama layout), spawning "
                    "(PointTemplate), damage hooks (OnModifyPlayerDamage), rounds/teams/money, bots, and saving "
                    "(SetSaveData). Search docs for 'game modes' (game_type/game_mode). Write down the hard limits "
                    "(e.g. no new weapons or classes from script) so the template doesn't promise them."
                ),
                tools=(
                    "search_docs",
                    "get_script_api",
                    "list_script_types",
                    "get_entity",
                    "get_doc",
                    "list_examples",
                    "get_example",
                    "add_evidence",
                ),
                gate="evidence",
            ),
            StepSpec(
                id="architecture",
                title="Template architecture",
                kind="design",
                instructions=(
                    "Define the template: script modules (camera.js, input.js, rules.js, hud.js, or one script with "
                    "sections), shared state, and the extension points for future games. Include the cfg layering "
                    "(gamemode cfg + addon cfg), the HUD layout files (panorama/*.xml/.css) and the map conventions "
                    "(spawns, arena, point_script/point_template names)."
                ),
                prompt={"name": "cs2_script", "args": {"task": "Game template: $goal ($view_style; $core_loop)"}},
                tools=("add_evidence",),
                depends_on=("research",),
                gate="evidence",
            ),
            _scaffold(("architecture",)),
            StepSpec(
                id="implement_core",
                title="Implement camera + input + rules",
                kind="build",
                instructions=(
                    "Write the core scripts following the architecture. Keep the game-specific values in one config "
                    "object at the top of each file, so the template can be reused. Validate each file."
                ),
                tools=("write_addon_file", "validate_cs_script", "get_script_api", "get_example"),
                depends_on=("scaffold",),
            ),
            StepSpec(
                id="implement_hud",
                title="Implement HUD",
                kind="build",
                instructions=(
                    "If custom_hud_layout is used: write the Panorama layout (panorama/layout/$addon.xml + .css) "
                    "and the cs_script that drives it. Check the custom_hud_layout keyvalues with `get_entity` "
                    "(only a limited set of panel types is supported). Validate the scripts."
                ),
                tools=("get_entity", "write_addon_file", "validate_cs_script", "search_docs"),
                depends_on=("implement_core",),
            ),
            StepSpec(
                id="rules_cfg",
                title="Rules config",
                kind="build",
                instructions=(
                    "Write cfg/$addon.cfg with the round, team, bot and economy rules. Validate it with `validate_cfg`."
                ),
                prompt={"name": "server_config", "args": {"goal": "Rules for $goal: $core_loop", "game": "$game"}},
                tools=("write_addon_file", "validate_cfg"),
                depends_on=("scaffold",),
                gate="cfgs_valid",
            ),
            _validate(("implement_core", "implement_hud", "rules_cfg")),
            StepSpec(
                id="map_blockout",
                title="Map blockout guide",
                kind="design",
                instructions=(
                    "Write maps/BLOCKOUT.md: the dimensions for the chosen camera (camera height, visible area, so "
                    "walls don't block the view), required entities (spawns, point_script, point_template, "
                    "custom_hud_layout, triggers) with their keyvalues and I/O confirmed via `get_entity`."
                ),
                prompt={"name": "hammer_io_logic", "args": {"goal": "Blockout for: $goal", "game": "$game"}},
                tools=("get_entity", "find_entities_with_io", "write_addon_file", "add_evidence"),
                depends_on=("validate",),
                gate="evidence",
            ),
            _test(("map_blockout",)),
            _document(("test_in_game",)),
        ),
    )
)

_register(
    WorkflowSpec(
        id="server_config",
        title="Server / practice config",
        description="Produce and validate a .cfg for a goal (no addon code).",
        games=("cs2", "deadlock"),
        needs_addon=False,
        steps=(
            StepSpec(
                id="draft",
                title="Draft the config",
                kind="design",
                instructions="Draft the config for: $goal.",
                prompt={"name": "server_config", "args": {"goal": "$goal", "game": "$game"}},
                tools=("list_convars", "get_convar", "search_docs"),
            ),
            StepSpec(
                id="validate",
                title="Validate",
                kind="validate",
                instructions="Run `validate_cfg` on the draft and fix every error. Paste the final cfg into the notes.",
                tools=("validate_cfg", "add_evidence"),
                depends_on=("draft",),
                gate="evidence",
            ),
        ),
    )
)
