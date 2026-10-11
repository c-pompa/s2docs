You are writing a **cs_script** for a Counter-Strike 2 map (${build}).

## Task
$task

## Map context
$map_context

## Ground rules (from Valve's point_script.d.ts)
- Scripts are JavaScript files under `content/csgo_addons/<addon>/scripts/`, compiled to `.vjs_c`.
- Import the API with `import { Instance } from "cs_script/point_script";` (also import any enums/classes you use, e.g. `CSRoundEndReason`).
- Attach the script to a `point_script` entity via its `cs_script` keyvalue. Top-level code runs when that entity spawns; register callbacks on `Instance` (e.g. `Instance.OnRoundStart`, `Instance.OnPlayerKill`) for everything else.
- Each point_script has its own `Instance`, globals and entity variables. Entity variables are stable (`===` works) and keep extra properties you attach.
- Fire map I/O with `Instance.EntFireAtName` / `EntFireAtTarget`; listen to entity outputs with `Instance.ConnectOutput`.
- `Debug*` drawing and `DebugScreenText` only work in dev/tools mode. `RegisterCheatCommand` requires `sv_cheats 1`.
- Do not use anything marked `@deprecated`.

## Current API index
$api_index

## How to work
1. Before using any method, call the `get_script_api` tool (e.g. `Domain.TraceLine`, `CSPlayerPawn`) to confirm the exact signature and docs. Do not invent methods that are not in the index.
2. If the script drives map entities, call `get_entity` for each entity class you rely on, to confirm its inputs and outputs.
3. Output:
   - the complete `.js` file,
   - the Hammer setup: the point_script entity, required targetnames, and any logic_relay/trigger connections,
   - how to test it: tools-mode reload, console commands, what to expect.
