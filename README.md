# s2docs — Source 2 API/CLI docs + MCP

One searchable place for the undocumented surface of Valve's **Source 2** games (Counter-Strike 2, Deadlock).
Everything is extracted from each game build, then served three ways:

- **Web UI**: a light vanilla-JS SPA for browsing and searching.
- **JSON API**: FastAPI, with OpenAPI docs at `/docs`.
- **MCP server**: tools, prompts and resources so coding agents can write cs_script, Hammer logic, configs and mods using verified names.

| Data | CS2 | Deadlock | Source |
|---|---:|---:|---|
| Console variables / commands | 4,327 / 1,246 | 4,859 / 1,421 | `DumpSource2/convars.txt`, `commands.txt` |
| Hammer entities (keyvalues, inputs, outputs) | 857 | 2,181 | `DumpSource2/entities/{server,client}.fgd` |
| Schema classes / enums (fields, metadata, inheritance) | 3,753 / 634 | 9,264 / 880 | `DumpSource2/schemas/**.h` |
| Game events | 273 | 196 | `game/*/pak01_dir/resource/*.gameevents` |
| Pulse bindings | 2,488 | 2,646 | `DumpSource2/module_metadata/*/pulse_bindings.txt` |
| cs_script API symbols | 517 | — | `point_script.d.ts` (Valve's official declarations) |
| Valve Hammer FGD classes (labels, help, defaults, choices) | 430 (63 editor-only) | — | `game/csgo/csgo.fgd` + `game/core/*.fgd` |
| Valve sample scripts | 7 | — | `content/csgo_addons/cs_script_demo/maps/scripts/*.js` |
| Community docs | 185 pages (all games) | | [source2.wiki](https://www.source2.wiki) (CC BY-SA 4.0) |

### Where the data comes from

| Source | Authored by | How we get it |
|---|---|---|
| `point_script.d.ts`, `tsconfig.json`, sample `*.js`, `*.gameevents`, Hammer `*.fgd`, `steam.inf` | **Valve**, copied verbatim from the game install | [SteamTracking GameTracking](https://github.com/SteamTracking) mirrors the files on every patch |
| Convars/commands, runtime entity lists, schema, Pulse bindings | **Valve's binaries**, extracted with [DumpSource2](https://github.com/ValveResourceFormat/DumpSource2) | Same GameTracking repos (`DumpSource2/`) |
| Wiki pages | source2.wiki community, CC BY-SA 4.0 | Shallow, sparse git clone of [Source2Wiki/Source2Wiki](https://github.com/Source2Wiki/Source2Wiki) |

No game install is needed. Entity lookups merge both entity sources. Every member is tagged `runtime` (registered by the binary), `valve_fgd` (an editor-only key from Valve's FGD) or `both`. The Valve Developer Community wiki is not ingested: it sits behind an anti-bot proof-of-work wall (Anubis) that deliberately blocks scripted clients. See [RESEARCH.md](RESEARCH.md) for the full source survey.

## Quick start

```bash
uv sync
uv run s2docs sync              # sparse-clone GameTracking (~25 MB/game) and build data/s2docs.db (~5 s)
uv run s2docs serve             # http://127.0.0.1:8000  (UI) · /docs (OpenAPI) · /mcp (MCP over HTTP)
```

Other commands:

```bash
uv run s2docs sync --game cs2         # one game
uv run s2docs sync --no-fetch         # re-index the existing checkout (e.g. after parser changes)
uv run s2docs sync --docs-only        # only refresh source2.wiki   (--skip-docs to skip it)
uv run s2docs load cs2 /path/to/GameTracking-CS2   # index a full local clone
uv run s2docs stats
uv run s2docs prompt cs2_script task="teleport players who touch the lava"
uv run s2docs mcp                     # MCP over stdio
```

Environment variables: `S2DOCS_DATA_DIR` (default `./data`) and `S2DOCS_DB` (default `$S2DOCS_DATA_DIR/s2docs.db`).

## MCP

**Claude Code**
```bash
claude mcp add s2docs -- uv --directory /Volumes/SSD_2/XCode_Repos/myprojects/valve-mcp-api-project run s2docs mcp
# or, with `s2docs serve` running:
claude mcp add --transport http s2docs http://127.0.0.1:8000/mcp
```

**Claude Desktop / other stdio clients**
```json
{
  "mcpServers": {
    "s2docs": {
      "command": "uv",
      "args": ["--directory", "/Volumes/SSD_2/XCode_Repos/myprojects/valve-mcp-api-project", "run", "s2docs", "mcp"]
    }
  }
}
```

| Tools (all read-only) | |
|---|---|
| `search_docs` | Full-text search over everything. Each hit's `key` feeds the matching detail tool. |
| `get_convar`, `list_convars` | Default, min/max, flags (`cheat`, `replicated`, …) and help text |
| `get_entity`, `find_entities_with_io` | Keyvalues (with choices), inputs and outputs including inherited ones, with `defined_in` |
| `get_schema_class`, `list_schema_classes` | Fields, types, defaults, metadata, inheritance chain and subclasses (VData, netvars) |
| `get_game_event`, `list_game_events` | The effective definition after core → game → mod overrides |
| `get_script_api`, `list_script_types` | The cs_script API (`Instance` = `Domain`), including inherited members |
| `search_pulse` | Pulse visual-scripting node bindings |
| `diff_builds` | What changed between two indexed builds (convars, entities, schema, events, script API) |
| `list_examples`, `get_example` | Valve's cs_script sample scripts, each with the API it uses |
| `list_docs`, `get_doc` | source2.wiki pages as Markdown, with URL and license for citation |

**Prompts:** `cs2_script`, `hammer_io_logic`, `server_config`, `deadlock_mod`, `explain_symbol`. Each is a Markdown template in `src/s2docs/prompts/templates/`, filled at render time with live data from the database (e.g. the current `Instance` method index). Edit the `.md` files to tune them. The same prompts are available at `GET /api/prompts` and `POST /api/prompts/{name}/render`.

**Resources:** `s2docs://cs2/cs_script/point_script.d.ts` and `tsconfig.json` (put them next to your scripts for editor IntelliSense), plus `s2docs://cs2/cs_script/examples/{name}`.

## Architecture

```
src/s2docs/
  config.py               game registry (repo, mod dir, sparse patterns), settings
  ingest/
    sources.py            shallow + blob-filtered + sparse git checkout
    parsers/              pure parsers: convars, fgd, schema (.h), gameevents (KV1), pulse, d.ts, steam.inf
    loader.py             one transaction per build → normalized tables + FTS rows
  db/
    migrations/*.sql      versioned schema (PRAGMA user_version)
    connection.py         read-only URI connections for readers, WAL for the writer
    fts.py                identifier splitting + injection-safe FTS5 query builder
    repository.py         every read query (parameterized, recursive CTEs for inheritance)
  models.py               Pydantic models shared by the API and MCP
  prompts/                prompt registry + Markdown templates
  api/                    FastAPI app + routes (MCP mounted at /mcp, SPA at /)
  mcp_server.py           MCP tools/prompts/resources (mcp SDK 2.x MCPServer)
  web/                    index.html, app.js, style.css (no build step)
```

**SQLite design**
- Every data table hangs off `builds(id)` with `ON DELETE CASCADE`. A whole build is written in one transaction, so readers see either the old build or the complete new one. Re-indexing the same build number replaces it. The last N builds are kept (`--keep`, default 5) for `diff_builds`.
- `search_fts` is an FTS5 index over the current build of each game. `name` keeps identifiers whole (`_` is a token character, so `mp_round*` prefix-matches). `name_parts` and the body hold split forms (`m_iHealth` → `m i health`), so word searches find identifiers. Results are ranked by bm25 with column weights, with exact name matches first.
- User input never becomes FTS syntax: it is reduced to quoted tokens and bound as a parameter. `LIKE` filters escape `%` and `_`. The only dynamic SQL is placeholder lists and fixed column names.
- Entity and schema inheritance are resolved with recursive CTEs. `row_number()` keeps the nearest definition of an overridden member.
- The API and MCP open the database read-only (`mode=ro`), one connection per request or tool call.

## Development

```bash
uv run pytest -q                     # 44 tests: parsers, repository, API, MCP (offline fixture tree)
uv run ruff check src tests && uv run ruff format src tests
uv run python scripts/check_frontend.py   # renders every SPA route in headless Chrome (server must be running on :8765)
```

## Roadmap
- **Human docs**: load the Valve Developer Community (MediaWiki API), source2.wiki (git), and the CounterStrikeSharp and Deadworks docs into the existing `docs` table, with license and source URL per page.
- **Scheduled sync**: cron or CI running `s2docs sync`. It's cheap because the checkouts are incremental.
- **Deeper Deadlock data**: hero, ability and item VData values (KV3) from the VPKs via the Source 2 Viewer CLI.
- **Semantic search**: embeddings alongside FTS5 for natural-language questions.
