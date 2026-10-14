-- 0011: human-confirm evidence that is durable independent of a project step.
--
-- The human-confirm gate POSTs kind='human-confirm' with the verdict/notes/evidence_refs in `data` and a
-- `test_run_id`, but NO project_id (the director does not have one). Two things blocked that:
--   * `kind`'s CHECK omitted 'human-confirm', and
--   * `project_id` was NOT NULL, so a row could not be stored when the run had no project.
-- A confirm can legitimately belong to a test run that itself has no project (e.g. t-5e1fb5cc, a
-- director-driven iso run), so project_id becomes nullable and a `test_run_id` link is added. When the
-- run *does* have a project the store still fills project_id, keeping the existing project/step filtering
-- and the evidence gate working unchanged.
--
-- SQLite cannot drop a NOT NULL or widen a CHECK constraint in place, so rebuild the table (its data is
-- tiny and it is a leaf: no other table references evidence, so no foreign_keys juggling is needed).

CREATE TABLE evidence_new (
    id          TEXT PRIMARY KEY,               -- evidence id, e.g. 'e-1a2b3c4d'
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,   -- NULL when only a test run anchors it
    step_id     TEXT,                           -- NULL = project-level evidence (not tied to a step)
    kind        TEXT NOT NULL
                CHECK (kind IN ('console_transcript', 'file_hash', 'pixel_diff', 'screenshot', 'note',
                                'other', 'human-confirm')),
    ref         TEXT,                           -- path / url / command the evidence points at
    data        TEXT NOT NULL DEFAULT '{}',     -- JSON detail: verdict/notes/evidence_refs, md5s, pixel diff, ...
    test_run_id TEXT REFERENCES test_runs(id) ON DELETE SET NULL,  -- the run this row records
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- Cascades with its step; only enforced when step_id is set (SQLite MATCH SIMPLE ignores a
    -- composite FK with any NULL part), so project-level rows are still valid.
    FOREIGN KEY (project_id, step_id) REFERENCES project_steps (project_id, step_id) ON DELETE CASCADE
);
INSERT INTO evidence_new (id, project_id, step_id, kind, ref, data, created_at)
    SELECT id, project_id, step_id, kind, ref, data, created_at FROM evidence;
DROP TABLE evidence;
ALTER TABLE evidence_new RENAME TO evidence;
CREATE INDEX evidence_step ON evidence (project_id, step_id, created_at DESC);
CREATE INDEX evidence_project ON evidence (project_id, created_at DESC);
CREATE INDEX evidence_test_run ON evidence (test_run_id, created_at DESC);
