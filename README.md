# s2docs — Source 2 API/CLI docs + MCP

One searchable place for the undocumented surface of Valve's **Source 2** games (Counter-Strike 2, Deadlock).
Everything is extracted from each game build, then served three ways:

- **Web UI**: a light vanilla-JS SPA for browsing and searching, plus operations pages (**Hosts**, **Activity**) for watching tests on remote hosts without SSHing anywhere, and an **Orchestration** page for the server's multi-step plans.
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

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `S2DOCS_DATA_DIR` | the checkout's `./data` when running from a source checkout, else a per-user dir (`~/.local/share/s2docs`, or `%LOCALAPPDATA%\s2docs` on Windows) | Directory holding the index, workspace and captured logs. **Never** relative to the cwd (see below). |
| `S2DOCS_DB` | `$S2DOCS_DATA_DIR/s2docs.db` | The SQLite index file. |
| `S2DOCS_WORKSPACE` | `$S2DOCS_DATA_DIR/workspace` | Local addon sources written by the build tools. |
| `S2DOCS_AUTH_TOKEN` | unset (auth off) | When set, mutating `/api` requests and **every** `/mcp` request must present this token (see below). Read once at startup — restart the server to rotate it. |
| `S2DOCS_STUCK_SEC` | `1800` | Phase age (seconds) after which a fresh host lock reads STUCK. |
| `S2DOCS_STUCK_PHASES` | built-in per-phase defaults | JSON object of per-phase stuck thresholds, **merged over** the built-ins. |
| `S2DOCS_REAP_SEC` | `60` | Seconds between orphan-run reaper passes (`<= 0` disables the periodic task). |

**The data dir never depends on the working directory.** With `S2DOCS_DATA_DIR` unset the default is a *fixed* path: a source checkout uses its own `./data`, and anything else uses a per-user directory. This closes the split-database incident — a server and an MCP client that were launched from different working directories (an MCP config that predates the env fix spawns `s2docs mcp` with `cwd` set to the client's profile dir) used to read and write two different stores, leaving a shadow DB next to an unrelated cwd. `serve` and `mcp` still print a loud stderr warning when the variable is unset, now naming the resolved path. `S2DOCS_DB` and `S2DOCS_WORKSPACE` overrides are unchanged.

**Auth is opt-in.** Set `S2DOCS_AUTH_TOKEN` and the server requires `Authorization: Bearer <token>` or `X-S2docs-Token: <token>` on every non-`GET`/`HEAD`/`OPTIONS` request under `/api` and on every request to the HTTP MCP endpoint `/mcp`; a missing or wrong token is `401` (compared in constant time). `GET` routes and the static web UI stay open, and stdio MCP is unaffected (it is local). With the variable unset the behaviour is unchanged — no auth — and `GET /api/health` and the MCP `server_info` tool report `auth_required: false` (they report only the boolean, never the token).

**`deploy_push` takes the host lease.** A deploy now arbitrates the host through the same lock the build and test services use, so it can never copy onto a host another owner holds. Pass `owner` if you already hold the lease — the push reuses it and leaves it locked; otherwise a free host is claimed as `deploy:<id>` for the copy+verify window and released afterwards (owner-conditional, even on failure), and a host held by a different owner is a `409` (`HostLocked`). The effective owner is recorded on the deployment row. The `deploy_verified` orchestration gate no longer falls back to the host's latest deployment: the step must name the deployment id from `deploy_push` as an artifact, and it must belong to the project's host and be `verified`.

## Web UI

`s2docs serve` mounts a small vanilla-JS SPA at `/` (hash routes, no framework, no build step). Besides the browse/search sections (`#/<game>/…`) it has install-wide operations pages for watching work on remote hosts, and a page for the server's orchestration plans:

- **Hosts** (`#/hosts`) — the registry plus live lock state: shell, GPU (from the last probe), how long ago it was probed, and the current lock owner with fresh/stale + heartbeat age, its **phase** (what the owner is doing) and a **STUCK** badge. A lock is STUCK when its heartbeat is fresh but its phase has not moved for longer than the server's threshold — liveness is not progress, so a worker that heartbeats faithfully while its compile hangs still shows up. Clicking a host opens `#/hosts/<id>` with its facts JSON, **lock history** (claim / takeover / release / phase-change events) and its recent builds, deployments and test runs. Read-only; auto-refreshes every 5 s while open.
- **Activity** (`#/activity`) — the page to watch instead of tailing logs: a **Now** section (the running test run — the same `name / step / state / elapsed` fields as the in-game TEST STATUS HUD strip — and active/queued build runs, freshest first), a **By host** section grouping every recent build run, test run and deployment under the host it ran on, and a collapsed full history. Auto-refreshes every 5 s while a run is active, 15 s when idle.
- **Orchestration** (`#/<game>/projects`) — the multi-step **orchestration DAGs** (distinct from runs): each project opens to a step-level view with a dependency graph, and per step its state, gate, evidence counts, updated time and notes. For the build/test/deploy runs those steps drive, use Activity.
- **Jobs** (`#/jobs`) — the job ledger + queue: one record per requested `test`/`demo`/`final` run (intake, timeline, screenshots/logs, links, and a mandatory close-out summary). A per-host queue board (running on top, then queued by position, error/crash counts in red); `#/jobs/<id>` shows the intake, the screenshot gallery, linked records and the summary card (first on a finished job). Jobs never claim the host lease — the build/test/deploy tools still do; a job links to them. See `docs/TEST_JOBS.md`.

**Activity is service-fed.** A row appears on that page only when work goes through the s2docs build / test / deploy services (`build_start` / `test_start` / `deploy_push` over MCP, or the equivalent `s2docs` CLI) — it is *not* a view of any CS2 that happens to be running anywhere, so an empty page means nothing has been recorded yet, not that the page is broken; the empty state says so and points at the commands to start something. All these pages read the existing JSON API (`/api/hosts`, `/api/hosts/{id}/locks`, `/api/builds`, `/api/deploys`, `/api/test-runs`, `/api/projects`, `/api/projects/{id}`, `/api/evidence`); nothing there can mutate a host or a run.

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
| `claim_host`, `heartbeat_host`, `release_host` | Server-side host locks with owner + TTL + heartbeat + **phase**. A fresh lock blocks other owners; a stale lock (crashed worker) is taken over automatically. Report a `phase` (what you are doing) so the server can flag a lock **STUCK** when its heartbeat is fresh but the phase has not moved (liveness is not progress). Check `list_hosts` before driving any host |
| **Stuck detection** | The server, never the client, decides stuck-ness: a lock is `stuck=true` when `fresh` and its `phase_age_sec` exceeds a threshold (env `S2DOCS_STUCK_SEC`, default 1800 s) unless a per-phase override applies. `compile:vis_rays` and `compile:vis_los` ship with built-in **5400 s** defaults (the long silent stretch of a CS2 compile is the visibility ray tracing — measured at ~1354 s and ~1409 s, against a ~2 min lighting bake — matching the driver's own 3 x 1800 s hang threshold; the driver's buffered log can leave the phase unchanged ~25-30 min on a healthy run), so no env is needed for them; env `S2DOCS_STUCK_PHASES` (a JSON object like `{"test": 600}`) **merges over** the built-in defaults per key rather than replacing them. Heartbeating the same phase does not clear it; report a new phase as work progresses. A lock with no phase (old clients) is never STUCK |

The same operations exist on the CLI (`s2docs host list|show|register|probe|lock|heartbeat|unlock`, JSON out, exit 2 = locked; `lock`/`heartbeat` take `--phase`/`--note`, and `host list`/`show` print a `STUCK:` line on stderr) and the HTTP API (`/api/hosts`, `/api/hosts/{id}/lock`, …; 409 = locked; every host read carries `lock.phase`, `lock.phase_age_sec` and `lock.stuck`; `/api/hosts/{id}/locks` returns the claim/takeover/release/phase-change audit trail the Hosts page shows).

| Build tools | |
|---|---|
| `build_start` | Start a **server-owned** run on a host: acquires the host lease (`build:<run-id>`), runs the caller's command over SSH, returns a run id immediately. No client sleep-polling — use `build_wait`/`build_status` |
| `build_status`, `build_list` | A run's record (status, exit code, parsed summary counts, material guard, artifacts) and recent runs |
| `build_log` | Tail the run's log — the log is UTF-16 (resourcecompiler), so this decodes it and reports the detected `encoding` plus any material-guard warnings |
| `build_wait` | Block server-side until the run finishes (bounded), or fail on timeout. `strict_materials=true` fails a 'succeeded' run whose log flagged missing/reserved materials |
| **Material guard** | Scans the decoded log for `referencing missing material`, `Failed to compile` and reserved-path references (Valve's `_vrf/` subtree, another map's `maps/<other>/` subtree). Findings land in the run's `summary.material_guard` / `warnings` field (counts + first paths). A warned run still finishes **`succeeded`** — the resourcecompiler exits 0 — unless strict mode is on |
| `build_cancel` | Mark the run cancelled and release its host lease |

| Deploy tools | |
|---|---|
| `deploy_push` | Copy files to a host per a manifest (`{path, local}`), then verify what landed by re-reading MD5s over SSH (`Get-FileHash`). Returns a deploy id with per-file `md5`/`verified`. Takes the host lease for the copy+verify window: `owner` reuses your existing lease, a free host is claimed as `deploy:<id>` and released afterwards, a host held by another owner is a `HostLocked` error |
| `deploy_verify` | Re-check a recorded deployment's MD5s and update its status |
| `deploy_list` | Recent deployments, optionally per host |

CLI: `s2docs build start|status|log|wait|cancel|list` and `s2docs deploy push|verify|list` (global `--json` → one `{"schema": "s2docs/v1", …}` object on stdout, logs to stderr; exit codes 0 ok, 1 error, 2 locked, 3 not found, 6 partial, 7 timeout/cancelled, 8 material warnings under `--strict-materials`). HTTP: `/api/hosts/{id}/builds`, `/api/builds/…`, `/api/deploys`.

| Evidence tools | | 
|---|---|
| `add_evidence` | Store a structured evidence row for a project step: `kind` (`console_transcript` / `file_hash` / `pixel_diff` / `screenshot` / `note` / `other`), `ref` (path / url / command) and `data` (JSON detail — md5s, pixel-diff pct + noise floor, transcript excerpt). **A step with an `evidence` gate cannot be marked done until at least one non-note row exists for it** |
| `list_evidence` | Evidence rows (newest first), filtered by project, step and/or kind |

| Test-run tools | |
|---|---|
| `test_start` | Open a server-side test session (`name`, optional `host` / `project`); claims the host lease server-side. Record fields (`name`, `step`, `state`, `elapsed`) mirror the in-game TEST STATUS HUD strip |
| `test_step`, `test_finish`, `test_clear` | Set the current step, finish `pass`/`fail` (releases the lease; records a console-transcript evidence row on the linked project), or dismiss (`iso_test off`) |
| `test_status`, `list_test_runs` | One run (or the current active run) and recent runs, optionally by state |

CLI: `s2docs evidence add|list|show` and `s2docs test start|step|finish|clear|status|list`. HTTP: `/api/evidence`, `/api/evidence/{id}`, `/api/test-runs` (+ `/current`, `/{id}/step|finish|clear`).

| Job tools | |
|---|---|
| `job_enqueue` | Enqueue a job (validated intake) — the `test`/`demo`/`final` run record with its pass criterion |
| `job_list`, `job_get`, `job_next` | Jobs (newest first, with error/crash counts), one job with its timeline/links/artifacts, and the head of a host's queue when its lease is free |
| `job_assign`, `job_start`, `job_event` | Set host/queue order, queued → running, and append timeline events (a `phase` updates the job; `crash`/`error` carry a log tail) |
| `job_attach`, `job_request_screenshot` | Copy a local file into the job's artifacts (append-only), or ask the runner for a screenshot |
| `job_link`, `job_finish`, `job_cancel` | Attach a test run/build/deploy/evidence/project, close out (all four summary fields required), or cancel |

CLI: `s2docs job enqueue|list|show|next|start|event|attach|shot-request|finish|cancel|watch` (the global exit codes: 3 not found, 4 conflict, 9 validation). HTTP: `/api/jobs` (+ `/next`, `/{id}`, `/{id}/assign|start|event|link|screenshot-request|finish|cancel`, `/{id}/artifacts/{name}`), `GET /api/artifacts/{job}/{name}`. Every job event also appends `<data_dir>/job_events.jsonl` (`/api/health` → `job_events_jsonl`). Full notes: `docs/TEST_JOBS.md`.

| Orchestration tools | |
|---|---|
| `list_workflows` | Multi-step recipes: `cs2_script_feature`, `cs2_camera_addon`, `cs2_game_template`, `cs2_live_test`, `server_config`, or `custom` |
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

- **Gates** are checked on the server, not taken on trust: `scripts_typecheck` re-runs `tsc` on every `.js` file in the addon, `cfgs_valid` validates every `.cfg`, `addon_exists`, and `evidence` — which requires at least one stored non-note evidence row for the step (via `add_evidence`); notes and artifacts alone no longer pass. `cs2_live_test` adds `host_claimed` (the project really holds the host lease, owner `project:<id>`) and `deploy_verified` — the deployment id from `deploy_push`, recorded as an artifact of the step, must belong to the project's host and be `verified` in the deploy registry (there is **no** latest-for-host fallback, so a stale or unrelated deployment can never satisfy it). A failed gate returns the exact errors (HTTP 409 / MCP tool error).
- **Steps form a DAG.** Independent steps (e.g. camera script and test cfg) become ready together, so a client can run them in parallel or hand them to subagents.
- **Projects persist.** Any session can pick one up with `next_step <id>`, and the web UI's **Orchestration** page shows the step-level DAG, gates and evidence, and can start projects or move steps.
- Steps are snapshotted when a project starts, so editing `orchestration/workflows.py` never changes a project in flight.

**Prompts:** `orchestrate`, `cs2_script`, `hammer_io_logic`, `server_config`, `deadlock_mod`, `cs2_map_rebuild`, `explain_symbol`. Each is a Markdown template in `src/s2docs/prompts/templates/`, filled at render time with live data from the database (e.g. the current `Instance` method index). Edit the `.md` files to tune them. The same prompts are available at `GET /api/prompts` and `POST /api/prompts/{name}/render`.

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
  hosts.py                host registry + server-side leases (claim/heartbeat/release, TTL expiry) + lock-event audit (host_lock_events) for the Hosts page
  builds.py               server-owned build runs (SSH launch, UTF-16 log tail, host lease)
  deploys.py              deployment registry (scp push + Get-FileHash MD5 verification)
  evidence.py             structured evidence store — what the `evidence` gate checks (kind/ref/data)
  test_runs.py            in-game test sessions mirroring the TEST STATUS HUD strip (start/step/finish/off)
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
uv run pytest -q                     # 115 tests: parsers, repository, API, MCP, build tools, orchestration, hosts, builds/deploys (incl. material guard), evidence, test runs, web-UI visibility
uv run ruff check src tests && uv run ruff format src tests
uv run python scripts/check_frontend.py   # renders every SPA route in headless Chrome (server on :8765; extra routes as args)
```

## Roadmap
- **Human docs**: load the Valve Developer Community (MediaWiki API), source2.wiki (git), and the CounterStrikeSharp and Deadworks docs into the existing `docs` table, with license and source URL per page.
- **Scheduled sync**: cron or CI running `s2docs sync`. It's cheap because the checkouts are incremental.
- **Deeper Deadlock data**: hero, ability and item VData values (KV3) from the VPKs via the Source 2 Viewer CLI.
- **Semantic search**: embeddings alongside FTS5 for natural-language questions.
