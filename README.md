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

**Hermes Agent** (native tools as `mcp_s2docs_*`; stdio, no server process needed)
```bash
hermes config set mcp_servers.s2docs.command /Volumes/SSD_2/XCode_Repos/myprojects/valve-mcp-api-project/.venv/bin/s2docs
hermes config set mcp_servers.s2docs.args '["mcp"]'
```

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

| Docs tools (read-only) | |
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

| Build tools | |
|---|---|
| `validate_cs_script` | Type-checks a script with `tsc` against this build's `point_script.d.ts`. Diagnostics are categorized (`unknown_api`, `needs_type_narrowing` with the cast to use, `deprecated_api`, …) |
| `validate_cfg` | Checks every cfg line against the indexed convars: unknown names (with suggestions), out-of-range values, cheat flags, client vs server |
| `scaffold_addon`, `list_addons`, `get_addon`, `read_addon_file`, `write_addon_file` | Local addon workspace (`$S2DOCS_WORKSPACE`, default `data/workspace`). Writes text sources only, with no path escapes and no overwrites unless asked |

| Host tools | |
|---|---|
| `list_hosts`, `get_host`, `register_host` | Registry of test/build hosts (ssh target, shell flavor, probed facts: GPU/RAM/processes/CS2 build) |
| `probe_host` | SSH-probe a host and store its facts (`scripts/windows/probe_host.ps1`) |
| `claim_host`, `heartbeat_host`, `release_host` | Server-side host locks with owner + TTL + heartbeat. A fresh lock blocks other owners; a stale lock (crashed worker) is taken over automatically. Check `list_hosts` before driving any host |

The same operations exist on the CLI (`s2docs host list|show|register|probe|lock|heartbeat|unlock`, JSON out, exit 2 = locked) and the HTTP API (`/api/hosts`, `/api/hosts/{id}/lock`, …; 409 = locked).

| Orchestration tools | |
|---|---|
| `list_workflows` | Multi-step recipes: `cs2_script_feature`, `cs2_camera_addon`, `cs2_game_template`, `server_config`, or `custom` |
| `start_project` | Creates a persistent project (stored in SQLite) and returns the first ready step(s) |
| `next_step` | Ready steps, each with instructions, the tools to use, rendered prompt text, and its gate |
| `update_step` | `in_progress` / `done` / `blocked` / `skipped`, with notes and artifacts. `done` runs the step's gate on the server |
| `get_project`, `list_projects`, `get_prompt_text` | State, history, and prompt text for clients that can't fetch prompts mid-task |

### Orchestration
The server holds the plan and the quality gates. The client's model does the work. Start with the `orchestrate` prompt ("build an isometric Diablo-style viewer for CS2"), or call `start_project` directly:

```
start_project ─▶ brief (ready steps + tools + rendered prompt) ─▶ agent works ─▶ update_step(done)
                     ▲                                                          │ gate check
                     └──────────────── next ready steps (dependency DAG) ◀──────┘
```

- **Gates** are checked on the server, not taken on trust: `scripts_typecheck` re-runs `tsc` on every `.js` file in the addon, `cfgs_valid` validates every `.cfg`, `addon_exists`, and `evidence` (needs notes or artifacts). A failed gate returns the exact errors (HTTP 409 / MCP tool error).
- **Steps form a DAG.** Independent steps (e.g. camera script and test cfg) become ready together, so a client can run them in parallel or hand them to subagents.
- **Projects persist.** Any session can pick one up with `next_step <id>`, and the web UI's **Projects** page shows progress and can start projects or move steps.
- Steps are snapshotted when a project starts, so editing `orchestration/workflows.py` never changes a project in flight.

**Prompts:** `orchestrate`, `cs2_script`, `hammer_io_logic`, `server_config`, `deadlock_mod`, `explain_symbol`. Each is a Markdown template in `src/s2docs/prompts/templates/`, filled at render time with live data from the database (e.g. the current `Instance` method index). Edit the `.md` files to tune them. The same prompts are available at `GET /api/prompts` and `POST /api/prompts/{name}/render`.

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
  build/                  validate_script (tsc), validate_cfg, workspace (addon files)
  orchestration/          workflows.py (registry) + engine.py (projects, DAG, gates)
  mcp_server.py           MCP tools/prompts/resources (mcp SDK 2.x MCPServer)
  web/                    index.html, app.js, style.css (no build step)
```

**SQLite design**
- Every data table hangs off `builds(id)` with `ON DELETE CASCADE`. A whole build is written in one transaction, so readers see either the old build or the complete new one. Re-indexing the same build number replaces it. The last N builds are kept (`--keep`, default 5) for `diff_builds`.
- `search_fts` is an FTS5 index over the current build of each game. `name` keeps identifiers whole (`_` is a token character, so `mp_round*` prefix-matches). `name_parts` and the body hold split forms (`m_iHealth` → `m i health`), so word searches find identifiers. Results are ranked by bm25 with column weights, with exact name matches first.
- User input never becomes FTS syntax: it is reduced to quoted tokens and bound as a parameter. `LIKE` filters escape `%` and `_`. The only dynamic SQL is placeholder lists and fixed column names.
- Entity and schema inheritance are resolved with recursive CTEs. `row_number()` keeps the nearest definition of an overridden member.
- The API and MCP read docs data read-only (`mode=ro`), one connection per request or tool call. Only orchestration (`projects`, `project_steps`, `project_events`) writes, through the WAL writer connection.

## Development

```bash
uv run pytest -q                     # 60 tests: parsers, repository, API, MCP, build tools, orchestration
uv run ruff check src tests && uv run ruff format src tests
uv run python scripts/check_frontend.py   # renders every SPA route in headless Chrome (server on :8765; extra routes as args)
```

## Roadmap
- **Human docs**: load the Valve Developer Community (MediaWiki API), source2.wiki (git), and the CounterStrikeSharp and Deadworks docs into the existing `docs` table, with license and source URL per page.
- **Scheduled sync**: cron or CI running `s2docs sync`. It's cheap because the checkouts are incremental.
- **Deeper Deadlock data**: hero, ability and item VData values (KV3) from the VPKs via the Source 2 Viewer CLI.
- **Semantic search**: embeddings alongside FTS5 for natural-language questions.
