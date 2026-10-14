"""Orphan reconciliation, the test-run reaper, lease renewal, DB identity and the log-path guard.

The review's high findings: runs that lose their owner stay 'running' forever, a test session longer
than its TTL can have its host taken over mid-test, and a client could point build_start's log at any
path on the server. No real host/network is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import builds, hosts, test_runs
from s2docs.api.app import create_app
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch, tmp_path):
    """get_settings is lru_cached and data_dir/reap thresholds come from env — isolate each test."""
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def rdb(tmp_path) -> Database:
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@100.64.130.72", shell="powershell")
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    return db


class _DeadProc:
    """A subprocess stand-in that never reports finishing (the watcher's process is 'gone')."""

    def __init__(self, argv, stdout=None, stderr=None, **_kw):
        self.pid = 777
        self.terminated = False
        if stdout is not None:
            stdout.write(b"")
            stdout.flush()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminate()


# ------------------------------------------------------- 6a orphan build reconcile
def _insert_build(db: Database, rid: str, host_id: str, status: str) -> None:
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner) VALUES (?, ?, 'build', 'x', ?, ?)",
            (rid, host_id, status, f"build:{rid}"),
        )


def test_reconcile_interrupts_orphaned_build_runs_and_releases_their_lease(rdb, monkeypatch):
    """A run left 'running'/'queued' with no live watcher in this process is finalized 'interrupted'."""
    monkeypatch.setattr(builds.subprocess, "Popen", _DeadProc)
    # A live run in *this* process (its watcher thread is registered) must survive the pass.
    live = builds.start(rdb, "desktop-pc", "compile", kind="compile")
    # Orphans written directly: one still owns its lease, one whose lease is gone.
    _insert_build(rdb, "b-orphan", "mini", "running")
    hosts.claim_host(rdb, "mini", "build:b-orphan", phase="compile")
    _insert_build(rdb, "b-queued", "desktop-pc", "queued")
    # b-queued never acquired a lease (its owner does not hold desktop-pc; the live run does)

    interrupted = builds.reconcile_orphans(rdb)
    assert set(interrupted) >= {"b-orphan", "b-queued"}
    assert builds.get_run(rdb, "b-orphan").status == "interrupted"
    assert builds.get_run(rdb, "b-queued").status == "interrupted"
    assert "interrupted" in (builds.get_run(rdb, "b-orphan").notes or "")
    # the orphan's own lease is released...
    assert hosts.get_host(rdb, "mini").lock is None
    # ...but the live run is untouched and still holds its host
    assert builds.get_run(rdb, live.id).status == "running"
    assert hosts.get_host(rdb, "desktop-pc").lock.owner == f"build:{live.id}"
    builds.cancel(rdb, live.id)
    builds.wait(rdb, live.id, timeout_sec=5, poll_sec=0.02)


def test_reconcile_does_not_release_a_lease_taken_over_by_someone_else(rdb):
    _insert_build(rdb, "b-orphan2", "desktop-pc", "running")
    hosts.claim_host(rdb, "desktop-pc", "build:b-orphan2")
    hosts.claim_host(rdb, "desktop-pc", "ws/new-owner", force=True)
    builds.reconcile_orphans(rdb)
    assert builds.get_run(rdb, "b-orphan2").status == "interrupted"
    assert hosts.get_host(rdb, "desktop-pc").lock.owner == "ws/new-owner"


# ------------------------------------------------------- 6b test-run reaper
def test_reaper_moves_test_runs_with_a_lost_lease_to_idle(rdb):
    taken = test_runs.start(rdb, "iso_taken", host_id="desktop-pc")
    hosts.claim_host(rdb, "desktop-pc", "ws/new-owner", force=True)  # the lease is stolen
    gone = test_runs.start(rdb, "iso_gone", host_id="mini")
    hosts.release_host(rdb, "mini", f"test:{gone.id}", force=True)  # lease released underneath it
    plain = test_runs.start(rdb, "iso_plain")  # host-less

    reaped = test_runs.reap_stale(rdb, stale_sec=3600)
    assert set(reaped) == {taken.id, gone.id}
    assert test_runs.get(rdb, taken.id).state == "idle"
    assert test_runs.get(rdb, gone.id).state == "idle"
    assert test_runs.get(rdb, taken.id).note == "reaped: lease lost"
    # the fresh host-less run is not reaped
    assert test_runs.get(rdb, plain.id).state == "running"


def test_reaper_reaps_host_less_runs_that_went_stale(rdb):
    run = test_runs.start(rdb, "iso_stale")
    with rdb.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7200 seconds') WHERE id = ?",
            (run.id,),
        )
    assert test_runs.reap_stale(rdb, stale_sec=3600) == [run.id]
    assert test_runs.get(rdb, run.id).state == "idle"
    assert test_runs.get(rdb, run.id).note == "reaped: lease lost"


def test_reaper_keeps_a_healthy_running_run(rdb):
    run = test_runs.start(rdb, "iso_ok", host_id="desktop-pc")
    assert test_runs.reap_stale(rdb, stale_sec=3600) == []
    assert test_runs.get(rdb, run.id).state == "running"
    assert hosts.get_host(rdb, "desktop-pc").lock.owner == f"test:{run.id}"


# ------------------------------------------- 6c test-run lease renewal on step
def test_test_step_renews_the_run_lease(rdb):
    run = test_runs.start(rdb, "iso_long", host_id="desktop-pc")
    with rdb.writer() as conn, conn:  # age the lease well past its TTL
        conn.execute("UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-4000 seconds')")
    assert not hosts.get_host(rdb, "desktop-pc").lock.fresh
    test_runs.step(rdb, run.id, "long in-game probe")
    lock = hosts.get_host(rdb, "desktop-pc").lock
    assert lock.fresh and lock.owner == f"test:{run.id}"  # renewed by the step call


# ------------------------------------------- 6 lifespan reaper wiring
def test_lifespan_starts_and_stops_the_reaper(rdb):
    """create_app wires a background reaper into the lifespan; starting and stopping it must not raise."""
    with TestClient(create_app(rdb.path)) as c:
        assert c.get("/api/health").json()["status"] == "ok"


def test_reaper_does_not_resurrect_a_missing_database(tmp_path):
    """A health probe against a missing db must still 503 — startup work must not create the file."""
    with TestClient(create_app(tmp_path / "missing.db", mount_mcp=False)) as c:
        assert c.get("/api/games").status_code == 503


# ------------------------------------------------------- 5 DB identity
def test_health_reports_db_identity(rdb):
    with TestClient(create_app(rdb.path)) as c:
        body = c.get("/api/health").json()
    settings = get_settings()
    assert body["status"] == "ok"
    assert Path(body["db_path"]) == rdb.path.resolve()
    assert Path(body["data_dir"]) == settings.data_dir.resolve()
    assert Path(body["db_path"]).is_absolute() and Path(body["data_dir"]).is_absolute()
    assert body["pid"] == os.getpid()
    assert body["version"] and body["started_at"]


def test_mcp_server_info_matches_health(rdb):
    server = create_mcp_server(rdb)

    async def main():
        async with Client(server) as c:
            return await c.call_tool("server_info", {})

    result = anyio.run(main)
    info = result.structured_content
    assert Path(info["db_path"]) == rdb.path.resolve()
    assert Path(info["data_dir"]) == get_settings().data_dir.resolve()
    assert info["pid"] == os.getpid()
    # server_info is a read-only tool
    tools = anyio.run(_list_tools, server)
    assert all(t.annotations.read_only_hint for t in tools if t.name == "server_info")


async def _list_tools(server):
    async with Client(server) as c:
        return (await c.list_tools()).tools



def test_cli_warns_loudly_when_data_dir_is_unset(monkeypatch, capsys):
    from s2docs import cli

    monkeypatch.delenv("S2DOCS_DATA_DIR", raising=False)
    get_settings.cache_clear()
    cli._warn_data_dir()
    err = capsys.readouterr().err
    assert "S2DOCS_DATA_DIR" in err and "WARNING" in err.upper()
    # when it is set, no warning
    monkeypatch.setenv("S2DOCS_DATA_DIR", "/tmp/somewhere")
    get_settings.cache_clear()
    cli._warn_data_dir()
    assert "S2DOCS_DATA_DIR" not in capsys.readouterr().err


# ------------------------------------------- 4 build log_path confinement
def test_build_start_rejects_a_log_path_outside_the_data_dir(rdb):
    for evil in ("/tmp/evil.log", "../../etc/passwd.log"):
        with pytest.raises(builds.BuildError, match="data dir|outside|log path"):
            builds.start(rdb, "desktop-pc", "compile", log_path=evil)
    assert builds.list_runs(rdb) == []  # nothing was recorded


def test_build_start_accepts_a_log_path_inside_the_data_dir(rdb, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", _DeadProc)
    inside = get_settings().data_dir / "logs" / "custom.log"
    inside.parent.mkdir(parents=True, exist_ok=True)
    run = builds.start(rdb, "desktop-pc", "compile", log_path=inside)
    assert Path(run.log_path) == inside.resolve()
    builds.cancel(rdb, run.id)
    builds.wait(rdb, run.id, timeout_sec=5, poll_sec=0.02)


# ------------------------------------------- 8 lock summary on list responses
def test_build_and_test_list_carry_the_host_lock_summary(rdb, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", _DeadProc)
    build = builds.start(rdb, "desktop-pc", "compile", kind="compile")
    # make the build's phase old so the server marks it STUCK
    with rdb.writer() as conn, conn:
        conn.execute(
            "UPDATE host_locks SET phase_since = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-4000 seconds')"
        )
    listed = {r.id: r for r in builds.list_runs(rdb)}
    assert listed[build.id].lock is not None
    assert listed[build.id].lock.owner == f"build:{build.id}"
    assert listed[build.id].lock.stuck
    builds.cancel(rdb, build.id)
    builds.wait(rdb, build.id, timeout_sec=5, poll_sec=0.02)

    test = test_runs.start(rdb, "iso_lock", host_id="mini")
    runs = {r.id: r for r in test_runs.list_runs(rdb)}
    assert runs[test.id].lock is not None and runs[test.id].lock.owner == f"test:{test.id}"
    assert runs[test.id].lock.phase == "test"
    # a run whose host lease belongs to someone else carries no lock summary
    hosts.claim_host(rdb, "mini", "ws/other", force=True)
    runs = {r.id: r for r in test_runs.list_runs(rdb)}
    assert runs[test.id].lock is None


def test_activity_shows_all_running_test_runs_marker(rdb):
    with TestClient(create_app(rdb.path)) as c:
        js = c.get("/app.js").text
    # the NOW section iterates every running test run, not just the single /current probe
    assert "tests.filter" in js and "state === \"running\"" in js
    assert 'test-runs/current' not in js or "tests.filter" in js
