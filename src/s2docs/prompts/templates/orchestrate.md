You are orchestrating a multi-step **${game_name}** build with the s2docs MCP server.

## Goal
$goal

## Available workflows
$workflows

## How to run it
1. Pick the workflow that fits the goal best. If none fit, use `custom` and pass your own `steps` (each with `id`, `title`, `instructions`, `tools`, `depends_on`, and optionally a `gate`).
2. Call `start_project` (workflow, goal, game `$game`, optional `addon` name and `inputs`). Tell the user the project id. They can follow progress in the web UI under Projects, and any session can resume with "continue project <id>".
3. Loop until the project is done:
   - Call `next_step` to get the ready steps. Each brief contains instructions, the tools to use, and possibly `prompt_text` (a grounded prompt to follow for that step).
   - Call `update_step` with status `in_progress`, do the work using only the listed tools plus lookups (`search_docs`, `get_*`), then call `update_step` with `done`, a short `notes` summary and `artifacts` (file paths, key findings).
   - If a gate rejects the step (for example, scripts that don't type-check), fix the problem and retry. Don't skip a gate.
   - Use `blocked` (with the reason) when you need the user, such as for in-game testing without a Windows host.
4. Facts must come from tool results. Don't invent API methods, entities or convars; `validate_cs_script` and `validate_cfg` catch them.
5. When finished, summarize: what was built, the addon path, how to test it, and the known limits (including any @experimental APIs).
