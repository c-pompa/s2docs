"""Web-UI visibility layer: host lock history and the per-host activity filters the Hosts/Activity
pages read. No real host/network: locks are DB-only and the ssh layer is never touched.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime

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


def test_frontend_projects_timeline_and_test_run_stuck(client):
    """The Projects page renders the project's recent_events as a Timeline (newest first) through the
    Central formatter, and test-run rows carry the host lock's STUCK badge like build rows."""
    js = client.get("/app.js").text
    assert "Timeline" in js and "recent_events" in js
    assert "lockStuckBadge(t.lock)" in js


def test_api_build_and_test_lists_carry_lock(client):
    """The Activity page's lockStuckBadge reads r.lock — build and test-run list rows must carry it."""
    started = client.post("/api/test-runs", json={"name": "iso_lock", "host_id": "desktop-pc"})
    assert started.status_code == 201
    row = client.get("/api/test-runs", params={"host_id": "desktop-pc"}).json()[0]
    assert row["lock"] is not None and row["lock"]["owner"] == f"test:{started.json()['id']}"


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


# ------------------------------------------------------- run time (elapsed) display
# The user ask: every row on Activity / Orchestration / Hosts must answer "how long has this been
# going / how long did it take". The one humanizer lives in app.js; these tests pin the timestamps
# each row needs (with known values, so the duration is exact) and then execute that humanizer.
def _secs(iso: str) -> int:
    """Epoch seconds of an API timestamp ('...Z'), for exact duration arithmetic in a test."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


@pytest.fixture
def timed_rows(wdb, client):
    """Known-timestamp rows across the services the pages render: a running and a finished build, a
    verified deploy plus a dry-run one (no timestamps), and a running and a finished test session."""
    with wdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, created_at, updated_at) "
            "VALUES ('b-live', 'desktop-pc', 'compile', 'x', 'running', 'build:b-live', "
            "'2026-10-13T10:00:00Z', '2026-10-13T10:00:00Z')"
        )
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, created_at, updated_at) "
            "VALUES ('b-done', 'desktop-pc', 'compile', 'x', 'succeeded', 'build:b-done', "
            "'2026-10-13T10:00:00Z', '2026-10-13T10:02:05Z')"
        )
        conn.execute(
            "INSERT INTO deployments (id, host_id, dest_root, entries, status, created_at, updated_at) "
            "VALUES ('d-1a2b3c4d', 'desktop-pc', 'C:/dst', '[]', 'verified', "
            "'2026-10-13T10:00:00Z', '2026-10-13T10:00:30Z')"
        )
        conn.execute(
            "INSERT INTO deployments (id, host_id, dest_root, entries, status, notes, created_at, updated_at) "
            "VALUES ('d-dry', 'desktop-pc', 'C:/dst', '[]', 'pushed', 'dry-run: nothing copied', "
            "'dry-run', 'dry-run')"
        )
    live_test = test_runs.start(wdb, "iso_live")  # no host: no lock to claim
    done_test = test_runs.start(wdb, "iso_tunnel", host_id="desktop-pc")
    test_runs.finish(wdb, done_test.id, "pass")
    with wdb.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET started_at = ?, finished_at = ? WHERE id = ?",
            ("2026-10-13T10:00:00Z", "2026-10-13T10:02:05Z", done_test.id),
        )
        conn.execute(
            "UPDATE test_runs SET started_at = ?, updated_at = ? WHERE id = ?",
            ("2026-10-13T10:00:00Z", "2026-10-13T10:00:00Z", live_test.id),
        )
    builds = {r["id"]: r for r in client.get("/api/builds").json()}
    deploys = {d["id"]: d for d in client.get("/api/deploys").json()}
    return {
        "build_live": builds["b-live"],
        "build_done": builds["b-done"],
        "deploy": deploys["d-1a2b3c4d"],
        "deploy_dry": deploys["d-dry"],
        "test_live": client.get(f"/api/test-runs/{live_test.id}").json(),
        "test_done": client.get(f"/api/test-runs/{done_test.id}").json(),
    }


def test_run_rows_carry_exact_duration_timestamps(timed_rows):
    """Finished rows: duration is exactly updated_at - created_at (build 125 s, deploy 30 s) and a
    finished test run's server-computed elapsed_sec is frozen at the same 125 s."""
    done = timed_rows["build_done"]
    assert done["status"] == "succeeded"
    assert _secs(done["updated_at"]) - _secs(done["created_at"]) == 125
    dep = timed_rows["deploy"]
    assert dep["status"] == "verified"
    assert _secs(dep["updated_at"]) - _secs(dep["created_at"]) == 30
    assert timed_rows["test_done"]["state"] == "pass"
    assert timed_rows["test_done"]["elapsed_sec"] == 125
    # Running rows: created_at is the start (the renderer ticks to now), and the live test run's
    # elapsed_sec comes back server-computed rather than frozen.
    live = timed_rows["build_live"]
    assert live["status"] == "running" and live["created_at"] == "2026-10-13T10:00:00Z"
    assert timed_rows["test_live"]["state"] == "running"
    assert timed_rows["test_live"]["elapsed_sec"] >= 0
    # A dry-run deploy has no real timestamps — the row must degrade, not throw.
    assert timed_rows["deploy_dry"]["created_at"] == "dry-run"


def test_project_runtime_and_step_duration_fields(wdb, client):
    """Project list rows carry created_at (total runtime) and each step carries started_at (its first
    event), so the step view can show per-step durations without a schema change."""
    pid = client.post(
        "/api/projects", json={"workflow": "cs2_camera_addon", "goal": "iso viewer", "game": "cs2"}
    ).json()["project_id"]
    step = client.get(f"/api/projects/{pid}").json()["steps"][0]["id"]
    assert client.post(f"/api/projects/{pid}/steps/{step}", json={"status": "in_progress"}).status_code == 200
    with wdb.writer() as conn, conn:
        conn.execute(
            "UPDATE projects SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2026-10-13T10:00:00Z", "2026-10-13T10:05:00Z", pid),
        )
        conn.execute(
            "UPDATE project_steps SET updated_at = ? WHERE project_id = ? AND step_id = ?",
            ("2026-10-13T10:04:05Z", pid, step),
        )
        conn.execute(
            "UPDATE project_events SET ts = ? WHERE project_id = ? AND step_id = ?",
            ("2026-10-13T10:02:00Z", pid, step),
        )
    summary = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert _secs(summary["updated_at"]) - _secs(summary["created_at"]) == 300  # total runtime
    project = client.get(f"/api/projects/{pid}").json()
    touched = next(s for s in project["steps"] if s["id"] == step)
    assert touched["started_at"] == "2026-10-13T10:02:00Z" and touched["updated_at"] == "2026-10-13T10:04:05Z"
    assert _secs(touched["updated_at"]) - _secs(touched["started_at"]) == 125  # per-step duration
    untouched = [s for s in project["steps"] if s["id"] != step]
    assert untouched and all(s["started_at"] is None for s in untouched)  # renders '—', no invented time


def test_frontend_duration_columns(client):
    """Every run table renders a duration from the one humanizer: build/test/deploy rows, the
    projects list, and the step table + DAG nodes."""
    js = client.get("/app.js").text
    # one contiguous helper block: parseTs + human + live/settled switch + the renderer
    assert "duration-helpers:start" in js and "duration-helpers:end" in js
    assert "const human" in js and "const runSeconds" in js and "const runTime" in js
    assert 'LIVE_STATES = new Set(["queued", "running", "active", "in_progress"])' in js
    # build / deploy / test rows each get an Elapsed column
    assert js.count("<th>Elapsed</th>") == 3
    assert "runTime(d)" in js and "human(t.elapsed_sec)" in js and "runTime(r)" in js
    # orchestration: project list total runtime + per-step duration (table and DAG)
    assert "<th>Runtime</th>" in js and "<th>Duration</th>" in js
    assert "runTime(p)" in js and "stepTime(s)" in js


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_duration_rendering_executes_in_node(client, timed_rows):
    """Lift the shipped humanizer out of app.js and run it against the fixture rows, so 'live while
    running, frozen once finished' is checked as behaviour, not just as a string in the file."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    block = js.split("// duration-helpers:start", 1)[1].split("// duration-helpers:end", 1)[0]
    script = (
        'const NOW = Date.parse("2026-10-13T10:05:00Z");\n'
        "Date.now = () => NOW;\n"
        f"{block}\n"
        f"const ROWS = {json.dumps(timed_rows)};\n"
        "console.log(JSON.stringify({\n"
        "  buildLive: runTime(ROWS.build_live),\n"
        "  buildDone: runTime(ROWS.build_done),\n"
        "  deploy: runTime(ROWS.deploy),\n"
        "  deployDry: runTime(ROWS.deploy_dry),\n"
        "  testDone: human(ROWS.test_done.elapsed_sec),\n"
        "  testLiveRunning: ROWS.test_live.state === 'running' && ROWS.test_live.elapsed_sec >= 0,\n"
        "  projectActive: runTime({status: 'active', created_at: '2026-10-13T10:00:00Z',"
        " updated_at: '2026-10-13T10:00:00Z'}),\n"
        "  projectDone: runTime({status: 'done', created_at: '2026-10-13T10:00:00Z',"
        " updated_at: '2026-10-13T10:01:00Z'}),\n"
        "  stepLive: runTime({status: 'in_progress', started_at: '2026-10-13T10:04:00Z',"
        " updated_at: '2026-10-13T10:04:00Z'}),\n"
        "  stepDone: runTime({status: 'done', started_at: '2026-10-13T10:02:00Z',"
        " updated_at: '2026-10-13T10:04:05Z'}),\n"
        "  stepUntouched: runTime({status: 'pending'}),\n"
        "}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "buildLive": "5m00s",  # running: ticks from created_at to now (NOW - 10:00:00 = 300 s)
        "buildDone": "2m05s",  # finished: frozen at updated_at - created_at (125 s)
        "deploy": "30s",
        "deployDry": "—",  # 'dry-run' placeholder timestamps degrade instead of throwing
        "testDone": "2m05s",  # test rows render the server's own elapsed_sec
        "testLiveRunning": True,
        "projectActive": "5m00s",
        "projectDone": "1m00s",
        "stepLive": "1m00s",  # in_progress: started_at -> now
        "stepDone": "2m05s",  # done: started_at -> updated_at
        "stepUntouched": "—",  # no start recorded: the view shows '—', never a made-up duration
    }


# ------------------------------------------------------- Central (America/Chicago) time display
# The user ask: timestamps must read as actual local clock time in Central, not just '5m ago' or a
# raw UTC ISO. Every timestamp renderer goes through one formatter: '<Central> (<relative>)'. The
# zone label is 'CT' (the zone shifts CST/CDT with DST, the label must not) and Intl supplies the
# DST rules, so these tests pin one winter (CST) and one summer (CDT) instant.
def test_frontend_central_time_markers(client):
    """The shared Central formatter exists next to human()/parseTs and every timestamp cell uses it."""
    js = client.get("/app.js").text
    # one helper block, sits with the other time helpers
    assert "central-time-helpers:start" in js and "central-time-helpers:end" in js
    assert 'CT_ZONE = "America/Chicago"' in js
    assert "const ctAbs" in js and "const ctStamp" in js and "const fmtTime" in js and "const nowCT" in js
    # 12-hour clock, short month, seconds; labelled 'CT' (never CST/CDT — the zone shifts with DST)
    assert 'hour12: true' in js and 'month: "short"' in js and 'second: "2-digit"' in js
    assert "} CT`" in js and "CST" not in js and "CDT" not in js
    # every timestamp cell renders through the one formatter (lock cell, lock history, deploy, hosts
    # probed x2, project list, step table, Activity idle note)
    assert js.count("fmtTime(") >= 8
    assert "heartbeat ${fmtTime(lock.heartbeat_at)}" in js
    assert "fmtTime(e.ts)" in js  # lock history
    assert "fmtTime(d.created_at)" in js  # deployments
    assert "fmtTime(p.updated_at)" in js and "fmtTime(s.updated_at)" in js  # projects + step table
    assert js.count("fmtTime(h.probed_at)") == 2  # hosts list + host detail
    # the raw ISO stays reachable in a tooltip; a null reads '—', an unparseable value is kept escaped
    assert 'title="${esc(String(iso))}"' in js
    # 'server now' line so a relative age can be sanity-checked (Hosts + Activity)
    assert "Server now:" in js and "nowLine()" in js
    assert js.count("nowLine()") >= 4  # hosts list, host detail, activity (incl. empty state)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_central_time_rendering_executes_in_node(client):
    """Lift the shipped Central formatter out of app.js and run it: the exact absolute string, the
    DST switch (one CST winter instant, one CDT summer instant), and the fallbacks."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    dur = js.split("// duration-helpers:start", 1)[1].split("// duration-helpers:end", 1)[0]
    ct = js.split("// central-time-helpers:start", 1)[1].split("// central-time-helpers:end", 1)[0]
    i = js.index("const secsAgo")
    secs = js[i : js.index("};", i) + 2]  # ctStamp's relative age comes from secsAgo()
    script = (
        'const esc = (v) => String(v ?? "");\n'
        f"{dur}\n{secs}\n{ct}\n"
        # Winter: 2026-01-15 22:42:21Z is 4:42 PM CST (UTC-6); summer is one hour offset (CDT).
        'const WINTER = Date.parse("2026-01-15T22:42:21Z");\n'
        'const SUMMER = Date.parse("2026-07-15T21:42:21Z");\n'
        "Date.now = () => WINTER;\n"
        "const winter = ctStamp('2026-01-15T22:37:21Z');\n"
        "const earlier = ctStamp('2026-01-15T22:12:21Z');\n"
        "const winterHtml = fmtTime('2026-01-15T22:37:21Z');\n"
        "const unparseableStamp = ctStamp('dry-run');\n"
        "const unparseableHtml = fmtTime('dry-run');\n"
        "const nullHtml = fmtTime(null);\n"
        "Date.now = () => SUMMER;\n"
        "const summer = ctStamp('2026-07-15T21:37:21Z');\n"
        "const now = nowCT();\n"
        "console.log(JSON.stringify({winter, earlier, winterHtml, summer, unparseableStamp,"
        " unparseableHtml, nullHtml, now}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["winter"] == "Jan 15, 4:37:21 PM CT (5m00s ago)"  # CST: UTC-6
    assert out["earlier"] == "Jan 15, 4:12:21 PM CT (30m00s ago)"
    assert out["summer"] == "Jul 15, 4:37:21 PM CT (5m00s ago)"  # CDT: UTC-5, the same wall clock
    assert out["winterHtml"] == (
        '<span title="2026-01-15T22:37:21Z">Jan 15, 4:37:21 PM CT (5m00s ago)</span>'
    )
    assert out["unparseableStamp"] is None and out["unparseableHtml"] == "dry-run"  # kept, escaped
    assert out["nullHtml"] == "—"
    assert re.fullmatch(r"\d{1,2}:\d{2} [AP]M CT", out["now"]), out["now"]  # '4:42 PM CT'
