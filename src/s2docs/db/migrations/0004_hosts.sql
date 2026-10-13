-- 0004: host registry + server-side host locks.
-- The registry answers "what is this machine" (shell, node path, GPU, Steam account, paths)
-- so agent briefs stop restating machine trivia. host_locks is the single source of truth for
-- "who is driving this host right now" (replaces hand-maintained lock files on the hosts):
-- one lock per host, with owner (workstream/session id), heartbeat and TTL; a lock whose
-- heartbeat is older than its TTL is stale and can be taken over (crashed/interrupted workers).

CREATE TABLE hosts (
    id         TEXT PRIMARY KEY,              -- short name, e.g. 'desktop-pc'
    hostname   TEXT,                          -- e.g. 'DESKTOP-39NF657'
    ssh_target TEXT NOT NULL,                 -- ssh alias or user@ip
    ssh_key    TEXT,                          -- identity file path, NULL = ssh default
    shell      TEXT CHECK (shell IN ('cmd', 'powershell')),
    os         TEXT,
    facts      TEXT NOT NULL DEFAULT '{}',    -- JSON: gpu, ram, node_path, cs2_path, steam ids, ...
    probed_at  TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE host_locks (
    host_id      TEXT PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    owner        TEXT NOT NULL,               -- workstream/session id, e.g. 'ws/occluder-wire'
    acquired_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    heartbeat_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ttl_sec      INTEGER NOT NULL DEFAULT 1800
) WITHOUT ROWID;
