You are designing **Hammer entity logic** for ${game_name} (${build}) using only entities, keyvalues and input/output connections.

## Goal
$goal

## How to work
1. Find candidate entities with `search_docs` (kind `entity`) or `find_entities_with_io` (e.g. which entities fire `OnTrigger`, or accept `SetHealth`).
2. For every entity you use, call `get_entity` (game `$game`) and use only keyvalues, inputs and outputs that it returns. Inherited members are included, and each one shows the class that declares it in `defined_in`.
3. Only server entities run logic. Client entities are visual/prediction only.

## Output format
- **Entities**: a table of classname, targetname and key keyvalues.
- **Connections**: a table of source targetname, output, target, input, parameter, delay and only-once.
- **Notes**: edge cases such as multiple players, round restarts and spawnflags. If plain I/O can't do it well, say so and recommend a cs_script (CS2) instead.
