"""Server-side in-game test-run records — the mirror of the iso-spectate TEST STATUS HUD strip.

The strip is driven by cs_script console commands sent over netcon (`iso_test start|step|pass|fail|off`)
and shows four fields: name, step, state, elapsed. This service keeps a durable record with exactly
those fields, so a worker can mirror each transition with one call:

    test_start(name, host?, project?)   -> state=running, started_at=now   (claims the host lease)
    test_step(run_id, text)             -> step=<text>                     (running only)
    test_finish(run_id, 'pass'|'fail')  -> state=pass/fail, finished_at=now (releases the lease)
    test_clear(run_id)                  -> state=idle                      (the HUD's `iso_test off`)

Pushing the update to the actual HUD over netcon stays the caller's job; these records are the
server's source of truth (and reconnect/resume). `elapsed_sec` is computed from `started_at`.
"""

from __future__ import annotations

import contextlib
import secrets
import sqlite3

from s2docs import evidence, hosts
from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

STATES: tuple[str, ...] = ("idle", "running", "pass", "fail")
TERMINAL: tuple[str, ...] = ("pass", "fail")

# elapsed is derived, never stored: seconds from started_at to finished_at (or to now while running).
_SELECT = """
    SELECT *, CASE WHEN started_at IS NULL THEN 0
                   ELSE CAST(strftime('%s', COALESCE(finished_at, 'now')) AS INTEGER)
                        - CAST(strftime('%s', started_at) AS INTEGER)
              END AS elapsed_sec
    FROM test_runs
"""


class TestRunError(ValueError):
    pass


def _row(row: sqlite3.Row) -> m.TestRun:
    return m.TestRun(
        id=row["id"],
        host_id=row["host_id"],
        project_id=row["project_id"],
        name=row["name"],
        step=row["step"],
        state=row["state"],
        elapsed_sec=row["elapsed_sec"] or 0,
        note=row["note"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get(db: Database, run_id: str) -> m.TestRun:
    with db.reader() as conn:
        row = conn.execute(_SELECT + " WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"no test run {run_id!r}")
    return _row(row)


def current(db: Database) -> m.TestRun | None:
    """The run the HUD would be showing: the most recently updated non-idle run, or None."""
    with db.reader() as conn:
        row = conn.execute(
            _SELECT + " WHERE state != 'idle' ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
    return _row(row) if row else None


def list_runs(db: Database, *, state: str | None = None, host_id: str | None = None) -> list[m.TestRun]:
    if state is not None and state not in STATES:
        raise TestRunError(f"state must be one of {STATES}")
    with db.reader() as conn:
        rows = conn.execute(
            _SELECT
            + " WHERE (? IS NULL OR state = ?) AND (? IS NULL OR host_id = ?)"
            + " ORDER BY updated_at DESC, created_at DESC",
            (state, state, host_id, host_id),
        ).fetchall()
    return [_row(r) for r in rows]


def start(db: Database, name: str, *, host_id: str | None = None, project_id: str | None = None) -> m.TestRun:
    """Open a test session (state=running). Claims the host's lease server-side when a host is given."""
    if not name or not name.strip():
        raise TestRunError("name is required (the label shown on the HUD strip)")
    if host_id is not None:
        hosts.get_host(db, host_id)  # raises HostError if unknown
    rid = "t-" + secrets.token_hex(4)
    owner = f"test:{rid}"
    if host_id is not None:
        hosts.claim_host(db, host_id, owner)  # raises HostLocked on contention, before any record exists
    try:
        with db.writer() as conn, conn:
            conn.execute(
                "INSERT INTO test_runs (id, host_id, project_id, name, state, started_at) "
                "VALUES (?, ?, ?, ?, 'running', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (rid, host_id, project_id, name),
            )
    except sqlite3.IntegrityError as exc:
        _release(db, host_id, owner)
        raise TestRunError(f"unknown project {project_id!r}: {exc}") from exc
    return get(db, rid)


def step(db: Database, run_id: str, text: str) -> m.TestRun:
    """Set the current step label. Only a running session has a current step."""
    run = get(db, run_id)
    if run.state != "running":
        raise TestRunError(f"test run {run_id} is {run.state}, not running; can't set a step")
    if not text or not text.strip():
        raise TestRunError("step text is required")
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET step = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (text, run_id),
        )
    return get(db, run_id)


def finish(db: Database, run_id: str, result: str, *, note: str | None = None) -> m.TestRun:
    """Mark a running session pass/fail, release its host lease, and record a console-transcript
    evidence row on the linked project (when there is one) so the outcome is durable proof."""
    if result not in TERMINAL:
        raise TestRunError(f"result must be one of {TERMINAL}, not {result!r}")
    run = get(db, run_id)
    if run.state != "running":
        raise TestRunError(f"test run {run_id} is {run.state}; only a running run can finish")
    merged = "\n".join(x for x in (run.note, note) if x) or None
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET state = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "note = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (result, merged, run_id),
        )
    _release(db, run.host_id, f"test:{run_id}")
    finished = get(db, run_id)
    if run.project_id:
        evidence.add(
            db,
            kind="console_transcript",
            project_id=run.project_id,
            data={
                "test_run": run_id,
                "name": run.name,
                "step": run.step,
                "state": result,
                "elapsed_sec": finished.elapsed_sec,
                "note": merged,
            },
        )
    return finished


def clear(db: Database, run_id: str) -> m.TestRun:
    """Dismiss a run (`iso_test off`): state=idle, releasing any host lease still held."""
    run = get(db, run_id)
    if run.state == "idle":
        raise TestRunError(f"test run {run_id} is already idle")
    if run.state == "running":
        _release(db, run.host_id, f"test:{run_id}")
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET state = 'idle', "
            "finished_at = COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (run_id,),
        )
    return get(db, run_id)


def _release(db: Database, host_id: str | None, owner: str) -> None:
    if host_id is None:
        return
    with contextlib.suppress(hosts.HostError):
        hosts.release_host(db, host_id, owner, force=True)
