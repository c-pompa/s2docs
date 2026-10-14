-- 0009: build_runs gains an 'interrupted' terminal status.
-- An orphaned run — still 'queued'/'running' after the owning server restarted, with no live watcher
-- in the new process — is finalized 'interrupted' at startup and its host lease released (only if
-- still owned by it). SQLite has no ALTER for a CHECK constraint, so the table is rebuilt and
-- back-filled. Nothing references build_runs, so the drop/rename is safe with PRAGMA foreign_keys = ON.

CREATE TABLE build_runs_new (
    id         TEXT PRIMARY KEY,
    host_id    TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'build',
    command    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    owner      TEXT NOT NULL,
    handle     TEXT,
    notes      TEXT,
    log_path   TEXT,
    exit_code  INTEGER,
    summary    TEXT NOT NULL DEFAULT '{}',
    artifacts  TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO build_runs_new (id, host_id, kind, command, status, owner, handle, notes, log_path, exit_code, summary, artifacts, created_at, updated_at)
    SELECT id, host_id, kind, command, status, owner, handle, notes, log_path, exit_code, summary, artifacts, created_at, updated_at
    FROM build_runs;
DROP TABLE build_runs;
ALTER TABLE build_runs_new RENAME TO build_runs;
CREATE INDEX build_runs_host ON build_runs (host_id, created_at DESC);
