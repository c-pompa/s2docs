Explain **$query** for ${game_name} (${build}).

The index returned these matches:
$hits

## How to work
1. Choose the best match. Fetch its details with the tool for its kind:
   - `convar` / `command` → `get_convar`
   - `entity` → `get_entity`
   - `schema_class` / `schema_enum` → `get_schema_class`
   - `event` → `get_game_event`
   - `script` → `get_script_api`
   - `pulse` → `search_pulse`
2. Explain what it is, where it's used (map I/O, cs_script, server config, plugin or VData), and its important fields or flags. Give a minimal usage example.
3. Only state facts that the tool results back up. If something is inferred, label it as inferred.
