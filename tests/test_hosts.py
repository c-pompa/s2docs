"""Host registry + locks: contention, stale takeover, heartbeat, MCP/API surface."""

import anyio
import pytest
from mcp import Client

from s2docs import hosts
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def hdb(tmp_path) -> Database:
    return Database(tmp_path / "hosts.db")


def _register(db: Database) -> None:
    hosts.register_host(
        db, "desktop-pc", "pompa@100.64.130.72", hostname="DESKTOP-39NF657", shell="powershell", os="windows"
    )


def test_register_and_get(hdb):
    _register(hdb)
    h = hosts.get_host(hdb, "desktop-pc")
    assert h.ssh_target == "pompa@100.64.130.72" and h.shell == "powershell" and h.lock is None
    # upsert: omitted fields keep their values
    hosts.register_host(hdb, "desktop-pc", "pompa@10.0.0.5")
    h = hosts.get_host(hdb, "desktop-pc")
    assert h.ssh_target == "pompa@10.0.0.5" and h.shell == "powershell"
    assert hosts.list_hosts(hdb)[0].id == "desktop-pc"


def test_register_validation(hdb):
    with pytest.raises(hosts.HostError):
        hosts.register_host(hdb, "Desktop PC", "x")
    with pytest.raises(hosts.HostError):
        hosts.register_host(hdb, "ok-name", "x", shell="bash")
    with pytest.raises(hosts.HostError):
        hosts.get_host(hdb, "never-registered")
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "never-registered", "ws/x")


def test_lock_contention_and_release(hdb):
    _register(hdb)
    lock = hosts.claim_host(hdb, "desktop-pc", "ws/a")
    assert lock.owner == "ws/a" and lock.fresh
    with pytest.raises(hosts.HostLocked):
        hosts.claim_host(hdb, "desktop-pc", "ws/b")
    # same owner re-claiming is fine (refresh)
    assert hosts.claim_host(hdb, "desktop-pc", "ws/a").owner == "ws/a"
    # heartbeat by the wrong owner fails; by the owner succeeds
    with pytest.raises(hosts.HostLocked):
        hosts.heartbeat_host(hdb, "desktop-pc", "ws/b")
    assert hosts.heartbeat_host(hdb, "desktop-pc", "ws/a").fresh
    # release by the wrong owner fails; force works
    with pytest.raises(hosts.HostLocked):
        hosts.release_host(hdb, "desktop-pc", "ws/b")
    assert "ws/a" in hosts.release_host(hdb, "desktop-pc", "ws/b", force=True)
    assert hosts.get_host(hdb, "desktop-pc").lock is None
    assert "not locked" in hosts.release_host(hdb, "desktop-pc", "ws/a")


def test_stale_lock_takeover(hdb):
    _register(hdb)
    hosts.claim_host(hdb, "desktop-pc", "ws/dead-worker", ttl_sec=60)
    with hdb.writer() as conn, conn:  # age the heartbeat past the TTL
        conn.execute("UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-120 seconds')")
    lock = hosts.claim_host(hdb, "desktop-pc", "ws/new-worker")
    assert lock.owner == "ws/new-worker"
    # heartbeat on a fresh lock keeps it fresh
    hb = hosts.heartbeat_host(hdb, "desktop-pc", "ws/new-worker")
    assert hb.age_sec < 60


def test_lock_ttl_bounds(hdb):
    _register(hdb)
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "desktop-pc", "ws/a", ttl_sec=10)
    with pytest.raises(hosts.HostError):
        hosts.claim_host(hdb, "desktop-pc", " ")


def test_probe_parses_and_stores(hdb, tmp_path, monkeypatch, capsys):
    _register(hdb)
    payload = '{"host":"DESKTOP-39NF657","gpu":[{"name":"RTX 3080","util_pct":2}],"cs2":{"build":"2000930"}}'
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
    h = hosts.probe_host(hdb, "desktop-pc")
    assert h.hostname == "DESKTOP-39NF657" and h.probed_at is not None
    assert h.facts["probe"]["gpu"][0]["name"] == "RTX 3080"


def test_probe_failures(hdb, monkeypatch, tmp_path):
    _register(hdb)
    monkeypatch.setattr(hosts, "PROBE_SCRIPT", tmp_path / "x.ps1")
    (tmp_path / "x.ps1").write_text("x")

    def boom(cmd, **kw):
        raise hosts.subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(hosts.subprocess, "run", boom)
    with pytest.raises(hosts.HostError, match="timed out"):
        hosts.probe_host(hdb, "desktop-pc", timeout=5)


def test_mcp_host_flow(hdb):
    server = create_mcp_server(hdb)

    async def main():
        async with Client(server) as c:
            await c.call_tool("register_host", {"host_id": "pomps-pc", "ssh_target": "pomps-pc", "shell": "cmd"})
            claimed = await c.call_tool("claim_host", {"host_id": "pomps-pc", "owner": "coord", "ttl_sec": 300})
            stolen = await c.call_tool("claim_host", {"host_id": "pomps-pc", "owner": "other"})
            listed = await c.call_tool("list_hosts", {})
            released = await c.call_tool("release_host", {"host_id": "pomps-pc", "owner": "coord"})
            bad_id = await c.call_tool("claim_host", {"host_id": "Bad ID", "owner": "coord"})
            return claimed, stolen, listed, released, bad_id

    claimed, stolen, listed, released, bad_id = anyio.run(main)
    assert not claimed.is_error and claimed.structured_content["owner"] == "coord"
    assert stolen.is_error and "locked by" in stolen.content[0].text
    host = listed.structured_content["result"][0]
    assert host["id"] == "pomps-pc" and host["lock"]["fresh"]
    assert not released.is_error
    assert bad_id.is_error and "invalid host id" in bad_id.content[0].text
