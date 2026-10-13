"""Web-UI visibility layer: host lock history and the per-host activity filters the Hosts/Activity
pages read. No real host/network: locks are DB-only and the ssh layer is never touched.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import hosts, test_runs
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def wdb(tmp_path) -> Database:
    db = Database(tmp_path / "webui.db")
    hosts.register_host(db, "desktop-pc", "pompa@100.64.130.72", hostname="DESKTOP-39NF657", shell="powershell")
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    return db


def _age_lock(db: Database) -> None:
    """Push every lock's heartbeat two minutes into the past so it is stale (ttl stays 1800 by default
    only where the test set a short one)."""
    with db.writer() as conn, conn:
        conn.execute("UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-120 seconds')")


# ------------------------------------------------------------ lock history (service)
def test_lock_history_records_claim_and_release(wdb, tmp_path):
    hosts.claim_host(wdb, "desktop-pc", "ws/a")
    hosts.release_host(wdb, "desktop-pc", "ws/a")
    evs = hosts.lock_history(wdb, "desktop-pc")
    assert [e.event for e in evs] == ["released", "claimed"]  # newest first
    assert evs[0].owner == "ws/a" and evs[0].host_id == "desktop-pc"
    assert evs[0].detail is None and evs[1].detail is None  # same owner: no takeover/release detail
    assert hosts.lock_history(wdb, "mini") == []  # no events for an untouched host


def test_lock_history_stale_takeover_detail(wdb):
    hosts.claim_host(wdb, "desktop-pc", "ws/dead", ttl_sec=60)
    _age_lock(wdb)
    hosts.claim_host(wdb, "desktop-pc", "ws/new")
    ev = hosts.lock_history(wdb, "desktop-pc")[0]
    assert ev.event == "claimed" and ev.owner == "ws/new" and "stale lock" in (ev.detail or "")


def test_lock_history_forced_release_and_limit(wdb):
    hosts.claim_host(wdb, "desktop-pc", "ws/a")
    hosts.release_host(wdb, "desktop-pc", "ws/other", force=True)  # released by a different owner
    assert "ws/other" in (hosts.lock_history(wdb, "desktop-pc")[0].detail or "")
    # many events, newest-first and capped by limit
    for i in range(5):
        hosts.claim_host(wdb, "desktop-pc", f"ws/{i}")
        hosts.release_host(wdb, "desktop-pc", f"ws/{i}")
    assert len(hosts.lock_history(wdb, "desktop-pc", limit=3)) == 3
    assert hosts.lock_history(wdb, "desktop-pc")[0].event == "released"
    with pytest.raises(hosts.HostError):
        hosts.lock_history(wdb, "Bad Id")


# ----------------------------------------------------- per-host filters (activity)
def test_test_runs_host_filter(wdb):
    a = test_runs.start(wdb, "iso_test", host_id="desktop-pc")
    test_runs.start(wdb, "other")  # no host
    assert [r.id for r in test_runs.list_runs(wdb, host_id="desktop-pc")] == [a.id]
    assert test_runs.list_runs(wdb, host_id="mini") == []
    # state + host combine
    test_runs.finish(wdb, a.id, "pass")
    assert [r.id for r in test_runs.list_runs(wdb, state="pass", host_id="desktop-pc")] == [a.id]
    assert test_runs.list_runs(wdb, state="pass", host_id="mini") == []


# ------------------------------------------------------------------- HTTP surface
@pytest.fixture
def client(wdb):
    with TestClient(create_app(wdb.path)) as c:
        yield c


def test_api_host_locks_route(wdb, client):
    hosts.claim_host(wdb, "desktop-pc", "ws/a")
    hosts.release_host(wdb, "desktop-pc", "ws/a")
    r = client.get("/api/hosts/desktop-pc/locks")
    assert r.status_code == 200
    events = r.json()
    assert [e["event"] for e in events] == ["released", "claimed"]
    assert events[0]["owner"] == "ws/a" and events[0]["id"] > events[1]["id"]
    assert client.get("/api/hosts/ghost/locks").status_code == 400  # HostError → 400, like GET /api/hosts/{id}
    assert client.get("/api/hosts/desktop-pc/locks", params={"limit": 1}).json().__len__() == 1


def test_api_test_runs_host_filter(client):
    started = client.post("/api/test-runs", json={"name": "iso_test", "host_id": "desktop-pc"})
    assert started.status_code == 201
    assert client.get("/api/test-runs", params={"host_id": "desktop-pc"}).json()[0]["id"] == started.json()["id"]
    assert client.get("/api/test-runs", params={"host_id": "mini"}).json() == []


# ------------------------------------------------- Activity page's data surface
def test_activity_api_surface(client):
    """The three feeds the Activity page reads, plus the 'now' test-run probe (404 when idle)."""
    assert client.get("/api/test-runs/current").status_code == 404  # no active run yet
    assert client.get("/api/builds", params={"active": "true"}).json() == []
    for path in ("/api/builds", "/api/test-runs", "/api/deploys"):
        assert isinstance(client.get(path).json(), list)
    # a running test session becomes the page's 'Now' card
    started = client.post("/api/test-runs", json={"name": "iso_tunnel", "host_id": "desktop-pc"}).json()
    assert client.get("/api/test-runs/current").json()["id"] == started["id"]
    assert started["state"] == "running"


# ------------------------------------------ Orchestration page's step-level surface
def test_orchestration_step_view_shape(client):
    """GET /api/projects/{id} carries the per-step fields the step graph/table render, and the
    evidence rows the view counts per step come from /api/evidence?project_id."""
    start = client.post("/api/projects", json={"workflow": "cs2_camera_addon", "goal": "iso viewer", "game": "cs2"})
    pid = start.json()["project_id"]
    p = client.get(f"/api/projects/{pid}").json()
    assert p["steps"], "a project always exposes its steps"
    fields = {"id", "title", "kind", "status", "gate", "updated_at", "depends_on", "artifacts"}
    assert all(fields <= set(s) for s in p["steps"])
    # evidence is listed per project and tagged with its step (the view groups it by step_id)
    step_id = p["steps"][0]["id"]
    client.post("/api/evidence", json={"project_id": pid, "step_id": step_id, "kind": "console_transcript"})
    client.post("/api/evidence", json={"project_id": pid, "step_id": step_id, "kind": "note"})
    evs = client.get("/api/evidence", params={"project_id": pid}).json()
    assert all(e["project_id"] == pid for e in evs)
    kinds = [e["kind"] for e in evs if e["step_id"] == step_id]
    assert kinds.count("console_transcript") == 1 and kinds.count("note") == 1  # gate counts non-note only


# ----------------------------------------------- frontend contract (no JS test runner)
def test_frontend_activity_and_orchestration_markers(client):
    """Guard the UI wiring a headless test can't execute: the Activity page must explain itself and
    group by host, poll fast only while active; Projects must be framed as Orchestration with a step DAG."""
    js = client.get("/app.js").text
    # Activity: honest empty state + the service-fed framing + host grouping
    assert "service-fed" in js
    assert "No activity yet" in js and "build_start" in js and "test_start" in js and "deploy_push" in js
    assert "By host" in js
    # adaptive live refresh: 5 s while a run is active, slower (15 s) when idle
    assert "state.activityActive" in js and "? 5000 : 15000" in js and "setTimeout(tick" in js
    # Projects reframed as Orchestration with a step-level DAG view
    assert '["projects", "Orchestration"]' in js
    assert "Step graph" in js and "function stepDag" in js and "ROUTES.orchestration = ROUTES.projects" in js
    # lock.stuck badge is rendered when present and tolerated when absent
    assert "STUCK" in js
    # the DAG needs its layout rules
    assert ".dag-node" in client.get("/style.css").text


# ------------------------------------------------------------------- MCP surface
def test_mcp_lock_history_via_list_hosts(wdb):
    """The lock state the Hosts page renders is the same object list_hosts returns over MCP."""
    server = create_mcp_server(wdb)

    async def main():
        async with Client(server) as c:
            await c.call_tool("claim_host", {"host_id": "desktop-pc", "owner": "project:p-1a2b3c4d"})
            listed = await c.call_tool("list_hosts", {})
            return listed

    listed = anyio.run(main)
    host = next(h for h in listed.structured_content["result"] if h["id"] == "desktop-pc")
    assert host["lock"]["owner"] == "project:p-1a2b3c4d" and host["lock"]["fresh"]
