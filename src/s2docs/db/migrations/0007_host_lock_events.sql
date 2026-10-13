-- 0007: host-lock audit trail for the web UI (Phase 6 — visibility pages).
-- host_locks is a single row per host (DELETE on release), so it answers "who holds this host right
-- now" but not "who held it, and when" — which is exactly what the Hosts page needs when a test run
-- looks stuck. host_lock_events appends one row per claim/takeover/release so the page can show the
-- lock history for a host. Events are low-volume (lock mutations, not heartbeats).

CREATE TABLE host_lock_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    owner   TEXT NOT NULL,                     -- owner of the lock at the time of the event
    event   TEXT NOT NULL CHECK (event IN ('claimed', 'released')),
    detail  TEXT,                              -- e.g. 'took over stale lock from ws/dead-worker'
    ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX host_lock_events_host ON host_lock_events (host_id, ts DESC, id DESC);
