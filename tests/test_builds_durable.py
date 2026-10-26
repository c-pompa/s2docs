"""Durable build runs: a run must outlive the process that started it, and stop leaving stale rows.

The failure this covers: `start` kept the Popen in a module-level dict, so the run lived inside
whichever process launched it — the CLI, the MCP server, or `s2docs serve`. Restarting that process
either killed a 38-minute map compile or orphaned it, and its row stayed 'running' forever, which also
refused the next build on that host ("already has a running build run"). The fix is a detached child
plus a persisted pid, and a `recover` that asks the OS instead of reading this process's memory.

No real host is touched: ssh/subprocess is faked, exactly as tests/test_builds_deploys.py fakes it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from s2docs import builds, cli, hosts
from s2docs.api.app import create_app
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def ddb(tmp_path) -> Database:
    db = Database(tmp_path / "durable.db")
    hosts.register_host(db, "build-pc", "pompa@192.0.2.172", shell="powershell")
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    return db


def _recording_popen(launched: list, pid: int = 4_242):
    """A Popen stand-in that records its launch kwargs and never finishes (the run stays 'running')."""

    class _P:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.pid = pid
            self.kwargs = {k: v for k, v in kwargs.items() if k not in ("stdout", "stderr")}
            self.terminated = False
            launched.append(self)

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminate()

    return _P


def _dead_pid() -> int:
    """A pid that is certainly not running: a child of this test, already waited for."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not builds.pid_alive(proc.pid)
    return proc.pid


def _insert_run(
    db: Database,
    rid: str,
    *,
    host_id: str = "mini",
    status: str = "running",
    pid: int | None = None,
    handle: str | None = None,
    log_path: str | None = None,
) -> None:
    """Write a run row directly — the state a previous incarnation of the server left in the table."""
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, handle, pid, log_path) "
            "VALUES (?, ?, 'build', 'compile', ?, ?, ?, ?, ?)",
            (rid, host_id, status, f"build:{rid}", handle, pid, log_path),
        )


def _row(db: Database, rid: str) -> dict:
    with db.reader() as conn:
        return dict(conn.execute("SELECT * FROM build_runs WHERE id = ?", (rid,)).fetchone())


# ------------------------------------------------------------- 1 detachment
def test_start_launches_the_child_in_its_own_session(ddb, monkeypatch):
    """A restart of the serving process must not decide the build's fate: the child gets its own session."""
    launched: list = []
    monkeypatch.setattr(builds.subprocess, "Popen", _recording_popen(launched))
    run = builds.start(ddb, "build-pc", "resourcecompiler -game csgo -nop4 addon")
    try:
        assert run.status == "running"
        assert launched[0].kwargs["start_new_session"] is True
        # and it must not inherit the server's stdin, or a detached ssh keeps reading our terminal
        assert launched[0].kwargs["stdin"] is subprocess.DEVNULL
    finally:
        builds.cancel(ddb, run.id)
        builds.wait(ddb, run.id, timeout_sec=5, poll_sec=0.02)


def test_start_persists_the_pid_and_the_log_path_for_a_later_process(ddb, monkeypatch):
    """`recover` in another process can only find the run if its pid and log were written down."""
    launched: list = []
    monkeypatch.setattr(builds.subprocess, "Popen", _recording_popen(launched, pid=os.getpid()))
    run = builds.start(ddb, "build-pc", "compile", kind="compile")
    try:
        row = _row(ddb, run.id)
        assert row["pid"] == os.getpid()
        assert row["log_path"] == run.log_path and row["log_path"]
        # handle stays the human-facing field, as before
        assert row["handle"] == str(os.getpid())
    finally:
        builds.cancel(ddb, run.id)
        builds.wait(ddb, run.id, timeout_sec=5, poll_sec=0.02)


def test_detach_flags_ask_for_a_new_process_group_on_windows(monkeypatch):
    """Windows has no sessions: the child gets its own process group instead (same guarantee)."""
    monkeypatch.setattr(builds.os, "name", "nt")
    assert builds._detach_kwargs() == {"creationflags": builds.WINDOWS_NEW_PROCESS_GROUP}


# ------------------------------------------------------------- 2/3 recover
def test_recover_interrupts_a_run_whose_owning_process_disappeared(ddb):
    dead = _dead_pid()
    log = ddb.path.parent / "b-dead.log"
    log.write_text("Compile Summary: 12 compiled, 1 failed\n", encoding="utf-8")
    hosts.claim_host(ddb, "mini", "build:b-dead", phase="compile")
    _insert_run(ddb, "b-dead", pid=dead, log_path=str(log))

    assert builds.recover(ddb) == ["b-dead"]

    run = builds.get_run(ddb, "b-dead")
    assert run.status == "interrupted"
    notes = run.notes or ""
    assert f"pid {dead}" in notes and "disappeared" in notes
    # the log survives the interruption: the tail of a dead compile is still readable
    assert run.log_path == str(log) and log.exists()
    assert "1 failed" in builds.log_tail(ddb, "b-dead").lines[-1]
    # and the host is usable again instead of being stuck behind a ghost run
    assert hosts.get_host(ddb, "mini").lock is None


def test_recover_leaves_a_run_whose_process_is_still_alive_alone(ddb):
    """This test process's own pid stands in for a live compile: it must never be interrupted."""
    pid = os.getpid()
    hosts.claim_host(ddb, "mini", "build:b-live")
    _insert_run(ddb, "b-live", pid=pid)

    assert builds.recover(ddb) == []

    assert builds.get_run(ddb, "b-live").status == "running"
    assert hosts.get_host(ddb, "mini").lock.owner == "build:b-live"


def test_recover_falls_back_to_a_pid_recorded_only_in_handle(ddb):
    """Rows written before the pid column existed kept the pid in `handle`; they are recoverable too."""
    pid = os.getpid()
    _insert_run(ddb, "b-legacy-live", pid=None, handle=str(pid))
    _insert_run(ddb, "b-legacy-dead", pid=None, handle=str(_dead_pid()))

    assert builds.recover(ddb) == ["b-legacy-dead"]
    assert builds.get_run(ddb, "b-legacy-live").status == "running"
    assert builds.get_run(ddb, "b-legacy-dead").status == "interrupted"


def test_recover_is_idempotent(ddb):
    _insert_run(ddb, "b-twice", pid=_dead_pid())

    assert builds.recover(ddb) == ["b-twice"]
    first = builds.get_run(ddb, "b-twice").notes
    assert builds.recover(ddb) == []
    run = builds.get_run(ddb, "b-twice")
    assert run.status == "interrupted" and run.notes == first  # no second note appended


def test_recover_does_not_release_a_lease_taken_over_by_someone_else(ddb):
    _insert_run(ddb, "b-taken", pid=_dead_pid())
    hosts.claim_host(ddb, "mini", "build:b-taken")
    hosts.claim_host(ddb, "mini", "ws/new-owner", force=True)

    builds.recover(ddb)

    assert builds.get_run(ddb, "b-taken").status == "interrupted"
    assert hosts.get_host(ddb, "mini").lock.owner == "ws/new-owner"


# ------------------------------------------------------------- 4 startup + CLI
def test_serve_startup_recovers_a_run_left_running_by_a_dead_process(ddb):
    _insert_run(ddb, "b-serve", pid=_dead_pid())

    with TestClient(create_app(ddb.path)) as c:
        assert c.get("/api/health").json()["status"] == "ok"

    assert builds.get_run(ddb, "b-serve").status == "interrupted"


def test_mcp_startup_recovers_a_run_left_running_by_a_dead_process(ddb):
    _insert_run(ddb, "b-mcp", pid=_dead_pid())

    create_mcp_server(ddb)

    assert builds.get_run(ddb, "b-mcp").status == "interrupted"


def test_cli_build_recover_prints_what_it_changed(capsys):
    db = Database(get_settings().db_path)
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    _insert_run(db, "b-cli", pid=_dead_pid())

    assert cli.main(["build", "recover"]) == 0
    printed = capsys.readouterr().out
    assert "interrupted 1 build run(s)" in printed and "b-cli" in printed
    assert builds.get_run(db, "b-cli").status == "interrupted"

    # a second pass reports nothing and still exits 0 (safe to run from cron or a wrapper script)
    assert cli.main(["build", "recover"]) == 0
    assert "nothing to recover" in capsys.readouterr().out


def test_cli_build_recover_json_contract(capsys):
    db = Database(get_settings().db_path)
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    _insert_run(db, "b-cli-json", pid=_dead_pid())

    assert cli.main(["--json", "build", "recover"]) == 0
    payload = capsys.readouterr().out.strip()
    assert '"schema": "s2docs/v1"' in payload and '"interrupted": ["b-cli-json"]' in payload
