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
import os
import secrets
import sqlite3

from s2docs import evidence, hosts, weburls
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
        web_url=weburls.test_run_url(weburls.public_base_url(), row["id"]),
    )


def _enrich_lock(conn: sqlite3.Connection, run: m.TestRun) -> m.TestRun:
    """Attach the host lock summary when this run still holds it (owner 'test:<id>'), else None."""
    if run.host_id:
        run.lock = hosts.lock_summary(conn, run.host_id, f"test:{run.id}")
    return run


def get(db: Database, run_id: str) -> m.TestRun:
    with db.reader() as conn:
        row = conn.execute(_SELECT + " WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"no test run {run_id!r}")
        return _enrich_lock(conn, _row(row))


def current(db: Database) -> m.TestRun | None:
    """The run the HUD would be showing: the most recently updated non-idle run, or None."""
    with db.reader() as conn:
        row = conn.execute(
            _SELECT + " WHERE state != 'idle' ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
        return _enrich_lock(conn, _row(row)) if row else None


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
        return [_enrich_lock(conn, _row(r)) for r in rows]


def start(db: Database, name: str, *, host_id: str | None = None, project_id: str | None = None) -> m.TestRun:
    """Open a test session (state=running). Claims the host's lease server-side when a host is given."""
    if not name or not name.strip():
        raise TestRunError("name is required (the label shown on the HUD strip)")
    if host_id is not None:
        hosts.get_host(db, host_id)  # raises HostError if unknown
    rid = "t-" + secrets.token_hex(4)
    owner = f"test:{rid}"
    if host_id is not None:
        # The lease's phase is the session's own work: 'test'. It is cleared when the lease is
        # released on finish/clear, and the server uses it (with the threshold) to flag a stuck run.
        hosts.claim_host(db, host_id, owner, phase="test")  # raises HostLocked before any record exists
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
    # Renew the run's lease: a long in-game session must not have its host taken over by a TTL expiry.
    # A lost lease (taken over) is not fatal to a step label — the reaper will finalize the run.
    if run.host_id is not None:
        with contextlib.suppress(hosts.HostError):
            hosts.heartbeat_host(db, run.host_id, f"test:{run_id}", phase="test")
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


def reap_stale(db: Database, *, stale_sec: int | None = None, note: str = "reaped: lease lost") -> list[str]:
    """Finalize test runs whose host lease is gone (or whose host-less record has gone cold).

    A run still 'running' whose host is held by someone else (or not held at all) can never finish
    on its own — the owner is gone. A host-less run has no lease to check, so it is reaped once it
    has not been touched for `stale_sec` (S2DOCS_TEST_RUN_STALE_SEC, default 3600 s). Returns the
    reaped run ids. This is the periodic reaper's function; it is also callable directly.
    """
    if stale_sec is None:
        try:
            stale_sec = int(os.environ.get("S2DOCS_TEST_RUN_STALE_SEC") or 3600)
        except ValueError:
            stale_sec = 3600
    reaped: list[str] = []
    with db.writer() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, host_id, "
                "CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', updated_at) AS INTEGER) AS age "
                "FROM test_runs WHERE state = 'running'"
            ).fetchall()
            for r in rows:
                rid = r["id"]
                if r["host_id"] is None:
                    lost = r["age"] > stale_sec
                else:
                    lock = conn.execute(
                        "SELECT owner FROM host_locks WHERE host_id = ?", (r["host_id"],)
                    ).fetchone()
                    lost = lock is None or lock["owner"] != f"test:{rid}"
                if lost:
                    conn.execute(
                        "UPDATE test_runs SET state = 'idle', note = ?, "
                        "finished_at = COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                        (note, rid),
                    )
                    reaped.append(rid)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return reaped


def _release(db: Database, host_id: str | None, owner: str) -> None:
    """Free the lease iff it is still ours. Never deletes another owner's lock (mirrors builds._release):
    finishing/clearing an orphaned run must not delete whoever holds the host now."""
    if host_id is None:
        return
    with contextlib.suppress(hosts.HostError):
        lock = hosts.get_host(db, host_id).lock
        if lock is not None and lock.owner == owner:
            hosts.release_host(db, host_id, owner)
