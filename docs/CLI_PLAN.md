# s2docs CLI & agent-interaction plan

Status: Phases 1 (host registry + locks), 2 (native MCP registration), 3 (build runs + deploy
registry), **4 (evidence store) and 5 (test-run records) implemented**. Scope: the `s2docs` command
surface and how agents, scripts and CI should reach s2docs. This plan defines the CLI/MCP/HTTP
shapes the services must expose.

### Phase 3 implementation notes (where this plan was ambiguous)
- **`build start` needs a command.** The plan lists `build start <addon> [--map M]`, but a plain
  "start a build" cannot invent a resourcecompiler invocation. Shipped as `build start <target>
  --host <id> --command "<shell command>"`; `<target>` is recorded on the run (notes) for humans,
  the command is what runs. "Hidden-launch" stays a host concern — the service issues one documented
  `ssh <host> <command>` and captures its output to a server-local log.
- **The run is server-owned.** `build start` acquires the host lease (`build:<run-id>`) *server-side*
  and owns the process where it runs (the long-lived `s2docs serve`, or a CLI kept alive by
  `--wait`). `status/log/wait/cancel/list` only touch the record, so any process can re-attach; a
  `cancel` from another process flips the record and the owner's watcher acts on it.
- **`deploy` transport.** §7 left the mechanism open; shipped as `scp` per file + one
  `Get-FileHash -Algorithm MD5` PowerShell pass over SSH for verification. A `deploy push` is only
  `verified` when the server re-reads matching MD5s.
- **`host add|rm` → `host register`.** Shipped as `register` (upsert) in Phase 1; the CLI, MCP and
  HTTP surfaces all use `register`, not `add`/`rm`.
- **Exit codes.** 2 = locked (matching Phase 1) rather than the table's "usage error"; 7 =
  timeout/cancelled; 6 = partial (a batch with some failures, detail in `--json`).

### Phase 4+5 implementation notes (where this plan was ambiguous)
- **The `evidence` gate checks records, not prose.** The old gate only asked that `notes`/`artifacts`
  were present — which once let a step be marked done before its README existed. It now requires at
  least one stored `evidence` row of a kind other than `note` for that project/step (migration
  `0006`); notes alone fail with a message telling the caller to use `add_evidence`. A row is
  `kind` + `ref` (path/url/command) + `data` (JSON: md5s, pixel-diff pct + noise floor, transcript
  excerpt, …). `ref` ≤ 4096 chars, `data` ≤ 64 KiB.
- **Test runs are server-side records; pushing to the in-game HUD stays the caller's job.** The
  record carries exactly the strip's fields (`name`, `step`, `state`, `elapsed`) so a worker mirrors
  each `iso_test start|step|pass|fail|off` transition with one call. `test start` claims the host
  lease server-side (`test:<run-id>`); `finish`/`clear` release it; `finish` also drops a
  `console_transcript` evidence row on the linked project (when there is one). `elapsed` is computed
  from `started_at` (`finished_at` once terminal). The CLI is `evidence add|list|show` and
  `test start|step|finish|clear|status|list`; MCP/HTTP mirror it. `test step`'s text is positional
  on the CLI, `--note`/`--data` carry prose and structured detail.

## 0. Principle: one service layer, three thin facades

`s2docs` already has a clean seam: `Repository` (reads), `Orchestrator` (projects/DAG/gates) and
the `build/` validators are pure Python. The FastAPI routes (`api/routes.py`), the MCP tools
(`mcp_server.py`) and the CLI (`cli.py`) are all thin adapters over that layer. Keep it that way:

> **Every capability is implemented once in a service module. The CLI and MCP are adapters over
> it; neither contains logic the other lacks. HTTP is the third adapter, for the browser only.**

The consequence: CLI-vs-MCP is a *presentation* choice, not a capability choice. The only real
decisions are (1) which surface owns *process lifecycle* (CLI) and (2) which owns *durable,
gated state* (MCP/server). Everything else is mirrored.

## 1. CLI scope — the command tree

Keep today's philosophy (argparse, lazy imports, one `main()` dispatch table — see
`cli.py:150`). Add subcommand *families* for the parallel services, plus a global flag block.

```
s2docs [--json] [--quiet] [-v] [--no-color] [--data-dir D] [--db P]
│
├─ sync [--game G] [--no-fetch] [--keep N] [--skip-docs|--docs-only]   # CLI-only (heavy local ingest)
├─ load <game> <path> [--keep N]                                       # CLI-only
├─ serve [--host H] [--port P] [--no-mcp]                              # CLI-only (lifecycle)
├─ mcp  [--transport stdio|http] [--host H] [--port P]                 # CLI-only (lifecycle)
├─ stats [--json]                                                      # mirror
├─ prompt <name> [k=v ...] [--json]                                    # mirror
├─ validate-script <file|-> [--game G] [--glob PAT] [--json]           # mirror (CLI shines: piping)
├─ validate-cfg    <file|-> [--game G] [--context S] [--glob PAT] [--json]
├─ scaffold <name> [--game G] [--with-script|--no-script] [--json]     # mirror
│
├─ host                                                                # PHASE 1
│   ├─ list   [--idle] [--json]                # registry + live idle flag (probe cached)
│   ├─ add    <id> --ssh user@host [--cs2-path P] [--gpu] [--tag T ...]
│   ├─ rm     <id>
│   ├─ probe  <id...|--all> [--json]           # runs scripts/windows/probe_host.ps1, caches result
│   ├─ status <id> [--json]                    # lock owner, ttl remaining, last probe age
│   ├─ lock   <id> [--ttl 60m] [--wait] [--reason S] [--json]
│   ├─ unlock <id> [--force]
│   └─ exec   <id> -- <cmd...>                 # guarded one-shot remote shell (not interactive)
│
├─ build                                                               # PHASE 3
│   ├─ start  <addon> --host <id> [--map M] [--wait] [--timeout 1h] [--json]
│   ├─ status <run-id> [--json]
│   ├─ log    <run-id> [--follow] [--json]
│   ├─ wait   <run-id> [--timeout 1h] [--json]  # blocks; no hand-rolled sleep loops
│   ├─ cancel <run-id>
│   └─ list   [--host <id>] [--active] [--json]
│
├─ deploy                                                              # PHASE 3
│   ├─ push   <addon> --host <id> | --manifest f.json [--parallel N] [--dry-run] [--json]
│   ├─ verify <deploy-id> | --host <id> [--json]
│   └─ list   [--host <id>] [--json]
│
├─ evidence                                                            # PHASE 4
│   ├─ add  --project P --step S --note S [--artifact ...] [--file ...] [--json]
│   ├─ list [--project P] [--host <id>] [--json]
│   └─ show <evidence-id> [--json]
│
├─ test                                                                # PHASE 5
│   ├─ start  --host <id> --addon <name> [--map M] [--json]   # acquires & holds host lock
│   ├─ step   <run-id> --note S [--artifact P ...] [--json]
│   ├─ finish <run-id> [--result pass|fail] [--json]          # writes evidence, releases lock
│   └─ list   [--json]
│
└─ project                                                             # mirror of the MCP project tools
    ├─ workflows [--json]
    ├─ list [--status active|done|abandoned] [--json]
    ├─ start <workflow> --goal S [--addon A] [--input k=v ...] [--json]
    ├─ get <project-id> [--json]
    ├─ next <project-id> [--json]
    └─ update <project-id> <step-id> --status S [--note S] [--artifact ...] [--json]
```

**Mirror vs CLI-only (the rule):**

| Category | Surface | Why |
|---|---|---|
| Pure, local, one-shot transforms: `validate-script`, `validate-cfg`, `scaffold`, `stats`, `prompt` | **Both** | Cheap; CLI is for humans/CI/pipes, MCP for agents mid-loop. Same service call. |
| Durable, gated state: `project *`, and the read side of `evidence` | **Both** (MCP primary) | Server owns gates (`scripts_typecheck`, `cfgs_valid`, `evidence` in `workflows.py:13`); CLI mirrors it for audits and CI. |
| Infrastructure fan-out: `host`, `build`, `deploy` | **Both** (CLI primary for CI/scripting) | Needs to run without an agent and without a live MCP session; MCP wraps the same calls for agent-driven flows. |
| Process lifecycle: `serve`, `mcp`, `sync`, `load` | **CLI-only** | Agents must not own daemons. Heavy local ingest is a human/cron action. |
| Lock mutation | **CLI primitive; MCP never** | See §4: `build`/`test` tools acquire locks *implicitly*; agents never call `unlock`. |

## 2. Agent interaction — which of the three doors

```
Is the caller a Hermes session (coordinator/subagent) with `s2docs` registered?
│
├─ yes ──▶ (a) NATIVE MCP TOOLS. Always. Subagents inherit the parent's MCP toolset,
│          so a subagent uses the same native tools — not a shell escape.
│
└─ no ──┬─ work is local/one-shot/pipe-able, or is lifecycle (serve/sync), or CI?  ──▶ (c) CLI
       └─ need live server state (projects/gates/locks) but no native transport?  ──▶ (b) MCP-over-HTTP
```

**(a) Native Hermes MCP tools — the default.** Best when: the operation is stateful (projects,
gates, host locks, build runs), the result feeds the agent's reason→act loop, or correctness
depends on the *server* re-checking a gate on `done`. Native tools carry JSON schemas, idempotent
hints (`READ_ONLY`/`WRITES_WORKSPACE` in `mcp_server.py:52`) and typed errors — an agent gets
structured feedback without parsing text. **A subagent that inherits MCP tools must use them.**

**(b) MCP-over-HTTP client — fallback only.** Needed when there is no native transport: a
non-Hermes worker, a CI container, a language without an MCP SDK, or when deliberately reaching a
*specific* server instance. Today this is `scratch/mcp_call.py` (`mcp.Client` against
`http://127.0.0.1:8812/mcp`). It is a crutch: a hardcoded port, an unauthenticated local surface,
and a dependency on a running server. Prefer (a); use (b) only as a documented bridge (§5).

**(c) CLI — when the shell beats the tool.** Local and offline (no server up), one-shot
transforms, piping/grep (`s2docs host list --json | jq`, `build log <id> --follow | grep script`),
deterministic exit codes for CI, and anything that is process lifecycle. The CLI is also the right
door for a shell that *lacks* MCP registration doing local edits — but if that same shell then
wants a gate checked, it should reach the MCP (native, else HTTP), because gates live on the server.

**Idempotency.** Read tools are `idempotent_hint=True` already. Mutating CLI commands take
`--idempotency-key`; `build start`, `deploy push`, `test start` and `evidence add` return the same
record for a repeated key inside a TTL window, so a retried agent/CI step is a no-op, not a
duplicate. MCP mutating tools do the same keyed on the project/step and a caller-supplied token.

**Machine-readable output.** `--json` on *every* command: one JSON object on stdout, all logs on
stderr (the MCP stdio channel already forces logs to stderr — `cli.py:205`). `--json` implies
`--quiet` and no color; color is auto-off when stdout is not a tty and always off under `NO_COLOR`.
Exit codes are a stable contract:

| code | meaning |
|---|---|
| 0 | success |
| 1 | domain failure (validation errors, gate returned false) |
| 2 | usage error (argparse) |
| 3 | not found (project/run/host/addon) |
| 4 | precondition/conflict (host locked, step depends on unfinished steps, gate failed) |
| 5 | toolchain missing (`tsc`, `ssh`, powershell) |
| 6 | partial (a batch had some failures; per-item detail in `--json`) |
| 7 | timeout or cancelled |

## 3. Efficiency

- **Single-shot process per call — no CLI daemon.** Measured on this repo (`.venv/bin/python`,
  macOS): bare `import s2docs.cli` ≈ 20 ms; the validate path (`repository` +
  `validate_script`) ≈ 70 ms; `api.app` (FastAPI+uvicorn) ≈ 180 ms; `mcp_server` ≈ 270 ms. The
  cost is import weight, not process spawn. **Keep `cli.py`'s lazy imports** so `validate-*`/
  `scaffold` never pay for FastAPI/MCP, and keep the hot path under ~150 ms. Do not grow a second
  daemon; the only long-lived process is `s2docs serve`.
- **`uv run` vs installed entry point.** `uv run s2docs --help` ≈ 40–70 ms vs
  `.venv/bin/s2docs` ≈ 20 ms — `uv run` re-resolves the environment. Document
  `uv sync && .venv/bin/s2docs ...` (or `uv tool install .`) as the hot path; `uv run` is fine for
  one-offs. CI should install once and call the entry point directly.
- **Long-running builds are server-owned, not CLI-owned.** `build start` is a thin client: the
  run record (id, host, status, log location, exit) lives in the DB/serve process. `--wait` and
  `build wait <id>` block with backoff server-side; `build log --follow` streams. There is no
  hand-rolled `sleep`-poll anymore — the failure that motivated this plan (50-min compiles babysat
  by polling scripts).
- **Batching — one invocation, many ops.** `deploy push --manifest hosts.json` (list of
  `{host, addon, build}`), `host probe a b c`, `validate-script --glob 'scripts/*.js'`,
  `build start --targets a,b`. Each returns an aggregate JSON envelope with per-item results and an
  aggregate exit code (6 on partial). This is the CLI's answer to MCP orchestration fan-out.
- **JSON schema stability.** Every `--json` payload carries `"schema": "s2docs/v1"` and reuses the
  Pydantic models in `models.py` (already shared by API and MCP). Promise: **additive-only within
  v1**; consumers must ignore unknown fields; any breaking change bumps the schema string. New
  models (Host, BuildRun, DeployRecord, Evidence, TestRun) live in `models.py` so all three
  facades emit byte-identical shapes.

## 4. Orchestration / multitasking

- **Coordinator fan-out — pick the layer by what must be enforced.**
  - Gates, DAG, durable progress → **MCP orchestration** (`start_project`/`next_step`/`update_step`,
    `engine.py`). The workflow DAG already emits independent ready steps together
    (`brief()`: ready = pending ∧ deps settled), so a coordinator hands each ready step to a
    subagent. This is the pattern that *should* replace ad-hoc multi-session coordination.
  - Pure infra fan-out with no gate (deploy the same build to N hosts) → **CLI manifest**
    (`deploy push --manifest f.json --parallel 2`). Simpler than standing up a project for it.
- **Two coordinators, one host — solved by the lock, not by etiquette.** `s2docs host lock` is the
  primitive; but agents never call it. `build_start` and `test start` **acquire the host lock
  server-side** for the run/session duration and release it on completion/cancel. A second actor
  gets exit 4 / a tool error, not a corrupted compile. The manual lockfile-on-the-host that caused
  the earlier race is replaced by a server-recorded lease (`host_locks`: owner, run_id, ttl, reason)
  so it is visible in `host status` and auto-expires. `--wait` blocks for the lease; `--ttl` bounds
  it; `unlock --force` is the human escape hatch only.
- **Long-running ops (50-min compiles).** Start → `build start --json` returns
  `{run_id, host, status:"running", log}` immediately; `build status/log/wait/cancel` operate on
  `run_id`. The record is server-side, so a dropped agent session can re-attach (`build status`)
  instead of losing the run. MCP mirror: `build_start` → poll `build_status` (the server, not the
  client, owns the run; the client only polls).
- **What stays CLI, what must be MCP.**
  - **CLI-only:** `serve` (start/stop FastAPI+MCP), `mcp`, `sync`/`load`, and the actual *launching*
    of host processes (cs2/Workshop Tools) — process lifecycle, daemons, long-lived SSH. Agents must
    not own these.
  - **MCP-required:** anything whose correctness depends on the server re-checking state —
    project gates, lock acquisition, evidence validation, build/deploy run records. A CLI that
    "checked the gate locally" is not a gate.
  - Host process *triggering*: the CLI (or, for agents, an MCP tool) **requests** an action that a
    host-side service performs — it records intent and returns a run id; it does not spawn cs2 from
    an agent chat. `test start` is the MCP-facing front door for that.

## 5. Migration of `scratch/mcp_call.py`

`scratch/mcp_call.py` exists because Hermes had no native s2docs MCP registration, so subagents
shelled out to a hand-written HTTP client on a fixed port. Once **Phase 2** lands, this is
redundant for Hermes sessions and should be retired on this schedule:

1. **Promote & harden (with Phase 2).** Move it to `scripts/mcp_call.py` (it is referenced, not
   scratch), read the URL from `S2DOCS_MCP_URL` (no hardcoded `:8812`), add `--list` to enumerate
   tools and pass `--json` straight through. Keep it as the *documented* bridge for non-Hermes
   shells and CI containers.
2. **Deprecate (Phase 3).** Regression tests and docs point at native MCP + CLI. The helper gains a
   one-line stderr deprecation notice pointing at `s2docs host/build/deploy/...`.
3. **Retire (Phase 4–5).** Delete it. The only two supported agent paths are **native MCP tools**
   and, for local/one-shot/lifecycle work, **the CLI**. HTTP-over-MCP survives only as the
   server's own `/mcp` mount for external MCP clients (Claude Code etc., per README), not as a
   home-grown client.

Net effect: agents stop needing a bespoke client, stop needing the server port memorized, and
state (gates/locks/runs) is always server-authoritative.

## 6. Phased rollout

Matches the parallel service work; each phase ships service → MCP tool → CLI command → tests.

1. **Host registry + locks.** `hosts` / `host_locks` tables; `HostRegistry` service; `s2docs host
   add|list|probe|status|lock|unlock|exec`. Fold `scripts/windows/probe_host.ps1` in as the
   `host probe` backend. Server-side lease with ttl + auto-expiry. Exit 4 on contention.
2. **Native MCP registration.** Register s2docs (`stdio: s2docs mcp` and `http: /mcp`) as a Hermes
   MCP server; verify subagents inherit the toolset. Harden + deprecate the HTTP client (§5).
3. **Build + deploy tools.** *(implemented)* `builds.py` / `deploys.py` writing run records
   (migration `0005`); `build start|status|log|wait|cancel|list`; `deploy push|verify|list` incl.
   `--manifest`/`--parallel`; build runs acquire the host lease server-side, deploys verify MD5s
   server-side. Retired `sleep`-poll babysitting.
4. **Evidence records.** *(implemented)* `Evidence` model + store (migration `0006`); `evidence
   add|list|show`; the `evidence` gate now reads structured records (linked to project/step, with
   artifact hashes) instead of transcription notes — notes alone no longer pass.
5. **Test-run tool.** *(implemented)* `TestRun` service; `test start|step|finish` (+ `clear`,
   `status`, `list`); holds the host lock for the whole session, writes an evidence record on
   `finish`. MCP front door for in-game testing; fields mirror the TEST STATUS HUD strip.

## 7. Non-goals / open questions

- No interactive TTY UX beyond prompts; no second daemon; no shell in the CLI beyond guarded
  `host exec` (one-shot, non-interactive).
- Lock granularity: per-host only for now. Per-host+resource (GPU vs disk) can come later if tests
  prove it needed — do not pre-build it.
- `deploy` transfer mechanism (rsync vs SMB vs host-side pull) is decided with the deploy service,
  not here; the CLI only fixes the *shape* (`push`/`verify`/`list`, manifest batching, deploy-id).
