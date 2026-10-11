-- 0003: orchestration. A project is one run of a workflow; steps are snapshotted at start so
-- later workflow edits never change a project in flight.

CREATE TABLE projects (
    id         TEXT PRIMARY KEY,
    workflow   TEXT NOT NULL,
    title      TEXT NOT NULL,
    goal       TEXT NOT NULL,
    game       TEXT NOT NULL,
    addon      TEXT,
    inputs     TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done', 'abandoned')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE project_steps (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id      TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    instructions TEXT NOT NULL,
    tools        TEXT NOT NULL DEFAULT '[]',   -- JSON list of tool names
    prompt       TEXT,                         -- JSON {"name": ..., "args": {...}} or NULL
    depends_on   TEXT NOT NULL DEFAULT '[]',   -- JSON list of step ids
    gate         TEXT,                         -- server-enforced completion check
    done_when    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'in_progress', 'done', 'skipped', 'blocked')),
    notes        TEXT,
    artifacts    TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings (paths, URLs, summaries)
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (project_id, step_id)
) WITHOUT ROWID;

CREATE TABLE project_events (
    id         INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id    TEXT,
    event      TEXT NOT NULL,
    detail     TEXT,
    ts         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX project_events_project ON project_events (project_id, id);
