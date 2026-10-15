-- 0013: jobs gains an 'inconclusive' terminal status.
--
-- The ledger was lying about runs that never got a verdict: a step-gated demo the user stopped
-- answering finished 'passed' with pass=0, fail=0, no-verdict=1 (jobs j-4a8bc044 / j-7a3258e2 on the
-- live server). A run with no verdict is not a pass, so 'inconclusive' is now a terminal status of its
-- own — what such a run ends as, and what a driver falls back to when it has no verdict to report.
--
-- SQLite has no ALTER for a CHECK constraint, so `jobs` is rebuilt (create new, copy, drop, rename).
--
-- `jobs` IS referenced by job_events and job_links (both ON DELETE CASCADE). With
-- PRAGMA foreign_keys = ON — set by db.connection.connect, and a no-op to change from inside a
-- migration, because the runner wraps every script in BEGIN/COMMIT — DROP TABLE jobs performs an
-- implicit DELETE that fires those cascades and silently wipes the whole timeline (proved on a copy:
-- 1 event + 1 link -> 0 + 0). So the children are stashed into plain copies, dropped, and recreated
-- verbatim (schema + index) after the swap. No FK is ever dropped while something references it, and
-- the rebuild runs entirely with foreign key enforcement ON.
--
-- Nothing else references jobs (no triggers, no views; artifacts are files under <data_dir>).

-- Stash the children: CREATE TABLE AS SELECT keeps the rows (and their ids) without carrying the FK.
CREATE TABLE job_events_stash AS SELECT * FROM job_events;
CREATE TABLE job_links_stash AS SELECT * FROM job_links;
DROP TABLE job_events;  -- dropping a child is safe: nothing references job_events / job_links
DROP TABLE job_links;

CREATE TABLE jobs_new (
    id           TEXT PRIMARY KEY,              -- job id, e.g. 'j-1a2b3c4d'
    kind         TEXT NOT NULL CHECK (kind IN ('test', 'demo', 'final')),
    title        TEXT NOT NULL,                 -- human name, shown on the HUD strip / banner
    target       TEXT NOT NULL,                 -- what is under test: feature/test name + branch@sha
    host_id      TEXT REFERENCES hosts(id) ON DELETE SET NULL,   -- NULL = not yet assigned
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'passed', 'failed', 'error', 'cancelled', 'inconclusive')),
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
INSERT INTO jobs_new (id, kind, title, target, host_id, status, position, params, requested_by, worker, phase,
                      project_id, summary, created_at, started_at, finished_at, updated_at)
    SELECT id, kind, title, target, host_id, status, position, params, requested_by, worker, phase,
           project_id, summary, created_at, started_at, finished_at, updated_at
    FROM jobs;
DROP TABLE jobs;                       -- no child references jobs at this point -> no cascade, no loss
ALTER TABLE jobs_new RENAME TO jobs;

-- The indexes 0012 created on jobs, in their original form.
CREATE INDEX jobs_queue ON jobs (host_id, status, position, created_at);
CREATE INDEX jobs_status ON jobs (status, created_at DESC);

-- The children come back exactly as 0012 defined them (same columns, CHECKs, PK and index), then the
-- stashed rows are restored. Ids are copied explicitly so the timeline keeps its order and its
-- AUTOINCREMENT high-water mark (sqlite_sequence is re-seeded from the largest id inserted).
CREATE TABLE job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    level      TEXT NOT NULL
               CHECK (level IN ('info', 'warn', 'error', 'crash', 'screenshot', 'milestone', 'learning')),
    phase      TEXT,                            -- the phase the job was in when this happened
    message    TEXT NOT NULL,
    data       TEXT NOT NULL DEFAULT '{}',      -- JSON: log_tail, exit_code, artifact url, md5, test_run_id
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO job_events (id, job_id, level, phase, message, data, created_at)
    SELECT id, job_id, level, phase, message, data, created_at FROM job_events_stash;
CREATE INDEX job_events_job ON job_events (job_id, created_at, id);
DROP TABLE job_events_stash;

CREATE TABLE job_links (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind   TEXT NOT NULL CHECK (kind IN ('test_run', 'build', 'deploy', 'evidence', 'project')),
    ref_id TEXT NOT NULL,
    PRIMARY KEY (job_id, kind, ref_id)
);
INSERT INTO job_links (job_id, kind, ref_id) SELECT job_id, kind, ref_id FROM job_links_stash;
DROP TABLE job_links_stash;
