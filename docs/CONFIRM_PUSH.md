# Confirm PUSH channel (human-confirm evidence → local files)

A Hermes agent waiting on a human verdict cannot listen on HTTP, and polling
`GET /api/evidence` every ~5 s is slow and wasteful. But the agent **can** cheaply read and stat a
local file — and the s2docs server runs on the same machine. So the server pushes every
human-confirm row to two files the agent watches instead.

## What the server writes

On every human-confirm row — a `POST /api/evidence` with `kind="human-confirm"`, or a verdict from
the job page's Pass/Fail/Skip buttons (`POST /api/jobs/{id}/verdict`, see *Where a verdict comes
from*) — the server also writes into the data dir (`S2DOCS_DATA_DIR`, e.g. `…/valve-mcp-api-project/data`):

| File | Shape | Write |
| --- | --- | --- |
| `confirms.jsonl` | append-only journal, one compact JSON record per confirm | append + `fsync` |
| `latest_confirm.json` | the single newest record | atomic: tmp file → `fsync` → `os.replace` |

A record is the full evidence row:

```json
{"id":"e-1a2b3c4d","project_id":null,"step_id":null,"kind":"human-confirm",
 "ref":null,"data":{"verdict":"pass","notes":"wall looked clean"},
 "test_run_id":"t-5e1fb5cc","created_at":"2026-10-14T19:31:02Z"}
```

`created_at` is the timestamp; `test_run_id` and `data` identify the verdict.
`latest_confirm.json` holds **byte-identical** JSON to the last `confirms.jsonl` line, so "is the
latest the confirm I'm waiting for?" is a string compare.

The push is best-effort: the durable evidence row is committed to the DB first, so a filesystem
error is logged and swallowed, never returned to the caller.

## Where a verdict comes from

Every human-confirm row is one verdict, and the server records which channel produced it in
`data.source` (`models.ConfirmSource`):

| source | channel |
| --- | --- |
| `chat` | in-game chat — `!pass` / `!fail` / `!skip` plus a note typed in chat |
| `console` | the game console |
| `key` | **KP_ENTER** — the confirm key bind |
| `web` | the **job page's** Pass/Fail/Skip buttons |
| `coordinator` | a Hermes coordinator relaying a verdict the user typed in chat |
| `director` | the in-game confirm gate's own producer |

Whichever channel it came from, the row lands in the DB and fires the same push files. A verdict made
from the **job page** (`POST /api/jobs/{id}/verdict`) goes through the same `evidence.add_and_publish`
path as `POST /api/evidence`, so it too lands in `confirms.jsonl` + `latest_confirm.json`. Its row
anchors to the job's current/last linked test run (`test_run_id`), carries `ref` = the job id so the
confirm traces back to the page it came from, and tags its note `"… via job page"` — so a waiting agent
can tell a page verdict from a chat/console/key one.

A verdict needs a linked test run to anchor on: a job-page verdict on a job with none is a 409 and
nothing is pushed. And only a real verdict publishes — other job events (log lines, progress) do not.

## Discovering the paths

Do **not** hardcode the paths. Read them from `GET /api/health` (no auth; the same fields ship on
the MCP `server_info` tool):

```json
{"status":"ok","db_path":"…/data/s2docs.db","data_dir":"…/data","pid":50232,
 "confirms_jsonl":"…/data/confirms.jsonl","latest_confirm":"…/data/latest_confirm.json", …}
```

## The agent polling pattern

Watch a local file on a short interval (1–2 s), remember the last record id you have seen, and stop
with a verdict. **A timeout must stop and report, never auto-proceed** — an absent human is not a
"pass".

```python
import json, time, urllib.request

def wait_for_confirm(base_url, run_id, timeout_s=600, interval_s=1.5):
    """Return the human-confirm row for run_id, or None on timeout (caller must NOT treat None as pass)."""
    health = json.load(urllib.request.urlopen(f"{base_url}/api/health"))
    latest_path = health["latest_confirm"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            rec = json.load(open(latest_path))          # atomic file: never a torn read
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(interval_s)
            continue
        if rec.get("test_run_id") == run_id:
            return rec                                   # the confirm we were waiting for
        time.sleep(interval_s)
    return None                                          # timed out — report, do not proceed

rec = wait_for_confirm("http://localhost:8812", "t-5e1fb5cc", timeout_s=600)
if rec is None:
    raise SystemExit("no human confirm within the timeout — reporting, not proceeding")
verdict = rec["data"]["verdict"]                          # "pass" / "fail"
```

Notes / gotchas:

- `latest_confirm.json` is rewritten atomically, so a reader sees the whole previous or the whole
  new record — never a partial one. `confirms.jsonl` is the full journal if you need history.
- Match on `test_run_id`: several runs can be in flight, and `latest_confirm.json` may belong to
  another one. `confirms.jsonl` is a cheap second source if you need every confirm in order.
- Poll at 1–2 s. That is 3–5× faster than the old 5 s HTTP poll and costs a local `read`, no server
  load; you can go faster with `os.stat` mtime checks if you want to skip the read when unmoved.
- The timeout is the whole point: when the human never answers, stop, report "no confirm", and let
  the caller decide. Never fall through to proceeding.
