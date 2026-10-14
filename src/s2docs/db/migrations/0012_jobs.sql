-- 0012: test jobs — one durable record per requested run (the ledger + queue).
--
-- A CS2 test / demo / final-product run is today spread over test runs, host locks, evidence rows,
-- project steps and chat, so nothing answers "what was asked, what is queued, what is running where,
-- what broke, show me the screen, and what did we learn". A job is that one record.
--
-- Jobs are a ledger + queue, NOT a lock: they never claim the host lease (the canonical tools —
-- director run, compile_map.py, test_start — keep claiming it, otherwise those would 409). A job
-- links to those records instead. Queue order is per host: position ascending, then created_at.

CREATE TABLE jobs (
    id           TEXT PRIMARY KEY,              -- job id, e.g. 'j-1a2b3c4d'
    kind         TEXT NOT NULL CHECK (kind IN ('test', 'demo', 'final')),
    title        TEXT NOT NULL,                 -- human name, shown on the HUD strip / banner
    target       TEXT NOT NULL,                 -- what is under test: feature/test name + branch@sha
    host_id      TEXT REFERENCES hosts(id) ON DELETE SET NULL,   -- NULL = not yet assigned
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'passed', 'failed', 'error', 'cancelled')),
    position     INTEGER NOT NULL DEFAULT 100,  -- per-host queue order (ascending)
    params       TEXT NOT NULL DEFAULT '{}',    -- JSON intake (purpose, expected, caps, flags)
    requested_by TEXT,                          -- coordinator session id
    worker       TEXT,                          -- subagent id / branch running it
    phase        TEXT NOT NULL DEFAULT '',      -- current phase label
    project_id   TEXT REFERENCES projects(id) ON DELETE SET NULL,
    summary      TEXT,                          -- JSON {what, why, learned, next} (required to close)
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at   TEXT,
    finished_at  TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- The job timeline: every event a runner posts (levels colour the UI; 'crash'/'error' carry a log tail).
CREATE TABLE job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    level      TEXT NOT NULL
               CHECK (level IN ('info', 'warn', 'error', 'crash', 'screenshot', 'milestone', 'learning')),
    phase      TEXT,                            -- the phase the job was in when this happened
    message    TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}',      -- JSON: log_tail, exit_code, artifact url, md5, test_run_id
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) ;

-- Attachments to the canonical records the job is about (test run / build / deploy / evidence / project).
CREATE TABLE job_links (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind   TEXT NOT NULL CHECK (kind IN ('test_run', 'build', 'deploy', 'evidence', 'project')),
    ref_id TEXT NOT NULL,
    PRIMARY KEY (job_id, kind, ref_id)
);

-- The queue board reads a host's active jobs in order; the list reads newest-first.
CREATE INDEX jobs_queue ON jobs (host_id, status, position, created_at);
CREATE INDEX jobs_status ON jobs (status, created_at DESC);
CREATE INDEX job_events_job ON job_events (job_id, created_at, id);
