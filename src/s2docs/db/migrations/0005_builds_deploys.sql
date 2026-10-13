-- 0005: build runs + deployment registry (Phase 3 of docs/CLI_PLAN.md).
-- A build run is a server-owned record of one remote compile/command: the server holds the host
-- lease (host_locks owner 'build:<run-id>'), captures the remote output to a local log and
-- records status/exit here, so a dropped client can re-attach with `build status <id>` instead of
-- babysitting the process with a hand-rolled sleep loop. resourcecompiler writes its log as
-- UTF-16; readers must decode accordingly (see builds.py).
-- A deployment records the files copied to a host plus the MD5s verified server-side over SSH, so
-- two workers can no longer race a deploy with nothing checking the artifacts that actually landed.

CREATE TABLE build_runs (
    id         TEXT PRIMARY KEY,               -- run id, e.g. 'b-1a2b3c4d'
    host_id    TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'build',  -- free label: 'build', 'resourcecompiler', ...
    command    TEXT NOT NULL,                  -- shell command executed on the host over SSH
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    owner      TEXT NOT NULL,                  -- host-lock owner, 'build:<id>'
    handle     TEXT,                           -- process handle (pid) when known
    notes      TEXT,
    log_path   TEXT,                           -- server-local path of the captured log
    exit_code  INTEGER,
    summary    TEXT NOT NULL DEFAULT '{}',     -- JSON: parsed counts (compiled/failed/errors/...)
    artifacts  TEXT NOT NULL DEFAULT '[]',     -- JSON: [{path, md5, size}]
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX build_runs_host ON build_runs (host_id, created_at DESC);

CREATE TABLE deployments (
    id         TEXT PRIMARY KEY,               -- deploy id, e.g. 'd-1a2b3c4d'
    host_id    TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    addon      TEXT,
    dest_root  TEXT NOT NULL,                  -- remote destination root
    entries    TEXT NOT NULL DEFAULT '[]',     -- JSON: [{path, size, md5, verified}]
    status     TEXT NOT NULL DEFAULT 'pushed'
               CHECK (status IN ('pushed', 'verified', 'failed')),
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX deployments_host ON deployments (host_id, created_at DESC);
