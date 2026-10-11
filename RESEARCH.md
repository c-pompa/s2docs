# Source 2 docs + MCP — source research (Oct 2026)

Engine: **Source 2**. Toolset: **Source 2 Workshop Tools** (Hammer, ModelDoc, Material Editor,
AnimGraph, Particle Editor, `resourcecompiler.exe`, `vpk.exe`). Game dirs: CS2 = `csgo`, Deadlock = `citadel`.

## Tier 1 — machine-readable, auto-updated (the backbone)

| Source | What it gives | How to ingest |
|---|---|---|
| [SteamTracking/GameTracking-CS2](https://github.com/SteamDatabase/GameTracking-CS2) `DumpSource2/` | `convars.txt`, `commands.txt`, `entities/{server,client}.fgd`, `schemas/<module>/*.h`, `network/*.txt`, `interfaces.txt`, `logging_channels.txt`, `module_metadata/*/pulse_bindings.txt` | `git clone --depth 1` / sparse checkout; re-pull on commit (updates every game patch) |
| [GameTracking-Deadlock](https://github.com/SteamDatabase/GameTracking-Deadlock) | Same layout for Deadlock (abilities, VData, heroes) | same |
| GameTracking-Dota2 / HLA | Same + Lua VScript dumps | optional |
| `Protobufs/` in those repos | Net messages, user messages, game events, demo format | parse `.proto` |
| [s2v.app Schema Explorer](https://s2v.app/SchemaExplorer/) | Browsable schema (classes, fields, enums, entity I/O); code on GitHub | reference / UI inspiration; data = DumpSource2 |
| [neverlosecc/source2sdk](https://github.com/neverlosecc/source2sdk) | Generated SDK per game branch (`csgo`, `deadlock`, ...) | optional, C++ headers |

## Tier 2 — official scripting APIs shipped in game files

- **CS2 `cs_script`** (JavaScript, `point_script` entity, module `cs_script/point_script`):
  `content/csgo_addons/cs_script_demo/maps/scripts/point_script.d.ts` + `tsconfig.json`.
  This .d.ts is the authoritative API — parse with the TypeScript compiler API into JSON.
  Example maps: `cs_script_demo.vmap`, `content/csgo/maps/editor/zoo` (`map editor/zoo/script_zoo`).
- **Pulse** (visual scripting): bindings in `pulse_bindings.txt`; not yet public for end users in CS2.
- **Panorama** (UI: XML/CSS/JS) — layouts and JS inside the game VPKs.
- **Lua VScript** (Dota 2 / HL:Alyx only): `script_help2` console dump. Not in CS2/Deadlock.
- **Game event definitions**: `*.gameevents` files inside `pak01_dir.vpk`.

## Tier 3 — querying the game locally (needs Windows + game install)

- Launch with `-tools` (Workshop Tools) or `-dev -condebug`; `con_logfile <file>`.
- Console: `cvarlist`, `find <term>`, `help <cvar>`, `cmdlist` → log to file → parse.
- Extract VPKs with **[Source 2 Viewer / ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat)** CLI
  (`Source2Viewer-CLI`): FGDs, gameevents, Panorama, VData (KV3), decompiled assets.
- `resourcecompiler.exe -h`, `vpk.exe` → CLI reference for build tooling.
- Note: GameTracking already does most of this in CI — local dumping is mainly for
  `point_script.d.ts` and for verifying against the user's installed build.

## Tier 4 — human-written docs (scrape + attribute)

- **Valve Developer Community** — `developer.valvesoftware.com/wiki/Counter-Strike_2_Workshop_Tools/*`,
  `.../Scripting`, `.../Scripting_API`, Hammer 2, Deadlock, `Category:Source_2`.
  MediaWiki → use `api.php?action=parse` / `action=query`, not HTML scraping.
- **[source2.wiki](https://www.source2.wiki/)** (Docusaurus, community) — cs_script intro + function list,
  Entity List, Convars across all Source 2 games, Editor Tools. Markdown repo on GitHub → clone.
- **CounterStrikeSharp** ([docs.cssharp.dev](https://docs.cssharp.dev)) — server plugins (C#, Metamod:Source 2).
  Also Swiftly, ModSharp.
- **Deadlock**: [Deadworks API](https://docs.deadworks.net) (server plugins),
  [deadlock-api/deadlock-server-plugins](https://github.com/deadlock-api/deadlock-server-plugins) knowledge base,
  [docs.deadlockmods.app/modding](https://docs.deadlockmods.app/modding) (VPK addons, sounds, models, UI), GameBanana.
- **AlliedModders hl2sdk `cs2`/`deadlock` branches** + Metamod:Source 2 — engine interfaces (C++).
- Dota 2: ModDota API docs (best-documented Lua VScript, useful for shared concepts).

## Licensing notes
- VDC and source2.wiki content: check each license; store attribution + source URL per page.
- Dumps of names/signatures are fine to index; don't redistribute Valve game assets.

## Proposed pipeline

```
ingest/  (per-source adapters, all idempotent, keyed by game + build id)
  gametracking  -> convars, commands, fgd entities (+I/O), schemas, network, pulse, protobufs
  cs_script     -> point_script.d.ts -> JSON (TS compiler API)
  vdc           -> MediaWiki API -> markdown
  source2wiki   -> git clone -> markdown
  cssharp/deadworks -> docs repos -> markdown
normalize/  -> SQLite (+ FTS5) + optional embeddings
site/       -> static site (Astro Starlight or Docusaurus) generated from DB
mcp/        -> MCP server over the same DB
```

### MCP tool sketch
- `search(query, game?, kind?)` — FTS across everything
- `get_convar(name, game)` — flags, default, help text, history across builds
- `get_entity(classname, game)` — FGD keyvalues, inputs, outputs
- `get_schema_class(name, game)` — fields, types, inheritance, network vars
- `get_cs_script_api(symbol)` — signature + JSDoc from point_script.d.ts
- `get_game_event(name, game)` — fields
- `diff_build(game, from, to)` — what changed this patch
- `get_doc(page)` — VDC / source2.wiki article text
