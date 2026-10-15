# Test jobs — queue, live reporting, screenshots, crash logs, close-out summary (v1)

A **job** is one requested run (`kind` `test` | `demo` | `final`): the intake ("what was asked, and the
pass criterion"), a timeline, artifacts (screenshots, logs, bundles), links to the canonical records,
and a mandatory close-out summary. It answers "what is queued, what is running where, what broke, show
me the screen, and what did we learn" in the webapp instead of across test runs, host locks, evidence
rows, project steps and chat.

Jobs are a **ledger + queue, not a lock.** They never claim the host lease — the canonical tools
(director run, `compile_map.py`, `test_start`) keep claiming it, otherwise they would 409. A job links
to those records instead. Queue order is per host: `position` ascending, then `created_at`.

Intake is the gate. Required: `kind`, `title`, `target`, `purpose` (one sentence), `expected` (an
observable pass criterion), and `host_id` **or** `host_choice:"ask"` (stays queued + unassigned). A
missing/invalid intake is one `422` naming every offending field. Optional: `max_minutes` (default 5;
a larger test/demo value needs `reason`), `needs_confirm` (the human `iso_confirm` gate decides
pass/fail), `screenshots` (`each_step` default | `on_request` | `none`), `watching`, `branch`,
`commit`, `cleanup`.

## REST (all under `/api`; non-GET needs the auth token when `S2DOCS_AUTH_TOKEN` is set)

| call | effect |
|---|---|
| `POST /jobs` | enqueue (validates intake) → `201` job; `422` with `fields` on a bad intake |
| `GET /jobs?status=&host_id=&limit=` | list, newest first; `status=active` = queued + running |
| `GET /jobs/next?host_id=` | head of that host's queue if its lease is free; `409` + holder otherwise; `404` if empty |
| `GET /jobs/{id}` | job + events + links + artifacts + `current_step` (the step awaiting a verdict; see below) |
| `POST /jobs/{id}/assign {host_id, position?}` | set/move host & queue order (queued only) |
| `POST /jobs/{id}/start {worker}` | queued → running, `started_at` |
| `POST /jobs/{id}/event {level, phase?, message, data?}` | append timeline; a `phase` updates `jobs.phase`; `error`/`crash` need a message and should carry `data.log_tail` (capped 64 KB) |
| `PUT /jobs/{id}/artifacts/{name}` | raw body, `Content-Type` one of `image/jpeg\|png`, `text/plain`, `application/json`, `application/zip`, `application/octet-stream` (crash dumps `.dmp`/`.mdmp`, Windows event logs `.evtx`); stored under `<data_dir>/artifacts/<job_id>/<name>`, ≤ 25 MB, name `^[A-Za-z0-9._-]{1,120}$`, no overwrite (`409`). An image auto-adds a `screenshot` event; any upload clears `pending_shot` |
| `GET /artifacts/{job_id}/{name}` | serve the file with the content type derived from its extension; non-images (logs, json, dumps, zips) come back as a download (`Content-Disposition: attachment`), images stay inline |
| `GET /jobs/{id}/screenshots?step=&reason=` | the job's screenshots tied to their step and verdict (`seq`, `step`, `step_index`, `step_total`, `reason` ready\|periodic\|verdict\|request\|verify, `url`, `web_url`, `name`, `md5`, `bytes`, `verdict{verdict,source,confirm_id,late}`, `full_res`, `width`, `height`, `created_at`). `step='unassigned'` selects the older shots with no step tag; a bad `reason` is `422` — see **Screenshots** below |
| `POST /jobs/{id}/link {kind, ref_id}` | attach a test run / build / deploy / evidence / project |
| `POST /jobs/{id}/verdict {verdict, notes?, step?, source?}` | answer the running job's step with a human verdict — see **Verdicts** below |
| `POST /jobs/{id}/screenshot-request {note}` | adds an `info` event and sets `params.pending_shot=true`; the runner answers by uploading |
| `POST /jobs/{id}/finish {status, summary}` | gate: all four of `what`/`why`/`learned`/`next` non-empty, each ≤ 600 chars (`422` + `fields` otherwise); queued/running only. `status` ∈ `passed`/`failed`/`error`/`cancelled`/`inconclusive` — see **Inconclusive** below |
| `POST /jobs/{id}/restatus {status, reason}` | correct a **finished** job's terminal status (terminal → terminal) with a mandatory `reason`; queued/running is `409`, a no-op change is `409`, a non-terminal target or a missing reason is `422` — see **Inconclusive** below |
| `POST /jobs/{id}/cancel {reason}` | queued/running → cancelled (the reason is recorded) |

`422` carries `{"detail", "fields"}`; a state conflict is `409` (`{"detail", "conflict": true}`); an
unknown job is `404`.

## Verdicts — respond from the webapp

A demo driver runs the steps and waits for the human's call on each one. A verdict is `pass` | `fail` |
`skip` (skip was added so a step that cannot be exercised can be moved past; a parallel director worker
sends it). It can come from four places, and the webapp is now one of them:

| source | how |
|---|---|
| `chat` | in game: `!pass NOTE` (the director posts a `human-confirm` evidence row + a job milestone) |
| `console` | the game console |
| `key` | a key bind |
| `web` | the job page's Pass/Fail/Skip buttons (`POST /jobs/{id}/verdict`) |
| `coordinator` | the coordinator relaying a verdict the user typed in the Hermes chat (`job_verdict`/CLI) |

`POST /jobs/{id}/verdict` body `{verdict, notes?, step?, source?}` (source defaults to `web`): the job
must be **running** (`409` otherwise) and must **link a test run** (`kind='test_run'`, most recent —
`409` with a clear message otherwise). It writes a `human-confirm` evidence row
`{test_run_id, data:{verdict, notes, step, source, job_id}}` **through the same code path as
`POST /api/evidence`** (`evidence.add_and_publish`) so the confirm push files
(`confirms.jsonl` + `latest_confirm.json`, see `docs/CONFIRM_PUSH.md`) fire, then appends a job event
level `milestone`, message `verdict VERDICT (SOURCE): NOTES`, data `{verdict, notes, step, source,
evidence_id}`.

Verdict validation lives on that shared evidence path: a `human-confirm` row must carry
`data.verdict ∈ {pass, fail, skip}` (required) and, when present, `data.source ∈ {chat, console, key,
web, coordinator, director}` — otherwise `422` naming `verdict`/`source`. (`director` is the tag the
in-game confirm gate's own producer already used.)

### The driver's step prompt + `current_step`

Per step the driver posts an `info` event with `phase` = the step id and
`data {prompt, look_for, step_index, step_total, respond_hint, deadline_utc}`. `GET /jobs/{id}` returns
`current_step` = the newest such prompt (+`phase`) plus `verdict` (set once a *verdict* milestone — any
`milestone` event carrying `data.verdict` — is newer than the prompt, from **any** source). It is
`null` when no prompt has been posted. `list_jobs` does not compute it (no events loaded).

## Inconclusive — a run that ended without a verdict

A step-gated demo the user stopped answering has no verdict to report (pass=0, fail=0, no-verdict=1).
It used to finish `passed`, which made the ledger claim a verdict nobody made. `status='inconclusive'`
is the terminal status such a run ends as; it carries the **same** close-out summary gate as any other
finish, so the record still says what happened and what is next. A demo driver posts it to
`POST /jobs/{id}/finish` (falling back to `error` if the gate returns `422`).

`inconclusive` is terminal: it counts as finished in the webapp (badge amber/grey, **never** the amber
of `passed` or the red of `failed`), it is excluded from `status=active`, and `s2docs job watch` treats
it as an end (exit `1`, like any non-`passed` end).

**Correcting a close-out** — `POST /jobs/{id}/restatus {status, reason}` (CLI `s2docs job restatus`)
re-states a finished job between terminal statuses, for a mis-recorded verdict. Only terminal →
terminal (a queued/running job finishes or is cancelled; `restatus` never reopens one, so it can never
re-enter a queue), the target must differ from the current status, and `reason` is **mandatory** —
it lands as a `milestone` timeline event (`data.restatus {from, to, reason}`) so the correction is
auditable. `finished_at` and the summary are left untouched: the run ended when it ended, this only
re-states the verdict.

## MCP

`job_enqueue, job_list, job_get, job_screenshots, job_next, job_assign, job_start, job_event, job_attach(job_id, path),
job_request_screenshot, job_link, job_verdict, job_finish, job_restatus, job_cancel`. `job_attach` runs on the Mac and copies a
local file into the job's artifacts (`path`, optional `name`); `job_verdict` answers the running job's step
(`verdict` pass|fail|skip, optional `notes`/`step`, and `source` — use `coordinator` when relaying a verdict the
user typed in the Hermes chat). Readers advertise `read_only_hint`; writers set `read_only_hint=False`, and every
tool that creates a record, appends an event or launches work advertises `idempotent_hint=False` (so a retry never
silently becomes a second job/event/artifact/verdict row).

## CLI

```
s2docs job enqueue --kind --title --target --purpose --expected (--host H | --ask-host)
                   [--max-minutes N] [--reason ...] [--confirm] [--screenshots ...] [--watching]
s2docs job list [--active] [--host H]
s2docs job show <id>
s2docs job next --host H
s2docs job start <id> --worker W
s2docs job event <id> --level L --message M [--phase P] [--data JSON] [--log-file F]   # tail 200 lines
s2docs job attach <id> <file> [--name NAME]
s2docs job shot-request <id> [--note ...]
s2docs job verdict <id> --verdict pass|fail|skip [--notes ...] [--step ...] [--source coordinator]
s2docs job finish <id> --status S --what W --why Y --learned L --next N   # S: passed|failed|error|cancelled|inconclusive
s2docs job restatus <id> --status S --reason R   # correct a FINISHED job (terminal -> terminal; reason required)
s2docs job cancel <id> --reason R
s2docs job screenshots <id> [--step S] [--reason R]
s2docs job watch <id>          # tail events until the job ends
```

Every `s2docs job` command prints a `VIEW: <url>` line (the job's web page) in its human output; the
same URL rides in the JSON as `job.web_url`.

`job verdict` is how the coordinator relays a verdict the user typed in the Hermes chat; its `--source`
defaults to `coordinator` (the webapp posts `web`).

Exit codes (the global contract, shared with every other subcommand): `0` ok, `1` error, `2` locked,
`3` not found, `4` conflict (illegal transition / held lease / existing artifact), `9` validation
(intake/summary gate). The `job` subcommand uses `3`/`4`/`9` — no job-local aliases, so an agent can
always read one meaning per code. Pass the global `--json` for one `{"schema": "s2docs/v1", …}` object
on stdout.

## Web UI

- `#/jobs` — the **queue board**: one column per registered host (plus "unassigned"), the running job
  on top with phase + elapsed, then queued jobs by position; status badges and error/crash counts in
  red; a finished-jobs table below. A job that ended without a verdict shows the `inconclusive` badge
  (muted, dashed — never the amber of `passed`), in the finished table, on the job page and on the
  Activity page's chain heading. Auto-refreshes every 5 s while a job is running, 15 s when idle.
- `#/jobs/<id>` — the job page, organized into a fixed hierarchy so a long run scans:
  **HEADER** (title, status/kind badges, host/kind/worker/phase, started/finished/duration, project,
  purpose + expected, and a **Copy job URL** button) → **NOW / STATE** (the current-test card with the
  `TEST k/n` stepper, what-is-tested, what-you-should-see, a client-side countdown to `deadline_utc`,
  the camera line, and Pass/Fail/Skip; when the job is finished this collapses to a compact
  "Run passed/failed/…" banner plus the stepper) → **RESULTS** (one row per step: verdict badge, source,
  note, late flag, when, and its screenshots with the verdict shot marked "screenshot at verdict") →
  **SUMMARY** (four labelled blocks what/why/learned/next) → **INTAKE & CLEANUP** → **EVIDENCE**
  (screenshots grouped by step, click-to-enlarge at full resolution) → **LINKS** (test run, project,
  related jobs) → **TIMELINE** (grouped by step into collapsible blocks, level-coloured; routine info
  lines — heartbeats, repeated screenshot/poll lines — fold into a "show N more" toggle while
  warn/error/crash/milestone/learning and user-feedback/ledger-correction stay visible).
  Auto-refreshes every 5 s. The verdict + screenshot-request forms are bound by ONE **delegated**
  `submit` listener (not per form), so a refresh can never leave the Pass/Fail/Skip buttons without a
  handler and silently drop a verdict; a stale `?verdict=…` query is stripped and shown as a dismissible
  "please re-send" notice instead of being auto-submitted.
- **SETTING UP / CLEANING UP** — while a demo driver stages the next test (or tears down the last) it
  posts an `info` event `{phase:'setup'|'cleanup', data:{countdown_s, until:<UTC ISO>, step,
  step_index, step_total, name, next}}`. The NOW area shows a banner (`SETTING UP: TEST k/n NAME  8s` /
  `CLEANING UP: TEST k/n → next: …  6s`) ticking down client-side from `until`, and the verdict buttons
  are disabled — reading "opens in Ns" during setup, "window closed" (with "recorded" if a verdict
  landed) during cleanup. A later step prompt or verdict supersedes the banner.

## web_url everywhere

Every REST row for a **job**, **test run**, **project**, **build run** and **deployment** carries an
absolute `web_url` the caller can open (single + list). It is `<base>/#/<route>`:

| entity | route |
|---|---|
| job | `#/jobs/<id>` |
| project | `#/<game>/projects/<id>` |
| test run / build / deploy | `#/activity` (these have no dedicated page; they are shown on Activity) |

The `<base>` resolves in this order (`s2docs/weburls.py`):
1. **`S2DOCS_PUBLIC_URL`** setting, when set (trailing slash stripped);
2. else the incoming REST request's `scheme://host:port` — a browser on `http://10.88.15.208:8812`
   gets URLs back on that same origin;
3. else the LAN fallback `http://10.88.15.208:8812` — used by the MCP tools and the CLI, which have
   no request.

`GET /api/health` (and MCP `server_info`) carry **`public_url`**. `s2docs job …` prints a
`VIEW: <url>` line; the MCP `job_get`/`job_list` results carry it too.

## Screenshots — tied to steps and verdicts

A `screenshot` job event may carry (the demo driver adds these; older events keep only `url`, `md5`,
`bytes` and read as **unassigned**):

```
{ url, md5, bytes, step, step_index, step_total, reason, seq,
  verdict: { verdict, source, confirm_id, late } | null, full_res, width, height }
```

`reason` ∈ `ready | periodic | verdict | request | verify`; `seq` is 1-based per job. The web page
groups shots by step (captions `#seq · reason · HH:MM:SS · step`, plus the verdict + source on verdict
shots), shows them as thumbnails in the RESULTS table, as a click-to-enlarge grid under EVIDENCE, and
as a camera line in the NOW card (`📷 N shots this test — last HH:MM:SS (reason)`); a recorded verdict
links its shot (matched on `verdict.confirm_id`, else the step, else the newest verdict shot) and says
"a screenshot will be saved with your verdict" until it arrives. `GET /api/jobs/{id}/screenshots`
(and MCP `job_screenshots`) returns the same list, filterable by `step`/`reason`, for a coordinator
pulling image context after a run.

## Push file

Every job event also appends one line to `<data_dir>/job_events.jsonl` (advertised on `GET /api/health`
as `job_events_jsonl`), so a coordinator can watch a local file instead of polling — the same push
pattern as the human-confirm channel (`docs/CONFIRM_PUSH.md`). The push is a best-effort side channel:
the DB row is durable before it runs, so a filesystem hiccup is logged and swallowed.
