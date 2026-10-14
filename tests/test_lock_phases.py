"""Lock phases + server-computed stuck-ness (the 2026-10-13 'fresh but hung' incident).

A build worker heartbeat its lock faithfully while its compile sat hung for 95 minutes, so the lock
read 'fresh' and the dashboard looked healthy. The fix is structural: locks carry a *phase* (what the
owner is doing) and the SERVER decides stuck-ness from the phase age — clients cannot fake it.

No real host/network is touched: the ssh layer (subprocess) is faked, exactly as in test_hosts.py.
"""

from __future__ import annotations

import time

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
def _fresh_settings():
    """get_settings is lru_cached and stuck thresholds come from env — never leak one test into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def hdb(tmp_path) -> Database:
    db = Database(tmp_path / "phases.db")
    hosts.register_host(db, "desktop-pc", "pompa@100.64.130.72", hostname="DESKTOP-39NF657", shell="powershell")
    return db


def _age(hdb: Database, *, heartbeat_sec: int | None = None, phase_sec: int | None = None) -> None:
    """Push the lock's heartbeat and/or phase_since into the past (seconds ago)."""
    with hdb.writer() as conn, conn:
        if heartbeat_sec is not None:
            conn.execute(
                "UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                (f"-{heartbeat_sec} seconds",),
            )
        if phase_sec is not None:
            conn.execute(
                "UPDATE host_locks SET phase_since = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
                (f"-{phase_sec} seconds",),
            )


# ------------------------------------------------------------------ migration
def test_migration_adds_phase_columns(hdb):
    with hdb.writer() as conn:
        lock_cols = {r["name"] for r in conn.execute("PRAGMA table_info(host_locks)")}
        assert {"phase", "phase_since", "note"} <= lock_cols
        event_cols = {r["name"] for r in conn.execute("PRAGMA table_info(host_lock_events)")}
        assert "phase" in event_cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 8


def test_migration_upgrades_a_v7_db_without_losing_rows(tmp_path):
    """An existing install (schema v7, live locks + lock history) upgrades in place, additive and lossless."""
    from s2docs.db import connection

    conn = connection.connect(tmp_path / "old.db")
    for version, sql in connection._migrations():
        if version > 7:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO hosts (id, ssh_target) VALUES ('desktop-pc', 'pompa@x')")
    conn.execute("INSERT INTO host_locks (host_id, owner) VALUES ('desktop-pc', 'ws/a')")
    conn.execute(
        "INSERT INTO host_lock_events (host_id, owner, event, detail) VALUES ('desktop-pc', 'ws/a', 'claimed', 'old')"
    )
    conn.commit()

    assert connection.migrate(conn) >= 8
    lock = conn.execute("SELECT owner, phase, phase_since, note FROM host_locks").fetchone()
    assert lock["owner"] == "ws/a" and lock["phase"] == "" and lock["phase_since"] is None
    events = conn.execute("SELECT owner, event, phase, detail FROM host_lock_events").fetchall()
    assert len(events) == 1 and events[0]["event"] == "claimed" and events[0]["phase"] is None
    conn.close()


def test_phase_event_shape_is_accepted(hdb):
    # A 'phase' row must pass the rebuilt CHECK constraint.
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    hosts.heartbeat_host(hdb, "desktop-pc", "ws/a", phase="link")
    events = hosts.lock_history(hdb, "desktop-pc")
    phase_events = [e for e in events if e.event == "phase"]
    assert phase_events and phase_events[0].phase == "link"
    assert "compile" in (phase_events[0].detail or "")


# ------------------------------------------------------------ phase transitions
def test_claim_sets_phase_and_same_phase_heartbeat_keeps_since(hdb):
    lock = hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    assert lock.phase == "compile" and lock.phase_since is not None
    _age(hdb, phase_sec=5000)
    # same phase on a refresh: phase_since is kept (a faithful heartbeat must not hide a stuck phase)
    kept = hosts.heartbeat_host(hdb, "desktop-pc", "ws/a", phase="compile")
    assert kept.phase == "compile" and kept.phase_age_sec > 4000


def test_phase_change_resets_phase_since(hdb):
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=5000)
    changed = hosts.heartbeat_host(hdb, "desktop-pc", "ws/a", phase="link")
    assert changed.phase == "link" and changed.phase_age_sec < 60
    # a claim that changes the phase resets it too
    _age(hdb, phase_sec=5000)
    reclaimed = hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="finalize")
    assert reclaimed.phase == "finalize" and reclaimed.phase_age_sec < 60


def test_heartbeat_without_phase_keeps_reported_phase(hdb):
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=5000)
    same = hosts.heartbeat_host(hdb, "desktop-pc", "ws/a")  # old client: no phase argument
    assert same.phase == "compile" and same.phase_age_sec > 4000


# ------------------------------------------------------------------- stuck matrix
def test_stuck_when_fresh_but_phase_is_old(hdb):
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=2000)  # fresh heartbeat, stale phase (default threshold 1800)
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert lock.fresh and lock.stuck
    assert "compile" in (lock.stuck_reason or "") and "1800" in (lock.stuck_reason or "")


def test_not_stuck_when_phase_is_recent(hdb):
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=100)
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert lock.fresh and not lock.stuck and lock.stuck_reason is None


def test_not_stuck_when_heartbeat_is_stale(hdb):
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile", ttl_sec=1800)
    _age(hdb, heartbeat_sec=4000, phase_sec=4000)
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert not lock.fresh and not lock.stuck  # a stale lock is already reported as stale


def test_no_phase_is_never_stuck(hdb):
    # Fail-soft for old clients: a lock that never reports a phase can't be judged stuck.
    hosts.claim_host(hdb, "desktop-pc", "ws/legacy")
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert lock.phase == "" and lock.phase_since is None and not lock.stuck
    # even a contrived old phase_since on an empty-phase lock is not stuck
    _age(hdb, phase_sec=100000)
    assert hosts.get_host(hdb, "desktop-pc").lock.stuck is False


def test_stuck_threshold_from_env(hdb, monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_SEC", "100")
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=200)
    assert hosts.get_host(hdb, "desktop-pc").lock.stuck


def test_stuck_phase_allowlist_override(hdb, monkeypatch):
    # 'compile' may legitimately run long: a per-phase override raises its threshold.
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", '{"compile": 3000}')
    hosts.claim_host(hdb, "desktop-pc", "ws/a", phase="compile")
    _age(hdb, phase_sec=2000)
    compile_lock = hosts.get_host(hdb, "desktop-pc").lock
    assert not compile_lock.stuck  # 2000 < 3000 override
    hosts.release_host(hdb, "desktop-pc", "ws/a")
    hosts.claim_host(hdb, "desktop-pc", "ws/b", phase="link")
    _age(hdb, phase_sec=2000)
    link_lock = hosts.get_host(hdb, "desktop-pc").lock
    assert link_lock.stuck  # 'link' has no override, so the 1800 default applies


def test_malformed_stuck_env_is_fail_soft(hdb, monkeypatch):
    monkeypatch.setenv("S2DOCS_STUCK_PHASES", "{not json")
    monkeypatch.setenv("S2DOCS_STUCK_SEC", "not-a-number")
    # malformed env is ignored, leaving only the built-in per-phase defaults intact
    assert get_settings().stuck_phases == {"compile:vis_rays": 5400, "compile:vis_los": 5400}
    assert get_settings().stuck_sec == 1800


# ------------------------------------------------------------------- surfaces: API
@pytest.fixture
def client(hdb):
    with TestClient(create_app(hdb.path)) as c:
        yield c


def test_api_exposes_phase_and_stuck(hdb, client):
    client.post("/api/hosts/desktop-pc/lock", json={"owner": "ws/a", "phase": "compile", "note": "dust2 rebuild"})
    _age(hdb, phase_sec=2000)
    host = client.get("/api/hosts").json()[0]
    lock = host["lock"]
    assert lock["phase"] == "compile" and lock["stuck"] and lock["phase_age_sec"] >= 2000
    assert lock["note"] == "dust2 rebuild"
    # heartbeat with a new phase clears stuck
    hb = client.post("/api/hosts/desktop-pc/heartbeat", json={"owner": "ws/a", "phase": "link"}).json()
    assert hb["phase"] == "link" and not hb["stuck"]
    assert client.get("/api/hosts/desktop-pc").json()["lock"]["stuck"] is False


# ------------------------------------------------------------------- surfaces: MCP
def test_mcp_list_hosts_exposes_stuck(hdb):
    server = create_mcp_server(hdb)

    async def claim():
        async with Client(server) as c:
            await c.call_tool("claim_host", {"host_id": "desktop-pc", "owner": "ws/a", "phase": "compile"})

    async def list_hosts_tool():
        async with Client(server) as c:
            return await c.call_tool("list_hosts", {})

    anyio.run(claim)
    _age(hdb, phase_sec=2000)
    listed = anyio.run(list_hosts_tool)
    lock = listed.structured_content["result"][0]["lock"]
    assert lock["phase"] == "compile" and lock["stuck"] is True


def test_mcp_heartbeat_takes_phase(hdb):
    server = create_mcp_server(hdb)

    async def run():
        async with Client(server) as c:
            await c.call_tool("claim_host", {"host_id": "desktop-pc", "owner": "ws/a", "phase": "compile"})
            return await c.call_tool("heartbeat_host", {"host_id": "desktop-pc", "owner": "ws/a", "phase": "link"})

    hb = anyio.run(run)
    assert hb.structured_content["phase"] == "link" and not hb.structured_content["stuck"]


# ------------------------------------------------------------------- surfaces: CLI
def _run_cli(monkeypatch, tmp_path, argv):
    from s2docs import cli

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_host_list_prints_stuck_marker(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    hosts.claim_host(db, "desktop-pc", "ws/hung", phase="compile")
    _age(db, phase_sec=2000)

    rc = _run_cli(monkeypatch, tmp_path, ["host", "list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "STUCK: desktop-pc" in captured.err and "compile" in captured.err
    assert '"stuck": true' in captured.out  # the JSON still carries the field


# --------------------------------------------------------------- build/test auto phase
class _HangingProc:
    def __init__(self, argv, stdout=None, stderr=None, **_kw):
        self.pid = 4242
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


def test_build_start_sets_phase_and_release_clears_it(hdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", _HangingProc)
    run = builds.start(hdb, "desktop-pc", "resourcecompiler addon", kind="compile")
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert lock is not None and lock.owner == f"build:{run.id}" and lock.phase == "compile"
    builds.cancel(hdb, run.id)
    builds.wait(hdb, run.id, timeout_sec=5, poll_sec=0.02)
    assert hosts.get_host(hdb, "desktop-pc").lock is None  # release clears the phase


def test_test_start_sets_test_phase_and_finish_clears_it(hdb):
    run = test_runs.start(hdb, "iso_test", host_id="desktop-pc")
    lock = hosts.get_host(hdb, "desktop-pc").lock
    assert lock is not None and lock.owner == f"test:{run.id}" and lock.phase == "test"
    test_runs.finish(hdb, run.id, "pass")
    assert hosts.get_host(hdb, "desktop-pc").lock is None


# ------------------------------------------------------- explicit expiry behaviour
def test_build_renews_its_lease_while_running(hdb, tmp_path, monkeypatch):
    """A legitimately running build keeps its host: the server renews the lease, so a TTL expiry can't
    silently cancel it (today's cancelled-build-on-lock-expiry incident)."""
    monkeypatch.setattr(builds.subprocess, "Popen", _HangingProc)
    run = builds.start(
        hdb,
        "desktop-pc",
        "compile",
        kind="compile",
        ttl_sec=60,
        lease_renew_sec=0.02,
        poll_sec=0.01,
    )
    _age(hdb, heartbeat_sec=120)  # force the lease to look expired for its 60 s TTL
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        lock = hosts.get_host(hdb, "desktop-pc").lock
        if lock is not None and lock.fresh:
            break
        time.sleep(0.02)
    assert hosts.get_host(hdb, "desktop-pc").lock.fresh  # renewed by the server, not the client
    builds.cancel(hdb, run.id)
    builds.wait(hdb, run.id, timeout_sec=5, poll_sec=0.02)


def test_build_cancelled_when_lease_is_taken(hdb, tmp_path, monkeypatch):
    """If another owner takes the host, the run stops driving it and finalizes explicitly."""
    monkeypatch.setattr(builds.subprocess, "Popen", _HangingProc)
    run = builds.start(
        hdb,
        "desktop-pc",
        "compile",
        kind="compile",
        ttl_sec=60,
        lease_renew_sec=0.02,
        poll_sec=0.01,
    )
    hosts.claim_host(hdb, "desktop-pc", "ws/new-owner", force=True)
    final = builds.wait(hdb, run.id, timeout_sec=5, poll_sec=0.02)
    assert final.status == "cancelled" and "lease lost" in (final.notes or "")
    # the new owner's lock is untouched
    assert hosts.get_host(hdb, "desktop-pc").lock.owner == "ws/new-owner"
