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
| `GET /jobs/{id}` | job + events + links + artifacts |
| `POST /jobs/{id}/assign {host_id, position?}` | set/move host & queue order (queued only) |
| `POST /jobs/{id}/start {worker}` | queued → running, `started_at` |
| `POST /jobs/{id}/event {level, phase?, message, data?}` | append timeline; a `phase` updates `jobs.phase`; `error`/`crash` need a message and should carry `data.log_tail` (capped 64 KB) |
| `PUT /jobs/{id}/artifacts/{name}` | raw body, `Content-Type` one of `image/jpeg\|png`, `text/plain`, `application/json`, `application/zip`, `application/octet-stream` (crash dumps `.dmp`/`.mdmp`, Windows event logs `.evtx`); stored under `<data_dir>/artifacts/<job_id>/<name>`, ≤ 25 MB, name `^[A-Za-z0-9._-]{1,120}$`, no overwrite (`409`). An image auto-adds a `screenshot` event; any upload clears `pending_shot` |
| `GET /artifacts/{job_id}/{name}` | serve the file with the content type derived from its extension; non-images (logs, json, dumps, zips) come back as a download (`Content-Disposition: attachment`), images stay inline |
| `POST /jobs/{id}/link {kind, ref_id}` | attach a test run / build / deploy / evidence / project |
| `POST /jobs/{id}/screenshot-request {note}` | adds an `info` event and sets `params.pending_shot=true`; the runner answers by uploading |
| `POST /jobs/{id}/finish {status, summary}` | gate: all four of `what`/`why`/`learned`/`next` non-empty, each ≤ 600 chars (`422` + `fields` otherwise); queued/running only |
| `POST /jobs/{id}/cancel {reason}` | queued/running → cancelled (the reason is recorded) |

`422` carries `{"detail", "fields"}`; a state conflict is `409` (`{"detail", "conflict": true}`); an
unknown job is `404`.

## MCP

`job_enqueue, job_list, job_get, job_next, job_assign, job_start, job_event, job_attach(job_id, path),
job_request_screenshot, job_link, job_finish, job_cancel`. `job_attach` runs on the Mac and copies a
local file into the job's artifacts (`path`, optional `name`). Readers advertise `read_only_hint`;
writers set `read_only_hint=False`, and every tool that creates a record, appends an event or launches
work advertises `idempotent_hint=False` (so a retry never silently becomes a second job/event/artifact).

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
s2docs job finish <id> --status S --what W --why Y --learned L --next N
s2docs job cancel <id> --reason R
s2docs job watch <id>          # tail events until the job ends
```

Exit codes (the global contract, shared with every other subcommand): `0` ok, `1` error, `2` locked,
`3` not found, `4` conflict (illegal transition / held lease / existing artifact), `9` validation
(intake/summary gate). The `job` subcommand uses `3`/`4`/`9` — no job-local aliases, so an agent can
always read one meaning per code. Pass the global `--json` for one `{"schema": "s2docs/v1", …}` object
on stdout.

## Web UI

- `#/jobs` — the **queue board**: one column per registered host (plus "unassigned"), the running job
  on top with phase + elapsed, then queued jobs by position; status badges and error/crash counts in
  red; a finished-jobs table below. Auto-refreshes every 5 s while a job is running, 15 s when idle.
- `#/jobs/<id>` — the job page: the close-out **summary card** first on a finished job, then the intake
  (purpose, expected, cap, confirm gate), a "Request screenshot" button (a themed input for the capture
  note), the screenshot gallery (inline `<img>` from `/api/artifacts/...` with the md5 wrapped inside the
  card), then a **Files** list of the non-image artifacts (`log`/`txt`/`json`/`dmp`/`zip`) with download
  links, linked records, and the timeline (levels coloured, `crash`/`error` expanded with the log tail in
  a `<pre>`).

## Push file

Every job event also appends one line to `<data_dir>/job_events.jsonl` (advertised on `GET /api/health`
as `job_events_jsonl`), so a coordinator can watch a local file instead of polling — the same push
pattern as the human-confirm channel (`docs/CONFIRM_PUSH.md`). The push is a best-effort side channel:
the DB row is durable before it runs, so a filesystem hiccup is logged and swallowed.
