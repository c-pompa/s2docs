---
name: s2docs-source2
description: "Use for Source 2 work (CS2, Deadlock): look up convars, Hammer entities, schema classes, game events, cs_script and Pulse APIs, then scaffold and validate addons, build maps on a test host, deploy and verify, record evidence, test runs and jobs, and keep a catalog of the assets that passed. Runs through the s2docs server."
---

# s2docs — Source 2 documentation and mod pipeline

s2docs indexes Valve's own game data for **CS2** and **Deadlock** (dump files, FGDs, game events,
the `cs_script` `.d.ts`, Pulse bindings) plus Source2Wiki, into a local SQLite database, and exposes
lookups and the mod pipeline as MCP tools. Everything runs on the user's machine; nothing is sent
to a third party.

Rules that decide whether the user's build works:

1. **Never invent a Source 2 name.** Convars, entity keyvalues, schema fields, game events and
   Pulse nodes are all indexed. If a name does not come back from a tool, it does not exist —
   say so instead of guessing a plausible `CCitadel...` or `sv_something`.
2. **Search short.** The index matches tokens with AND semantics: a prose sentence returns
   nothing. Pass a two-to-four word subject ("spectator pawn class", "buy menu convar"), one
   concept per call, and prefer the rare word over the generic one.
3. **Quote the build you answered from.** `list_games` reports the indexed build and patch
   version per game; a CS2 update changes convars, schema fields and Pulse nodes, so name the
   build beside a version-sensitive answer.
4. **A write is not done until it is read back.** After `write_addon_file` read the file, after
   `deploy_push` call `deploy_verify`, and record what you verified with `add_evidence`. The
   `evidence` gate and the user both check that row, not your summary.
5. **This server is `untrusted` in Hermes.** Every write-capable tool call needs the user's
   approval. Do not batch speculative writes to get through the prompts in one go; ask for the
   run you actually want.

## Start here

- `server_info` — which store this process serves (`db_path`, `pid`) and the web UI URL. If two
  sessions report different `db_path` values they are not talking to the same index; stop and
  fix that before writing.
- `list_games` — games plus their indexed build. If a game is missing or its build is old, the
  user needs `s2docs sync`/`load` before answers about it are trustworthy.

## Look something up

`search_docs` is the entry point: it returns rows with a `kind` and a `key`, and the `kind`
selects the detail tool.

| kind | detail tool | use it for |
|---|---|---|
| convar / command | `get_convar`, `list_convars` | defaults, min/max, flags (cheat, replicated), help text |
| entity (Hammer) | `get_entity`, `find_entities_with_io` | keyvalues with choices, inputs/outputs incl. inherited |
| schema class / enum | `get_schema_class`, `list_schema_classes` | engine fields, types, defaults, inheritance |
| game event | `get_game_event`, `list_game_events` | fields and types after core/game/mod override |
| cs_script API | `get_script_api`, `list_script_types` | Valve's JavaScript API: types, symbols, member docs |
| Pulse node | `search_pulse` | entity inputs/outputs and functions usable in Pulse graphs |
| example script | `list_examples`, `get_example` | Valve-written `cs_script_demo` samples to adapt |
| wiki page | `get_doc`, `list_docs` | Source2Wiki Markdown, with its URL and licence |
| diff between builds | `diff_builds` | what an update added or changed, member by member |

`get_prompt_text` renders this server's own task prompts (map rebuild, cs_script authoring,
Hammer I/O logic, server config, Deadlock modding, symbol explanation) — use it when the user
asks for a workflow rather than a lookup.

## Author an addon

- `scaffold_addon` creates `content/csgo_addons/<name>/` (scripts/, maps/, materials/) in the
  local workspace and returns its root.
- `read_addon_file` / `write_addon_file` / `get_addon` / `list_addons` work inside that workspace
  only; write real files, never paste a whole addon into chat.
- `validate_cs_script` type-checks a `.js` with the TypeScript compiler against the indexed
  `point_script.d.ts`; `validate_cfg` checks every command in a `.cfg` against the indexed
  convars and flags unknown names. Run the validator after each edit — it is the cheapest way to
  catch an invented API before a compile or a launch.

## Run the pipeline

Steps that need a machine other than this one are explicit:

- **Hosts** — `list_hosts`/`get_host` show registered Windows test/build PCs and their lease
  state. `claim_host` before touching one, `heartbeat_host` (same phase) during long work,
  `release_host` when finished. A host that cannot compile VMaps must not be given a build job.
- **Builds** — `build_start` runs a server-owned compile on a host's lease; `build_status`,
  `build_log`, `build_wait` (server-side wait, no client sleep loop) and `build_cancel` round it
  out. `build_list` shows recent runs.
- **Deploys** — `deploy_push` copies files and hashes them on the far side; `deploy_verify`
  re-checks the recorded hashes. A copy command's own success line is not verification.
- **Evidence** — `add_evidence` records the structured row a step's gate checks (screenshot,
  console readout, log excerpt, hash).
- **Test runs** — `test_start`/`test_step`/`test_finish`/`test_clear` mirror the in-game HUD
  strip; `test_status`/`list_test_runs` read them back.
- **Jobs** — the queue the user watches: `job_enqueue` (validated intake), `job_next`/`job_start`,
  `job_event` for timeline entries, `job_verdict` to answer a human-review step, `job_finish`
  (summary must say what, why, learned and next), `job_link` to tie a run to the record it is
  about, `job_screenshots` for the shots tied to a step.
- **Projects** — `start_project`, `next_step`, `update_step`, `get_project`, `list_projects` for
  multi-step recipes (`list_workflows` shows the recipes). `update_step` with `done` runs that
  step's gate; a failed gate means the step is not done, not that the gate is wrong.

### The chain as one job

`s2docs pipeline run` and `s2docs pipeline script` record a whole chain as a single durable job
with one event per stage, instead of leaving a trail of unrelated builds. They are honest about
which stages are real, so read the timeline rather than assuming every stage ran:

- `s2docs pipeline run --prompt … --addon <name> --name <prop> --mesh <path> --host <id> [--dest <root>] [--ingame-host <id> [--ingame-model <vmdl>] [--ingame-clear] [--verdict pass|fail]]` —
  `prompt` → `concept` → `mesh` → `asset` → `build` → `deploy` → `ingame`. `asset` and `build`
  are **real** (the same `.vmdl`/`.vmat` writers and server-owned build runs the CLI uses), and a
  succeeded build also collects its compiled files off the compile host (md5-checked, attached to the
  build run). `deploy` is real when you pass `--dest` and the build succeeded: it pushes the addon the
  way `s2docs deploy push` does, verifies the MD5s on the host, and links the deployment to the job.
  Without `--dest` the stage is **skipped**, with a reason saying no destination was given.
  `ingame` is real when you pass `--ingame-host` (a host running CS2 with `hermes_sandbox` loaded): it
  pushes the collected artifacts **loose** into that host's `game/csgo` (md5 readback), then sends the
  round lock and `hs_model`. It is `verified` only on the game's own
  `[hs] model <path> spawned name=<path>` line plus a rising `hs_ping` — a console line is not a visual
  verdict, so the job closes `inconclusive` and stops there. `--verdict pass|fail` on `pipeline run` is
  only for a verdict you already made *while* watching; do not use it to predict what you will see.
  `concept` and `mesh` are recorded **stubbed** because no text-to-3D generator is wired into this
  repo (the chain starts from a mesh that exists).
- `s2docs pipeline verdict <job-id> pass|fail [--notes TEXT] [--source …]` — the look at the screen,
  recorded **after** the chain closed. `pass` closes the job `passed`, `fail` closes it `failed`; nothing
  is re-run and nothing is spawned twice. It writes a `human-confirm` evidence row (so the confirm push
  files fire) and a `verdict` stage event, and rewrites the close-out summary. Refused: a job with no
  `ingame` stage, an `ingame` stage that was skipped or blocked (nothing was on screen), a readback that
  did not verify (a `pass` never overrides `failed`), a still-running job (use `job_verdict` for a step),
  and a second contradictory verdict — repeating the same one is a no-op.
- `s2docs pipeline script --prompt … --addon <name> --name <script> [--script file.js] [--compile] [--deploy-host <id>]` —
  text → cs_script. `generate` writes the `--script` text you supply (the caller is the
  generator, so the stage never invents code it cannot attribute) and `validate` type-checks it
  through the same `validate_cs_script`; a missing TypeScript toolchain (or a data dir with no
  ingested script API) is **blocked**, never a pass and never a failure. With `--compile` the
  `build` stage is real: the `.js` is pushed to `--host` and one `resourcecompiler` run turns it
  into `scripts/<name>.vjs_c`, pulled back MD5-checked and attached to the run; a failed
  `validate` never compiles. `--deploy-host` then copies the `.vjs_c` **loose** to that host's
  `<CS2>/game/csgo/scripts/`. `verdict` stays **blocked**: running the script needs a map whose
  `point_script` references it.

Both return the job; `job_get`/`get_project` show the same chain. A produced artifact is linked
to the job, not pasted into a message. Two things that bite: `--name` must be addon-safe
(`[a-z][a-z0-9_]+`), and omitting `--build-command` records the build as **not attempted** — the
chain still closes out, so the job shows an `asset` stage with no compile behind it.

### Save what the user made — the catalog

The catalog records an asset only after the chain proved it works. The gates are enforced on the
server, so a rejected call means the evidence is genuinely missing — do not work around it:

- `asset_promote(kind, name, build_id, licence, …)` — `kind` is `model` (a Hammered prop chain)
  or `action` (cs_script). `build_id` must have reached `succeeded`, and `licence` is required
  (never a Valve-derived asset). For `kind=model`, `frame_job_id` **and** `frame_artifact` are
  required, the job must actually have stored that artifact, and its verdict must not be `fail` —
  a job with no verdict is not a pass. A duplicate slug is a conflict. Provenance (build, deploy,
  frame job, mesh hash, tri count) is linked, not copied: the catalog is a view over real runs,
  never a second file store.
- `asset_list(kind=?, tag=?, include_retired=false)` — newest first. Retiring is a soft delete,
  so `include_retired=true` brings the row back with every link intact.
- `asset_get(asset_id)` — one entry by id or slug, with its build/deploy/frame-job ids.

Promote only what the user asked to keep, and quote the returned slug and ids back to them.

## Report

Give the user the tool result, not a paraphrase of it: the convar's default and flags, the exact
rank/hit, the build id, the deploy id and its verified hashes, the evidence row id. When a tool
returns "not found" or an empty list, say that plainly and name what you searched for — an
invented answer here costs a compile and a launch to disprove.

`references/tools.md` is the generated table of every tool this server exposes (name, read-only
or write, summary), built from a live `tools/list`.
