-- 0006: evidence store + test-run records (Phases 4+5 of docs/CLI_PLAN.md).
--
-- EVIDENCE. The old `evidence` gate only checked that notes/artifacts were present, which let a step
-- be marked done before its README even existed. An evidence row is a structured, server-stored
-- record linked to a project step: what kind of proof it is (console transcript, file hash, pixel
-- diff, screenshot, note, other), where it points (`ref`) and the structured detail (`data` JSON,
-- e.g. pixel-diff pct + noise floor, md5s, transcript excerpt). The rewired gate requires at least
-- one non-note row for the step, so notes alone no longer pass.
--
-- TEST RUNS. A server-side record of an in-game test session, with exactly the fields the
-- iso-spectate TEST STATUS HUD strip shows (name, step, state, elapsed). A worker driving the HUD
-- over netcon (`iso_test start|step|pass|fail|off`) mirrors each transition here with one call.
-- elapsed is derived from started_at/finished_at; host_id/project_id are optional links (a session
-- may run outside any project, and finishing releases the caller's host lease).

CREATE TABLE evidence (
    id         TEXT PRIMARY KEY,               -- evidence id, e.g. 'e-1a2b3c4d'
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id    TEXT,                           -- NULL = project-level evidence (not tied to a step)
    kind       TEXT NOT NULL
               CHECK (kind IN ('console_transcript', 'file_hash', 'pixel_diff', 'screenshot', 'note', 'other')),
    ref        TEXT,                           -- path / url / command the evidence points at
    data       TEXT NOT NULL DEFAULT '{}',     -- JSON detail: md5s, pixel diff pct + noise floor, transcript excerpt
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- Cascades with its step; only enforced when step_id is set (SQLite MATCH SIMPLE ignores a
    -- composite FK with any NULL part), so project-level rows are still valid.
    FOREIGN KEY (project_id, step_id) REFERENCES project_steps (project_id, step_id) ON DELETE CASCADE
);
CREATE INDEX evidence_step ON evidence (project_id, step_id, created_at DESC);
CREATE INDEX evidence_project ON evidence (project_id, created_at DESC);

CREATE TABLE test_runs (
    id          TEXT PRIMARY KEY,              -- test-run id, e.g. 't-1a2b3c4d'
    host_id     TEXT REFERENCES hosts(id) ON DELETE SET NULL,
    project_id  TEXT REFERENCES projects(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,                 -- shown on the HUD strip, e.g. 'iso_test'
    step        TEXT NOT NULL DEFAULT '',      -- current step label, e.g. 'wall-occlusion probe'
    state       TEXT NOT NULL DEFAULT 'idle'
                CHECK (state IN ('idle', 'running', 'pass', 'fail')),
    started_at  TEXT,                          -- when the run started (elapsed is measured from here)
    finished_at TEXT,                          -- when it reached pass/fail/off (NULL while running)
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX test_runs_state ON test_runs (state, updated_at DESC);
