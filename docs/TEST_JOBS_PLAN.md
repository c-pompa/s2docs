# Test jobs: queue, live reporting, screenshots, crash logs, close-out summary (v1)

Status: PLAN — branch `feature/dev/test-jobs` (cut from `ws/confirm-push`, so migration 0012 sits
on top of the live server's 0011). Lands on `dev` first, never straight to `main`.

## Why
A CS2 test / demo / final-product run today is spread over test runs, host locks, evidence rows,
project steps and chat. Nothing in the webapp answers "what was asked, what is queued, what is
running where, what broke, show me the screen, and what did we learn". A **job** is that one record.
The orchestration procedure that drives jobs lives in the Hermes skill `cs2-test-orchestration`.

## Concepts
- **Job** = one requested run: `kind` test | demo | final, a `target` (test name / feature / build
  under test), a host, the intake parameters, a status, a timeline, artifacts, and a mandatory
  close-out summary.
- Jobs are a **ledger + queue, not a lock.** They never claim the host lease: the canonical tools
  (director run, `compile_map.py`, `test_start`) keep claiming it as today, otherwise they would 409.
  A job links to those records instead.
- Queue order is per host: `position` ascending, then `created_at`. `GET /api/jobs/next?host_id=`
  returns the head queued job only when that host's lease is free (else 409 with the holder).

## Schema — migration `0012_jobs.sql`
```
jobs(
  id TEXT PK,                 -- 'j-1a2b3c4d'
  kind TEXT CHECK in ('test','demo','final'),
  title TEXT NOT NULL,        -- human name, shown on the HUD strip / banner
  target TEXT NOT NULL,       -- what is under test: feature/test name + branch@sha
  host_id TEXT REFERENCES hosts(id) ON DELETE SET NULL,   -- NULL = not yet assigned
  status TEXT CHECK in ('queued','running','passed','failed','error','cancelled') DEFAULT 'queued',
  position INTEGER NOT NULL DEFAULT 100,
  params TEXT NOT NULL DEFAULT '{}',   -- JSON intake (see Intake below)
  requested_by TEXT,          -- coordinator session id
  worker TEXT,                -- subagent id / branch running it
  phase TEXT NOT NULL DEFAULT '',      -- current phase label
  project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
  summary TEXT,               -- JSON {what, why, learned, next}  (required to close)
  created_at, started_at, finished_at, updated_at TEXT
)
job_events(
  id INTEGER PK AUTOINCREMENT, job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  level TEXT CHECK in ('info','warn','error','crash','screenshot','milestone','learning'),
  phase TEXT, message TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}',     -- JSON: log_tail, exit_code, artifact url, md5, test_run_id
  created_at TEXT
)
job_links(job_id, kind CHECK in ('test_run','build','deploy','evidence','project'), ref_id, PK(job_id,kind,ref_id))
```

## Intake (params) — server-validated on enqueue
Required: `kind`, `title`, `target`, `purpose` (one sentence), `expected` (pass criterion, observable),
`host_id` **or** `host_choice:"ask"` (stays queued+unassigned until `assign`).
Optional with defaults: `max_minutes` (5 for test/demo per the user rule; larger needs `reason`),
`needs_confirm` (false; true = the human `iso_confirm` gate decides pass/fail), `screenshots`
(`"each_step"` default | `"on_request"` | `"none"`), `watching` (bool: user is watching live),
`branch`, `commit`, `cleanup` (list of things that must be gone at the end).
Reject with 422 and a field list when required fields are missing — the intake is the gate.

## Lifecycle & API (all under /api; non-GET gated by the optional auth token)
| call | effect |
|---|---|
| `POST /jobs` | enqueue (validates intake) → 201 job |
| `GET /jobs?status=&host_id=&limit=` | list, newest first; `status=active` = queued+running |
| `GET /jobs/{id}` | job + events + links + artifacts |
| `GET /jobs/next?host_id=` | head of that host's queue if its lease is free; 409 + holder otherwise; 404 if empty |
| `POST /jobs/{id}/assign {host_id, position?}` | set/move host & order (queued only) |
| `POST /jobs/{id}/start {worker}` | queued → running, started_at |
| `POST /jobs/{id}/event {level, phase?, message, data?}` | append timeline; a `phase` updates `jobs.phase`; `error`/`crash` require `message` and SHOULD carry `data.log_tail` (capped 64 KB) |
| `PUT /jobs/{id}/artifacts/{name}` (raw body, Content-Type image/jpeg|png|text/plain|application/json|application/zip|application/octet-stream) | store under `<data_dir>/artifacts/<job_id>/<name>`, ≤ 25 MB, name `^[A-Za-z0-9._-]{1,120}$`, no overwrite (409) — append-only like committed evidence; auto-adds a `screenshot` event for images (`data.url`, `data.md5`, `data.bytes`). Crash artifacts `.dmp`/`.mdmp`/`.evtx` are octet-stream |
| `GET /artifacts/{job_id}/{name}` | serve the file (correct content type); non-images as a download (`Content-Disposition: attachment`) |
| `POST /jobs/{id}/link {kind, ref_id}` | attach test run / build / deploy / evidence / project |
| `POST /jobs/{id}/screenshot-request {note}` | adds an `info` event `screenshot requested` and sets `params.pending_shot=true`; the runner polls `GET /jobs/{id}` (or `/jobs/{id}/requests`) and answers by uploading; upload clears it |
| `POST /jobs/{id}/finish {status: passed|failed|error|cancelled, summary:{what,why,learned,next}}` | **gate:** all four summary fields non-empty, each ≤ 600 chars; running/queued only; sets finished_at |
| `POST /jobs/{id}/cancel {reason}` | queued/running → cancelled (summary optional; reason recorded) |

Push file (same pattern as confirms): every event appends `<data_dir>/job_events.jsonl`; `/api/health`
gains `job_events_jsonl` so a coordinator can watch locally instead of polling.

## MCP tools (mirror; writers `read_only_hint=False`, launch/record writers `idempotent_hint=False`)
`job_enqueue, job_list, job_get, job_next, job_assign, job_start, job_event, job_attach(job_id, path)`
(MCP runs on the Mac: copies a local file into artifacts), `job_request_screenshot, job_link,
job_finish, job_cancel`. Add them to `tests/test_mcp.py`'s explicit writer set.

## CLI (`s2docs job …`, exit 0 ok / 3 not found / 4 conflict / 9 validation — the global contract)
`enqueue --kind --title --target --purpose --expected (--host | --ask-host) [--max-minutes] [--confirm]`,
`list [--active] [--host]`, `show <id>`, `next --host`, `start <id> --worker`,
`event <id> --level --message [--phase] [--log-file F (tail 200 lines)]`,
`attach <id> <file> [--name]`, `shot-request <id>`, `finish <id> --status --what --why --learned --next`,
`cancel <id> --reason`, `watch <id>` (tail events until the job ends).

## Web UI — `#/jobs` and `#/jobs/<id>`
- Queue board: one column per host (+ "unassigned"): running job on top with phase + elapsed,
  then queued jobs in order. Status badges; error/crash counts in red.
- Job page: intake card (purpose, expected, cap, confirm), timeline (levels coloured, `crash`/`error`
  expanded with log tail in a `<pre>`), screenshot gallery (inline `<img>` from `/api/artifacts/...`),
  linked test runs/builds/deploys, a **"Request screenshot"** button, and the close-out
  **summary card** (what / why / learned / next) — the first thing shown on a finished job.
- Nav link "Jobs"; auto-refresh every 5 s while any job is running. Vanilla JS, no new deps.

## Client side (iso-spectate-view, follow-up branch `feature/dev/job-client`, after this lands)
`tools/host/job.py` thin client: `event`, `crash-collect <host>` (cs2 console.log tail, director.log tail,
Windows Application log last 20 entries for cs2/resourcecompiler, crash dumps list) → `crash` event +
artifacts; `shot <host> --job` = `peek.run_peek(..., shot=True)` then `PUT` the composed jpg;
`serve-requests --job` (poll pending_shot → shot). Real smoke on desktop-pc once, after the full demo
on pomps-pc ends.

## Tests (pytest, synthetic DB)
intake 422s per missing field; queue order + `next` 409 when lease held; event phase update; artifact
put/get/size cap/bad name/no overwrite/content type (incl. `.dmp`/`.mdmp`/`.evtx` octet-stream + attachment header); screenshot event auto-added; request→upload clears
pending; finish gate (missing/oversized summary field → 422); cancel; push jsonl + health field; MCP
tool registration + one end-to-end MCP round trip; CLI exit codes; web UI static check (route present).
