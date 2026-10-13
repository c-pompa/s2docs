-- 0008: lock phases + server-computed stuck-ness.
-- The 2026-10-13 incident: a build worker heartbeat its lock faithfully while its compile sat hung
-- for 95 minutes, so the lock read 'fresh' and the dashboard looked healthy. Liveness (a fresh
-- heartbeat) is not progress. A lock now carries *what the owner is doing* (phase) and *since when*
-- (phase_since); the server compares the phase age against a configurable threshold and reports
-- lock.stuck. Heartbeating the same phase keeps phase_since, so a genuinely hung run stays STUCK;
-- changing the phase (or the owner) resets it. A lock with no phase (an old client) is never STUCK.

ALTER TABLE host_locks ADD COLUMN phase TEXT NOT NULL DEFAULT '';  -- e.g. 'compile', 'test'; '' = not reported
ALTER TABLE host_locks ADD COLUMN phase_since TEXT;                -- when the current phase started (NULL when '')
ALTER TABLE host_locks ADD COLUMN note TEXT;                       -- free text from the owner (e.g. current step)

-- host_lock_events is rebuilt so it can record a 'phase' transition and carry the new phase value.
-- SQLite has no ALTER for a CHECK constraint; the table is recreated and back-filled. Nothing else
-- references host_lock_events (only the FK to hosts, which survives), so the drop/rename is safe
-- with PRAGMA foreign_keys = ON.
CREATE TABLE host_lock_events_new (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    owner   TEXT NOT NULL,                     -- owner of the lock at the time of the event
    event   TEXT NOT NULL CHECK (event IN ('claimed', 'released', 'phase')),
    phase   TEXT,                              -- the phase the event moved to ('phase' events)
    detail  TEXT,                              -- e.g. 'took over stale lock from ws/dead-worker'
    ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO host_lock_events_new (id, host_id, owner, event, phase, detail, ts)
    SELECT id, host_id, owner, event, NULL, detail, ts FROM host_lock_events;
DROP TABLE host_lock_events;
ALTER TABLE host_lock_events_new RENAME TO host_lock_events;
CREATE INDEX host_lock_events_host ON host_lock_events (host_id, ts DESC, id DESC);
