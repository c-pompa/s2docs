You are planning a **Deadlock** mod (${build}). Deadlock's internal name is "Citadel" and its game directory is `citadel`.

## Goal
$goal

## Facts to respect
- Deadlock ships **no official scripting API**. It has no cs_script and no public Lua VScript. The available paths are:
  1. **Addon VPKs**: override files such as models, materials, particles, sounds, Panorama UI, and KV3 VData like hero/ability data. Pack them as `pakNN_dir.vpk` in `citadel/addons/`.
  2. **Server plugins**: community frameworks on Metamod/Deadworks that read and write networked schema fields. These only work on servers you control.
  3. **Map logic**: Hammer entities and I/O, wherever the workshop tools support it.
- Ability, hero and item tuning lives in VData classes whose names end in `VData`, e.g. `CCitadel_Ability_*VData`. Look them up with `get_schema_class` (game `deadlock`) to get exact field names and types before you write any KV3.
- Schema field names (e.g. `m_flCooldown`) are the keys used in KV3 VData and in plugin `GetField`/`SetField` calls.

## How to work
1. `search_docs` with game `deadlock` to find the relevant classes, entities, events and convars.
2. Verify every field and entity you reference with `get_schema_class` or `get_entity`.
3. Output:
   - which path you chose and why,
   - the files to create or override and where they go,
   - example KV3 or plugin code using only verified names,
   - the risks: anti-cheat/matchmaking (client mods only), and that patches change schemas. Suggest `diff_builds` to recheck after each patch.
