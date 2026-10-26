"""Host registry + locks: contention, stale takeover, heartbeat, MCP/API surface."""

import threading

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def hdb(tmp_path) -> Database:
    return Database(tmp_path / "hosts.db")


def _register(db: Database) -> None:
    hosts.register_host(
        db, "build-pc", "pompa@192.0.2.172", hostname="DESKTOP-BUILDPC", shell="powershell", os="windows"
    )


def test_register_and_get(hdb):
    _register(hdb)
    h = hosts.get_host(hdb, "build-pc")
    assert h.ssh_target == "pompa@192.0.2.172" and h.shell == "powershell" and h.lock is None
    # upsert: omitted fields keep their values
    hosts.register_host(hdb, "build-pc", "pompa@10.0.0.5")
    h = hosts.get_host(hdb, "build-pc")
    assert h.ssh_target == "pompa@10.0.0.5" and h.shell == "powershell"
    assert hosts.list_hosts(hdb)[0].id == "build-pc"


def test_register_validation(hdb):
    with pytest.raises(hosts.HostError):
        hosts.register_host(hdb, "Desktop PC", "x")
    # The unknown-host read below goes through db.reader(), so the store must exist (initialized)
    # rather than being lazily created by a writer.
    with hdb.writer():
        pass
    with pytest.raises(hosts.HostError):
        hosts.register_host(hdb, "ok-name", "x", shell="bash")
    with pytest.raises(hosts.HostError):
        hosts.get_host(hdb, "never-registered")
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "never-registered", "ws/x")


def test_lock_contention_and_release(hdb):
    _register(hdb)
    lock = hosts.claim_host(hdb, "build-pc", "ws/a")
    assert lock.owner == "ws/a" and lock.fresh
    with pytest.raises(hosts.HostLocked):
        hosts.claim_host(hdb, "build-pc", "ws/b")
    # same owner re-claiming is fine (refresh)
    assert hosts.claim_host(hdb, "build-pc", "ws/a").owner == "ws/a"
    # heartbeat by the wrong owner fails; by the owner succeeds
    with pytest.raises(hosts.HostLocked):
        hosts.heartbeat_host(hdb, "build-pc", "ws/b")
    assert hosts.heartbeat_host(hdb, "build-pc", "ws/a").fresh
    # release by the wrong owner fails; force works
    with pytest.raises(hosts.HostLocked):
        hosts.release_host(hdb, "build-pc", "ws/b")
    assert "ws/a" in hosts.release_host(hdb, "build-pc", "ws/b", force=True)
    assert hosts.get_host(hdb, "build-pc").lock is None
    assert "not locked" in hosts.release_host(hdb, "build-pc", "ws/a")


def test_stale_lock_takeover(hdb):
    _register(hdb)
    hosts.claim_host(hdb, "build-pc", "ws/dead-worker", ttl_sec=60)
    with hdb.writer() as conn, conn:  # age the heartbeat past the TTL
        conn.execute("UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-120 seconds')")
    lock = hosts.claim_host(hdb, "build-pc", "ws/new-worker")
    assert lock.owner == "ws/new-worker"
    # heartbeat on a fresh lock keeps it fresh
    hb = hosts.heartbeat_host(hdb, "build-pc", "ws/new-worker")
    assert hb.age_sec < 60


def test_lock_ttl_bounds(hdb):
    _register(hdb)
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "build-pc", "ws/a", ttl_sec=10)
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "build-pc", " ")


def test_probe_parses_and_stores(hdb, tmp_path, monkeypatch, capsys):
    _register(hdb)
    payload = '{"host":"DESKTOP-BUILDPC","gpu":[{"name":"RTX 3080","util_pct":2}],"cs2":{"build":"2000930"}}'
    script = tmp_path / "probe.ps1"
    script.write_text("Write-Output '{}'")
    monkeypatch.setattr(hosts, "PROBE_SCRIPT", script)

    def fake_run(cmd, **kw):
        assert "powershell" in cmd[-1] and "-EncodedCommand" in cmd[-1]

        class R:
            returncode = 0
            stderr = ""
            stdout = "#< CLIXML\n<Objs></Objs>\n" + payload + "\n"

        return R()

    monkeypatch.setattr(hosts.subprocess, "run", fake_run)
    h = hosts.probe_host(hdb, "build-pc")
    assert h.hostname == "DESKTOP-BUILDPC" and h.probed_at is not None
    assert h.facts["probe"]["gpu"][0]["name"] == "RTX 3080"


def test_probe_failures(hdb, monkeypatch, tmp_path):
    _register(hdb)
    monkeypatch.setattr(hosts, "PROBE_SCRIPT", tmp_path / "x.ps1")
    (tmp_path / "x.ps1").write_text("x")

    def boom(cmd, **kw):
        raise hosts.subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(hosts.subprocess, "run", boom)
    with pytest.raises(hosts.HostError, match="timed out"):
        hosts.probe_host(hdb, "build-pc", timeout=5)


def test_mcp_host_flow(hdb):
    server = create_mcp_server(hdb)

    async def main():
        async with Client(server) as c:
            await c.call_tool("register_host", {"host_id": "rtx-pc", "ssh_target": "rtx-pc", "shell": "cmd"})
            claimed = await c.call_tool("claim_host", {"host_id": "rtx-pc", "owner": "coord", "ttl_sec": 300})
            stolen = await c.call_tool("claim_host", {"host_id": "rtx-pc", "owner": "other"})
            listed = await c.call_tool("list_hosts", {})
            released = await c.call_tool("release_host", {"host_id": "rtx-pc", "owner": "coord"})
            bad_id = await c.call_tool("claim_host", {"host_id": "Bad ID", "owner": "coord"})
            return claimed, stolen, listed, released, bad_id

    claimed, stolen, listed, released, bad_id = anyio.run(main)
    assert not claimed.is_error and claimed.structured_content["owner"] == "coord"
    assert stolen.is_error and "locked by" in stolen.content[0].text
    host = listed.structured_content["result"][0]
    assert host["id"] == "rtx-pc" and host["lock"]["fresh"]
    assert not released.is_error
    assert bad_id.is_error and "invalid host id" in bad_id.content[0].text


# ------------------------------------------------- atomic, owner-conditional locks
def test_claim_is_atomic_under_a_race(hdb):
    """The review's finding: claim_host was a SELECT-then-INSERT, so two racing claims both INSERT and
    the loser raised IntegrityError (HTTP 500). Exactly one must win; the other gets HostLocked."""
    _register(hdb)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim(name: str) -> None:
        barrier.wait()
        try:
            hosts.claim_host(hdb, "build-pc", name)
            outcomes[name] = "ok"
        except hosts.HostLocked:
            outcomes[name] = "locked"
        except Exception as exc:  # noqa: BLE001 - an IntegrityError leaking here is exactly the bug
            outcomes[name] = f"error:{type(exc).__name__}"

    threads = [threading.Thread(target=claim, args=(n,)) for n in ("ws/one", "ws/two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()) == ["locked", "ok"], outcomes
    lock = hosts.get_host(hdb, "build-pc").lock
    assert lock is not None and lock.owner in ("ws/one", "ws/two")


def test_heartbeat_and_release_are_owner_conditional(hdb):
    """A former owner's heartbeat/release must not touch the new owner's lock (the review's finding)."""
    _register(hdb)
    hosts.claim_host(hdb, "build-pc", "ws/a", phase="compile")
    hosts.claim_host(hdb, "build-pc", "ws/b", force=True)  # takeover
    before = hosts.get_host(hdb, "build-pc").lock.heartbeat_at
    with pytest.raises(hosts.HostLocked):
        hosts.heartbeat_host(hdb, "build-pc", "ws/a")
    assert hosts.get_host(hdb, "build-pc").lock.heartbeat_at == before  # new owner's lock unchanged
    assert hosts.heartbeat_host(hdb, "build-pc", "ws/b").fresh  # the real owner can heartbeat
    with pytest.raises(hosts.HostLocked):
        hosts.release_host(hdb, "build-pc", "ws/a")
    assert hosts.get_host(hdb, "build-pc").lock.owner == "ws/b"


def test_api_claim_race_conflict_is_409(hdb):
    _register(hdb)
    with TestClient(create_app(hdb.path)) as c:
        assert c.post("/api/hosts/build-pc/lock", json={"owner": "ws/a"}).status_code == 200
        r = c.post("/api/hosts/build-pc/lock", json={"owner": "ws/b"})
        assert r.status_code == 409 and r.json()["host_locked"]
        # forced takeover: the former owner's heartbeat must 409 and not refresh the new owner's lock
        taken = c.post("/api/hosts/build-pc/lock", json={"owner": "ws/b", "force": True})
        assert taken.status_code == 200 and taken.json()["owner"] == "ws/b"
        hb = c.post("/api/hosts/build-pc/heartbeat", json={"owner": "ws/b"})
        before = hb.json()["heartbeat_at"]
        assert c.post("/api/hosts/build-pc/heartbeat", json={"owner": "ws/a"}).status_code == 409
        assert c.get("/api/hosts/build-pc").json()["lock"]["heartbeat_at"] == before


# ------------------------------------------------------- read paths use the reader
def test_host_reads_do_not_take_the_write_lock(hdb, monkeypatch):
    """GET-only host paths (list/get) must read through db.reader(), never the writer."""
    _register(hdb)

    def boom(self):
        raise AssertionError("a read path took the write lock (db.writer)")

    monkeypatch.setattr(Database, "writer", boom)
    assert hosts.list_hosts(hdb)[0].id == "build-pc"
    assert hosts.get_host(hdb, "build-pc").id == "build-pc"


# The exact `facts` shape `s2docs host probe desktop-pc` stored on 2026-10-25 (trimmed to the CS2 part).
PROBED = {"probe": {"host": "DESKTOP-39NF657", "cs2": {
    "path": "c:\\program files (x86)\\steam\\steamapps\\common\\Counter-Strike Global Offensive",
    "build": "2000935", "workshop_tools": True}}}


def test_cs2_install_path_reads_the_probe_facts():
    from s2docs import models as m
    host = m.Host(id="desktop-pc", ssh_target="desktop-pc", facts=PROBED)
    assert hosts.cs2_install_path(host) == PROBED["probe"]["cs2"]["path"]


@pytest.mark.parametrize("facts", [{}, {"probe": {}}, {"probe": {"cs2": {}}}, {"cs2_path": "C:\\x"}])
def test_cs2_install_path_refuses_unprobed_hosts(facts):
    from s2docs import models as m
    host = m.Host(id="pomps-pc", ssh_target="pomps-pc", facts=facts)
    with pytest.raises(hosts.HostError, match=r"s2docs host probe pomps-pc"):
        hosts.cs2_install_path(host)
