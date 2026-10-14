# Handoff — human-confirm evidence (ws/human-confirm-evidence)

## What changed
- `EvidenceKind` gained `"human-confirm"` (models.py).
- `Evidence.project_id` is now nullable; `Evidence.test_run_id` added (models.py).
- `POST /api/evidence` accepts an optional `test_run_id` (top-level, or read from `data.test_run_id`)
  and no longer requires `project_id` when a run is given: the store resolves the project from the run,
  and leaves `project_id` NULL when the run itself has no project.
- Migration `0011_human_confirm_evidence.sql` rebuilds the `evidence` table (SQLite can't widen a CHECK
  or drop NOT NULL in place): new `kind` CHECK includes `human-confirm`, `project_id` nullable,
  `test_run_id` column with an FK to `test_runs(id) ON DELETE SET NULL`, plus an index on it. Existing
  rows are copied verbatim.

## Why
The director POSTs `kind='human-confirm'` with `data {test_run_id, verdict, notes, evidence_refs,
purpose, expected, map, source}` and got 422 (kind not allowed; `project_id` required + FK-checked). The
live run `t-5e1fb5cc` (`director-de_dust2_iso`) has `project_id = NULL`, so a human-confirm row must be
storable with a NULL project and a `test_run_id` anchor.

## Tests
- `tests/test_evidence_testruns.py`: added human-confirm API tests (no-project run -> project_id null;
  project-bearing run -> project resolved; missing run / no project -> 400).
- Full suite: `220 passed`.

## Restart
The :8812 server was restarted to load the new code (in-memory code was stale).
Old listener pid 84430 → stopped; new pid 50232, started 2026-10-14T07:09:25Z, `/api/health` = ok.
Command line unchanged: `.venv/bin/python3 .venv/bin/s2docs serve --host 0.0.0.0 --port 8812`
with `S2DOCS_DATA_DIR=<checkout>/data`.

## End-to-end proof (live)
`POST /api/evidence {kind: human-confirm, test_run_id: t-5e1fb5cc, data: {...}}` → HTTP 201, row
`e-cf37b275` with `project_id=null`, `test_run_id=t-5e1fb5cc`, payload intact. `GET
/api/evidence?kind=human-confirm` returns the same row. Verified durable on disk.

## Note
Not merged to `main` (per task). No other tree changes; the sibling project iso-spectate-view is a
separate checkout.
