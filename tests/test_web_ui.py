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


def test_frontend_live_refresh_never_clobbers_typing(client):
    """A live page's auto-refresh replaces its #live region's innerHTML — and the job page's verdict
    notes + screenshot request live inside that region. The tick must (1) SKIP the render while an
    input in the region is focused/holds unsubmitted text, and (2) snapshot + restore each input's
    value/focus/caret across any render it does run, so typing can't be wiped mid-edit."""
    js = client.get("/app.js").text
    # (1) skip-while-editing guard sits in the tick before the innerHTML swap
    assert "function liveBeingEdited" in js and "liveBeingEdited(el)" in js
    assert "if (liveBeingEdited(el)) { liveTimer = setTimeout(tick, delay()); return; }" in js
    # the `dirty` flag is set on 'input' and cleared after a successful submit
    assert "let liveDirty = false" in js and "liveDirty = true" in js and "liveDirty = false" in js
    assert 'addEventListener("input", liveInputListener)' in js
    # (2) the value-restore helper wraps the innerHTML replacement
    assert "function snapshotLiveInputs" in js and "function restoreLiveInputs" in js
    assert "const saved = snapshotLiveInputs(el)" in js and "restoreLiveInputs(el, saved)" in js
    # (3) a successful verdict submit clears the note box and refreshes the card in place (a
    # transition-aware refresh, not a full route() re-render, so the recorded state and the scroll stay put)
    verdict = js.split("function bindJobVerdict", 1)[1]
    assert "form.elements.notes.value = \"\"" in verdict and "refreshJobLive(" in verdict


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
# zone label is the real Intl abbreviation for that date — 'CST' in standard time, 'CDT' in daylight
# time (a November date reads CST even in a CDT year) — so these tests pin a winter and a summer instant.
def test_frontend_central_time_markers(client):
    """The shared Central formatter exists next to human()/parseTs and every timestamp cell uses it."""
    js = client.get("/app.js").text
    # one helper block, sits with the other time helpers
    assert "central-time-helpers:start" in js and "central-time-helpers:end" in js
    assert 'CT_ZONE = "America/Chicago"' in js
    assert "const ctAbs" in js and "const ctStamp" in js and "const fmtTime" in js and "const nowCT" in js
    # 12-hour clock, short month, seconds; the zone label comes from Intl's short timeZoneName part
    assert 'hour12: true' in js and 'month: "short"' in js and 'second: "2-digit"' in js
    assert 'timeZoneName: "short"' in js and "formatToParts" in js and 'x.type === "timeZoneName"' in js
    # no hard-coded ' CT' label survives anywhere; the abbr is per-timestamp, with 'CT' only as fallback
    assert not re.search(r" CT[`\"]", js)
    assert 'return "CT";' in js  # Intl-less fallback only
    assert "all times shown in Central time (America/Chicago, CST/CDT)" in js
    # every timestamp cell renders through the one formatter (lock cell, lock history, deploy, hosts
    # probed x2, project list, step table, Activity idle note)
    assert js.count("fmtTime(") >= 8
    assert "heartbeat ${fmtTime(lock.heartbeat_at)}" in js
    assert "fmtTime(e.ts)" in js  # lock history
    assert "fmtTime(d.created_at)" in js  # deployments
    assert "fmtTime(p.updated_at)" in js and "fmtTime(s.updated_at)" in js  # projects + step table
    assert js.count("fmtTime(h.probed_at)") >= 2  # hosts list + host detail
    # the raw ISO stays reachable in a tooltip; a null reads '—', an unparseable value is kept escaped
    assert 'title="${esc(String(iso))}"' in js
    # 'server now' line so a relative age can be sanity-checked (Hosts + Activity)
    assert "Server now:" in js and "nowLine()" in js
    assert js.count("nowLine()") >= 4  # hosts list, host detail, activity (incl. empty state)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_central_time_rendering_executes_in_node(client):
    """Lift the shipped Central formatter out of app.js and run it: the exact absolute string, the
    per-timestamp DST abbreviation (a CST winter instant, a CDT summer instant, a CST November
    instant), and the fallbacks."""
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
        # 2026-11-01 is the CST switch; a mid-November instant must read CST, not CDT.
        "const novAbs = ctAbs(new Date(Date.parse('2026-11-15T22:37:21Z')));\n"
        "console.log(JSON.stringify({winter, earlier, winterHtml, summer, unparseableStamp,"
        " unparseableHtml, nullHtml, now, novAbs}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["winter"] == "Jan 15, 4:37:21 PM CST (5m00s ago)"  # CST: UTC-6
    assert out["earlier"] == "Jan 15, 4:12:21 PM CST (30m00s ago)"
    assert out["summer"] == "Jul 15, 4:37:21 PM CDT (5m00s ago)"  # CDT: UTC-5, the same wall clock
    assert out["novAbs"] == "Nov 15, 4:37:21 PM CST"  # after the 2026-11-01 CST switch
    assert out["winterHtml"] == (
        '<span title="2026-01-15T22:37:21Z">Jan 15, 4:37:21 PM CST (5m00s ago)</span>'
    )
    assert out["unparseableStamp"] is None and out["unparseableHtml"] == "dry-run"  # kept, escaped
    assert out["nullHtml"] == "—"
    assert re.fullmatch(r"\d{1,2}:\d{2} [AP]M (CST|CDT)", out["now"]), out["now"]  # '4:42 PM CST'


# ------------------------------------------------- Build Diff: clarity layer
def test_frontend_diff_clarity_markers(client):
    """The diff page renders labelled Before/After columns, groups field-level rows under their parent,
    collapses empty sections, offers change-type + name filters and a Copy-as-Markdown button."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    assert "diff-helpers:start" in js and "diff-helpers:end" in js
    # labelled, separate before/after columns (not one 'Before → After' cell)
    assert '"<tr><th>Change</th><th>Name</th><th>Before</th><th>After</th></tr>"' in js
    # an added/removed side that does not exist reads 'absent', so an added row has no before value
    assert "absentCell" in js and "— absent" in js
    # long values go into a bounded scroll box / expander, never a mid-token slice
    assert "diff-long" in js and "diff-val" in js and "wordPreview" in js and "more chars" in js
    # `changed` rows trim the shared prefix/suffix; the old/new spans carry the diff-old/diff-new classes
    assert "function commonEdges" in js and 'diff-${side}' in js
    assert '"old"' in js and '"new"' in js
    # field-level rows cluster under their parent class/entity/event (the API `group`)
    assert "const diffGrouped" in js and "diff-group" in js
    # zero-change sections collapse to one line; a capped section says how much is hidden
    assert ": no changes" in js and "showing first" in js
    # filters wired through the hash query + the export button and its textarea fallback
    assert "filter changed names" in js
    # the change-type dropdown shows per-type totals from the server counts (labels only): its VALUES
    # stay '' / added / removed / changed so existing ?change= deep links keep working
    assert '["", `all changes (${c.added + c.removed + c.changed})`]' in js
    assert '["added", `added (${c.added})`]' in js and '["removed", `removed (${c.removed})`]' in js
    assert '["changed", `changed (${c.changed})`]' in js
    assert "Copy as Markdown" in js and "data-copy-md" in js and 'id="diff-md"' in js and "bindCopyMd" in js
    # the header shows both builds' metadata and the added/removed/changed breakdown, taken from the
    # server's pre-limit `counts` (falling back to the received rows only for an older server), and it
    # never presents a filtered subset as the diff's totals
    assert "diff-head" in js and "source_commit" in js and "diffCounts" in js
    assert "const server = d.counts && Object.keys(d.counts).length ? d.counts : null" in js
    assert "counted from the rows shown" in js
    assert "filter active: showing " in js
    # the CSS the layout relies on
    for sel in (".diff-head", ".diff-old", ".diff-new", ".diff-val", ".diff-none", "table.diff"):
        assert sel in css


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_diff_render_executes_in_node(client):
    """Lift the shipped diff helpers out of app.js and render a synthetic diff: absent sides, trimmed
    changed spans, grouped rows, an expander for a long value, an empty-section line, a truncation note
    and the Markdown export."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    block = js.split("// diff-helpers:start", 1)[1].split("// diff-helpers:end", 1)[0]
    prologue = (
        'const ESC_MAP = {"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"};\n'
        'const esc = (v) => String(v ?? "").replace(/[&<>"\']/g, (c) => ESC_MAP[c]);\n'
        'const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;\n'
    )
    long_val = "type " + " ".join(["chunk"] * 60)
    data = {
        "game": "cs2",
        "from_build": 2000930,
        "to_build": 2000932,
        "limit": 500,
        "totals": {"convars": 3, "entities": 0, "schema": 4, "events": 0, "script_api": 0},
        "sections": {
            "convars": [
                {"name": "convar:cl_demo_freecam", "change": "added", "before": None,
                 "after": "true During demo playback", "group": None},
                {"name": "convar:demo_mouse_enable_binding", "change": "removed",
                 "before": "drop Name of the binding", "after": None, "group": None},
                {"name": "convar:r_csgo_sky_occluder", "change": "changed",
                 "before": "0 Old sky occluder text", "after": "1 New sky occluder text", "group": None},
            ],
            "entities": [],
            "schema": [
                {"name": "C_CSPlayerPawn.m_flEmitSoundTime_Client", "change": "added", "before": None,
                 "after": "GameTime_t", "group": "client:C_CSPlayerPawn"},
                {"name": "CCSPlayerPawn.m_flLongValue", "change": "added", "before": None,
                 "after": long_val, "group": "server:CCSPlayerPawn"},
            ],
        },
    }
    # A CAPPED section with the server's pre-limit `counts` (the deadlock 6774->6782 shape): the header
    # totals must come from `counts`, not from the two rows received, and a filter must not look like
    # it changes those totals.
    capped = {
        "game": "deadlock",
        "from_build": 6774,
        "to_build": 6782,
        "limit": 500,
        "totals": {"schema": 699},
        "counts": {"schema": {"added": 459, "removed": 3, "changed": 237}},
        "sections": {
            "schema": [
                {"name": "CCSPlayerPawn.m_flA", "change": "added", "before": None, "after": "float32",
                 "group": "server:CCSPlayerPawn"},
                {"name": "CCSPlayerPawn.m_flB", "change": "changed", "before": "float32 1",
                 "after": "float32 2", "group": "server:CCSPlayerPawn"},
            ]
        },
    }
    script = (
        prologue
        + block
        + "\nconst D = " + json.dumps(data) + ";\n"
        + "const CAP = " + json.dumps(capped) + ";\n"
        + "const all = () => true;\n"
        + 'const onlyA = (r) => r.name === "CCSPlayerPawn.m_flA";\n'
        + "console.log(JSON.stringify({\n"
        + "  html: diffSectionsHtml(D, all),\n"
        + "  counts: diffCounts(D),\n"
        + "  md: diffMarkdown(D, all),\n"
        + "  capRowCounts: diffCounts(CAP),\n"
        + "  capFiltered: diffCounts(CAP, onlyA),\n"
        + "}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    html = out["html"]
    # an added row has NO before value (it reads absent) and its after value is shown
    added = html.split("C_CSPlayerPawn.m_flEmitSoundTime_Client", 1)[1].split("</tr>", 1)[0]
    assert "— absent" in added and "GameTime_t" in added
    # a removed row keeps its before value and shows 'absent' after
    removed = html.split("demo_mouse_enable_binding", 1)[1].split("</tr>", 1)[0]
    assert "drop Name of the binding" in removed and "— absent" in removed
    # a `changed` row trims the shared prefix/suffix, highlighting only the differing span
    assert '<span class="diff-old">0 Old</span>' in html and '<span class="diff-new">1 New</span>' in html
    assert "sky occluder text" in html  # the shared suffix stays visible (muted)
    # a long value is not cut mid-token: it lives whole inside a bounded <details>/<pre>
    assert 'class="diff-long"' in html and 'class="mono diff-val"' in html and "more chars" in html
    assert long_val in html  # the full value is present, only collapsed
    # field-level rows group under their parent key
    assert '<h3 class="diff-group mono">client:C_CSPlayerPawn' in html
    assert '<h3 class="diff-group mono">server:CCSPlayerPawn' in html
    # a zero-change section takes one line; a capped section says how much is hidden
    assert "entities: no changes" in html
    assert "showing first 2 of 4" in html
    # header counts and the Markdown export
    assert out["counts"]["added"] == 3 and out["counts"]["removed"] == 1 and out["counts"]["changed"] == 1
    assert out["counts"]["truncated"] is True
    # a CAPPED section: the header totals are the server's pre-limit counts (only 2 rows arrived), not
    # a count of the rows — and the capped section is named, with `exact` marking the counts as trusted
    assert out["capRowCounts"]["added"] == 459 and out["capRowCounts"]["removed"] == 3
    assert out["capRowCounts"]["changed"] == 237 and out["capRowCounts"]["exact"] is True
    assert out["capRowCounts"]["truncated"] is True and out["capRowCounts"]["capped"] == ["schema"]
    assert out["capRowCounts"]["total"] == 2  # rows actually received (the section shows 2 of 699)
    # an active filter reports how many rows match WITHOUT masquerading as the patch's totals
    assert out["capFiltered"]["matched"] == 1
    assert out["capFiltered"]["added"] == 459 and out["capFiltered"]["changed"] == 237
    assert "## schema" in out["md"] and "| Change | Group | Name | Before | After |" in out["md"]
    assert "| added | client:C_CSPlayerPawn | `C_CSPlayerPawn.m_flEmitSoundTime_Client` | — | GameTime_t |" in out["md"]


# ------------------------------------------------- Job page: verification UX
# The user ask: "make it clear we have changed to the next verification — all look the same and the
# button doesn't look pressed and nothing looks like it's loading or moving to the next". These tests
# pin the stepper, the NOW TESTING card and the results summary — the render functions by executing
# them, the transition/banner/countdown/ticker wiring as markers in the shipped file.
def test_frontend_verification_stepper_and_card_markers(client):
    """Stepper + NOW TESTING card + results summary + transition/ticker wiring all exist in app.js."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    # (1) the stepper model + chip renderer + the state vocabulary
    assert "function jobVerifications" in js and "function jobStepper" in js and "function vxChip" in js
    assert "VX_STATE_CLASS" in js and '"no-verdict": "vx-noverdict"' in js
    assert "vx-testing" in js and "vx-upcoming" in js
    assert 'data-vx-goto=' in js and "aria-current" in js
    # chips anchor to a timeline entry (jobTimeline tags its rows) and a delegated click scrolls there
    assert 'data-vx-step="${esc(e.phase)}"' in js and "function gotoStepTimeline" in js
    assert 'closest("[data-vx-goto]")' in js
    # (2) the card: 'TEST k of n', both explainer fields, the countdown bar, the respond hint
    assert "function nowTestingCard" in js and "TEST ${esc(k)} of ${esc(n)}" in js
    assert "What is tested" in js and "What you should see" in js and "function vxCountdown" in js
    assert "vx-bar-fill" in js and 'data-vx-fill' in js
    # current-step transition: animate the card, transient banner, and the document.title mirror
    assert "function jobPostRender" in js and "function showTransitionBanner" in js
    assert "Moved on to TEST ${k}: ${name}" in js and "vx-enter" in js
    assert "document.title = `TEST ${k}/${n} — ${name}`" in js
    # (3) pressed/sending/recorded states on the verdict form
    assert "vx-btn vx-btn-pass" in js and "vx-btn vx-btn-fail" in js and "vx-btn vx-btn-skip" in js
    assert "Sending…" in js and "b.disabled = true" in js and "b.disabled = false" in js
    assert '"Recorded: " + String(verdict.verdict).toUpperCase()' in js and "(via ${esc(verdict.source)})" in js
    assert "Waiting for the demo to move to the next test…" in js
    # (4) the results summary when the run ends (a finished-state banner + the per-step table)
    assert "function jobResultsTable" in js and "function jobNow" in js and "vx-rtable" in js
    # (5) countdown/spinners stay client-side: the 1 s ticker drives both readout and bar width
    assert "function updateCountdowns" in js and "setInterval(updateCountdowns, 1000)" in js
    assert 'el.hasAttribute("data-vx-fill")' in js and "el.style.width = " in js
    # the CSS the states rely on: pulse, press, spinner, bar transition, banner
    assert "vx-pulse" in css and "vx-btn:not(:disabled):active" in css and "vx-rot" in css
    assert "transition: width 1s linear" in css and ".vx-banner" in css and ".vx-card" in css


def _vx_fixture(status, *, current_verdict=False, done=False):
    """A job shaped like the live verify-demo run: a 'plan' event (6 tests), s1..s4 prompt events, and
    verdicts on s1 (pass/chat), s2 (fail/web), s3 (skip/console) — optionally a verdict on the current
    s4, and optionally a closing 'done' event."""
    plan = [
        {"id": f"s{i}", "index": i, "title": f"Test {i} title", "strip_name": f"test-{i}-name",
         "strip_purpose": f"purpose {i}", "strip_expect": f"expect {i}"}
        for i in range(1, 7)
    ]
    events = [{"id": 1, "level": "info", "phase": "plan", "message": "plan posted",
               "data": {"plan": plan, "wait_seconds": 120}}]
    for i in range(1, 5):
        events.append({"id": 10 + i, "level": "info", "phase": f"s{i}", "message": f"STEP {i}/6",
                       "data": {"prompt": f"prompt {i}", "look_for": f"look {i}", "step_index": i, "step_total": 6,
                                "strip_name": f"test-{i}-name", "strip_purpose": f"purpose {i}",
                                "strip_expect": f"expect {i}", "deadline_utc": "2026-10-14T23:40:00Z",
                                "respond_hint": "!pass / !fail / !skip"}})
    for sid, vd, src in (("s1", "pass", "chat"), ("s2", "fail", "web"), ("s3", "skip", "console")):
        events.append({"id": 30 + int(sid[1]), "level": "milestone", "phase": None, "message": f"verdict {vd}",
                       "data": {"verdict": vd, "source": src, "notes": f"note {sid}", "step": sid}})
    if current_verdict:
        events.append({"id": 99, "level": "milestone", "phase": None, "message": "verdict fail",
                       "data": {"verdict": "fail", "source": "chat", "notes": "camera stuck", "step": "s4"}})
    if done:
        events.append({"id": 100, "level": "info", "phase": "done", "message": "run complete",
                       "data": {"results": [{"id": f"s{i}", "verdict": "pass", "source": "chat", "notes": None}
                                            for i in range(1, 7)]}})
    cs = None
    if status == "running":
        cs = {"phase": "s4", "step_index": 4, "step_total": 6, "prompt": "prompt 4", "look_for": "look 4",
              "deadline_utc": "2026-10-14T23:40:00Z", "asked_at": "2026-10-14T23:38:00Z",
              "respond_hint": "!pass / !fail / !skip"}
    return {"id": "j-vux", "status": status, "current_step": cs, "events": events}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_verification_render_executes_in_node(client):
    """Lift the shipped verification renderers out of app.js and run them against a synthetic job:
    the six chip states, the current test's card (TEST k of n + the bar), the recorded+waiting state,
    and the finished 'Run complete' results table."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    block = "const VERDICT_SET" + js.split("const VERDICT_SET", 1)[1].split("function jobDetail", 1)[0]
    prologue = (
        'const ESC_MAP = {"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"};\n'
        'const esc = (v) => String(v ?? "").replace(/[&<>"\']/g, (c) => ESC_MAP[c]);\n'
        'const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;\n'
    )
    epilogue = (
        "const fmtTime = (iso) => String(iso ?? '—');\n"
        "const states = (j) => jobVerifications(j).map((x) => [x.id, x.state]);\n"
        "console.log(JSON.stringify({\n"
        "  runningStates: states(RUN),\n"
        "  stepper: jobStepper(RUN),\n"
        "  card: nowTestingCard(RUN),\n"
        "  recordedCard: nowTestingCard(REC),\n"
        "  curStates: states(CUR),\n"
        "  curStepper: jobStepper(CUR),\n"
        "  curCard: nowTestingCard(CUR),\n"
        "  doneStates: states(DONE),\n"
        "  results: jobNow(DONE) + jobResultsTable(DONE),\n"
        "}));\n"
    )
    # a webapp verdict POSTs without a `step`, so its milestone has no phase: it must still land on the
    # test that was current when it arrived (the newest prompt before it).
    cur = _vx_fixture("running")
    cur["events"].append({"id": 250, "level": "milestone", "phase": None, "message": "verdict fail (web): stuck",
                          "data": {"verdict": "fail", "source": "web", "notes": "stuck", "step": None}})
    script = (
        prologue
        + block
        + "\nconst RUN = " + json.dumps(_vx_fixture("running")) + ";\n"
        + "const REC = " + json.dumps(_vx_fixture("running", current_verdict=True)) + ";\n"
        + "const CUR = " + json.dumps(cur) + ";\n"
        + "const DONE = " + json.dumps(_vx_fixture("failed", done=True)) + ";\n"
        + epilogue
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # the six chip states: done colours, the current test, the untouched upcoming ones
    assert out["runningStates"] == [
        ["s1", "pass"], ["s2", "fail"], ["s3", "skip"], ["s4", "current"], ["s5", "upcoming"], ["s6", "upcoming"],
    ]
    # the stepper carries the state classes, the verdict + its source, and the current pulse
    assert "vx-chip vx-pass" in out["stepper"] and "vx-chip vx-fail" in out["stepper"]
    assert "vx-chip vx-skip" in out["stepper"] and "vx-testing vx-current" in out["stepper"]
    assert 'data-vx-goto="s4"' in out["stepper"] and "via chat" in out["stepper"] and "vx-upcoming" in out["stepper"]
    # the card shows the current test big: TEST k of n, both explainer fields, and a countdown bar
    assert "TEST 4 of 6" in out["card"] and 'data-vx-step="s4"' in out["card"]
    assert "What is tested" in out["card"] and "purpose 4" in out["card"]
    assert "What you should see" in out["card"] and "expect 4" in out["card"]
    assert "vx-bar-fill" in out["card"] and "!pass / !fail / !skip" in out["card"]
    assert "Recorded:" not in out["card"]  # nothing recorded for the current test yet → no waiting state
    # a verdict already on the current test renders the recorded + waiting state (from any source)
    assert "Recorded: FAIL" in out["recordedCard"] and "(via chat)" in out["recordedCard"]
    assert "camera stuck" in out["recordedCard"]
    assert "Waiting for the demo to move to the next test…" in out["recordedCard"]
    # a web verdict (no step tag) still lands on the test that was current when it arrived
    assert out["curStates"][3] == ["s4", "fail"]
    assert "vx-fail" in out["curStepper"] and "vx-current" in out["curStepper"] and "via web" in out["curStepper"]
    assert "Recorded: FAIL" in out["curCard"] and "(via web)" in out["curCard"]
    # a finished job renders a 'Run …' banner with the counts and the per-step results table
    assert out["doneStates"] == [["s1", "pass"], ["s2", "fail"], ["s3", "skip"],
                                 ["s4", "pass"], ["s5", "pass"], ["s6", "pass"]]
    assert "Run failed" in out["results"] and "4</strong> pass" in out["results"]
    assert "vx-rtable" in out["results"] and "via chat" in out["results"]


# ------------------------------------------ Activity page: the unified timeline
# The user ask: the Activity page was hard to read IN SEQUENCE — the three types were split into
# separate lists and a reaped run jumped to the top via a late updated_at touch an hour after it
# died. The fix is ONE chronological timeline (sorted by each run's OWN start, never updated_at),
# with consecutive same-job rows nested, an honest 'reaped' tag, and the by-host grouping kept below.
def test_frontend_activity_timeline_markers(client):
    """One timeline replaces the three Full-history lists; the sorts/merges/nesting and the reaped tag
    exist in the shipped app.js, and the by-host grouping stays."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    # the timeline helper block, marked like the other liftable blocks
    assert "activity-timeline:start" in js and "activity-timeline:end" in js
    # sorted by each run's OWN start — NOT updated_at (the reaped-run jump-in bug)
    assert "const activityStart = (r) => r.started_at || r.created_at" in js
    assert "const activityRows" in js and "const activityChains" in js and "const activityTimelineHtml" in js
    assert "const activityRowHtml" in js and "const isReaped" in js and "const reapedTag" in js
    # all three feeds merged, and job linkage resolved from the job ledger (a run row carries no job id)
    assert 'api("/jobs", { limit: 200 })' in js
    assert "own(`build:${b.id}`)" in js and "own(`test_run:${t.id}`)" in js and "own(`deploy:${d.id}`)" in js
    # the owning job's status rides along into the row, so the chain heading can badge it
    assert "statusById" in js and "c.rows[0].jobStatus" in js
    # a row carries time | type chip | state | name | duration | host | job
    assert "<th>Job</th>" in js and '"tl-row tl-${r.type}"' in js
    # job nesting: a heading + indented rows for 2+ consecutive same-job rows
    assert "tl-job-head" in js and "tl-nested" in js and "activityChains(rows)" in js
    # the three old Full-history lists are GONE; the by-host grouping stays
    assert '"Full history"' not in js
    assert "By host" in js
    # the honest reaped tag replaces the bare idle badge, and its tooltip keeps the note
    assert 'badge("reaped", "muted reaped")' not in js  # the tag is built inline with a title tooltip
    assert 'class="badge muted reaped" title=' in js and "REAPED_RE" in js
    # the CSS the timeline relies on
    assert "table.timeline" in css and ".tl-nested" in css and ".badge.reaped" in css
    assert ".badge.tl-build" in css and ".badge.tl-test" in css and ".badge.tl-deploy" in css


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_activity_timeline_executes_in_node(client):
    """Lift the shipped timeline helpers out of app.js and run them on three feeds + a job ledger:
    all three types merge into one list sorted by started_at (so a reaped run with a NEWER updated_at
    stays at the bottom), the reaped row shows the tag instead of 'idle', and three consecutive
    same-job rows nest under one heading."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    dur = js.split("// duration-helpers:start", 1)[1].split("// duration-helpers:end", 1)[0]
    block = js.split("// activity-timeline:start", 1)[1].split("// activity-timeline:end", 1)[0]
    # the shipped status-class map + badge (the timeline's chain heading uses them) — lifted, not stubbed
    status_block = "const JOB_STATUS_CLASS" + js.split("const JOB_STATUS_CLASS", 1)[1].split("const jobFlags", 1)[0]
    prologue = """const ESC_MAP = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const enc = encodeURIComponent;
const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;
const firstLine = (s) => (s || "").split(String.fromCharCode(10))[0];
const stateBadge = (s) => badge(s, "");
const lockStuckBadge = () => "";
const fmtTime = (iso) => String(iso ?? "—");
const link = (parts, label) => `<a href="#/${parts.map(enc).join("/")}">${esc(label)}</a>`;
""" + status_block
    # builds on desktop-pc start at 10:00; the deploy at 10:08, the live test at 10:05; a REAPED test
    # started at 09:00 (oldest) but its updated_at is 10:30 (newest) — the late reaper touch that used
    # to float it to the top. A job links the three newest runs (deploy/test/build), so they nest.
    builds = [{"id": "b-1", "host_id": "desktop-pc", "kind": "compile", "status": "succeeded",
               "created_at": "2026-10-13T10:00:00Z", "updated_at": "2026-10-13T10:02:00Z"}]
    tests = [
        {"id": "t-1", "host_id": "desktop-pc", "name": "iso_tunnel", "state": "pass",
         "started_at": "2026-10-13T10:05:00Z", "created_at": "2026-10-13T10:05:00Z",
         "updated_at": "2026-10-13T10:07:00Z", "elapsed_sec": 120},
        {"id": "t-reap", "host_id": "desktop-pc", "name": "iso_dead", "state": "idle",
         "note": "reaped: lease lost", "started_at": "2026-10-13T09:00:00Z",
         "created_at": "2026-10-13T09:00:00Z", "finished_at": "2026-10-13T10:30:00Z",
         "updated_at": "2026-10-13T10:30:00Z", "elapsed_sec": 0},
    ]
    deploys = [{"id": "d-1", "host_id": "desktop-pc", "addon": None, "status": "verified",
                "created_at": "2026-10-13T10:08:00Z", "updated_at": "2026-10-13T10:08:30Z", "entries": []}]
    jobs = [{"id": "j-a", "status": "inconclusive", "links": [{"kind": "build", "ref_id": "b-1"},
                                    {"kind": "test_run", "ref_id": "t-1"},
                                    {"kind": "deploy", "ref_id": "d-1"}]}]
    script = (
        prologue + dur + block
        + "\nconst B = " + json.dumps(builds) + ";\n"
        + "const T = " + json.dumps(tests) + ";\n"
        + "const D = " + json.dumps(deploys) + ";\n"
        + "const J = " + json.dumps(jobs) + ";\n"
        + "const rows = activityRows(B, T, D, J);\n"
        + "const HTML = activityTimelineHtml(rows);\n"
        + "const marks = [['compile','b-1'],['iso_tunnel','t-1'],['d-1','d-1'],['iso_dead','t-reap']];\n"
        + "const order = marks.map(([m,k]) => [HTML.indexOf(m), k]).sort((a,b) => a[0]-b[0]).map((x) => x[1]);\n"
        + "const ri = HTML.indexOf('iso_dead');\n"
        + "const reapedRow = HTML.slice(HTML.lastIndexOf('<tr class=\"tl-row', ri), HTML.indexOf('</tr>', ri)+5);\n"
        + "const chain = (HTML.match(/tl-chain\">([^<]*)</) || [])[1] || null;\n"
        + "const hi = HTML.indexOf('tl-job-head');\n"
        + "const head = hi < 0 ? '' : HTML.slice(HTML.lastIndexOf('<tr', hi), HTML.indexOf('</tr>', hi) + 5);\n"
        + "console.log(JSON.stringify({order, types: rows.map((r) => r.type), jobs: rows.map((r) => r.job),"
        + " reapedRow, chain, head, nested: (HTML.match(/tl-nested/g) || []).length,"
        + " heads: (HTML.match(/tl-job-head/g) || []).length,"
        + " jobLinks: (HTML.match(/#\\/jobs\\/j-a/g) || []).length}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # merged + newest-first by STARTED_AT: deploy 10:08, test 10:05, build 10:00 — and the reaped run,
    # despite the newest updated_at (10:30), sits LAST because it STARTED at 09:00.
    assert out["order"] == ["d-1", "t-1", "b-1", "t-reap"]
    assert sorted(out["types"]) == ["build", "deploy", "test", "test"]
    assert out["jobs"] == ["j-a", "j-a", "j-a", None]
    # three consecutive same-job rows nest under ONE heading, the heading names the top-down chain
    # 3 nested rows + the one job heading = 4 links to the job
    assert out["heads"] == 1 and out["nested"] == 3 and out["jobLinks"] == 4
    assert out["chain"] == "deploy → test → build"
    # the heading also carries the job's own status badge, so an 'inconclusive' chain (a run that
    # ended with no verdict) is readable from the timeline instead of looking like any other job
    assert 'class="badge inconclusive">inconclusive<' in out["head"]
    assert out["head"].count("#/jobs/j-a") == 1
    # the reaped row is honest: a muted 'reaped' tag, NOT a bare 'idle' badge, and no job link ('—')
    assert "reaped" in out["reapedRow"] and ">idle<" not in out["reapedRow"]
    assert "reaped: lease lost" in out["reapedRow"]  # the note rides in the tooltip
    assert "—" in out["reapedRow"] and "#/jobs/" not in out["reapedRow"]


# ------------------------------------------- Job status: the 'inconclusive' badge
# The ledger used to finish a no-verdict run 'passed' (pass=0/fail=0/no-verdict=1). The webapp's job
# status badge now gives that run its own class, so it can never be read as a pass at a glance.
@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_job_status_badges_execute_in_node(client):
    """Lift the shipped status-class map + badge and render every job status: the amber 'kind' of a pass
    and the red 'warn' of a failure are untouched, and 'inconclusive' gets its own class."""
    assert NODE is not None  # skipif above
    js = client.get("/app.js").text
    block = "const JOB_STATUS_CLASS" + js.split("const JOB_STATUS_CLASS", 1)[1].split("const jobFlags", 1)[0]
    prologue = """const ESC_MAP = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;
"""
    epilogue = (
        'const ALL = ["queued","running","passed","failed","error","cancelled","inconclusive"];\n'
        "console.log(JSON.stringify(Object.fromEntries(ALL.map((s) => [s, jobStatusBadge(s)]))));\n"
    )
    proc = subprocess.run(
        [NODE, "-e", prologue + block + epilogue], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert set(out) == {"queued", "running", "passed", "failed", "error", "cancelled", "inconclusive"}
    assert out["passed"] == '<span class="badge kind">passed</span>'
    assert out["failed"] == '<span class="badge warn">failed</span>'
    assert out["inconclusive"] == '<span class="badge inconclusive">inconclusive</span>'
    # the distinction that matters: an unjudged run reads as neither a pass nor a failure
    assert "kind" not in out["inconclusive"] and "warn" not in out["inconclusive"]


def test_overview_wallboard_invariants(client):
    """Overview wallboard: single .ovw-now-wrap cell in .ovw-top so concurrent active runs never
    spill into the Machine Dock column; ovwLockStrip joins HostLock.fresh so RunLockSummary never
    falsely renders STALE; and shortAgo omits trailing zero units."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    assert "async overview()" in js
    assert "ovw-now-wrap" in js and "ovw-dockpanel" in js and "ovw-main" in js
    assert "ovwLockStrip" in js and "onceLockStrip" in js
    assert "lockCell(t.lock)" not in js  # RunLockSummary omits `fresh` — must go through ovwLockStrip
    assert ".ovw-top" in css and ".ovw-main" in css and ".ovw-tile" in css
    assert "grid-auto-rows: 64px" in css


# --------------------------- Job page: live phase banners + static caching (j-336087bd)
# The user ask on a live demo: "i am not seeing latest ui updates on this test". Two causes, both fixed:
#   (1) a browser kept serving a CACHED app.js/style.css after the deploy — StaticFiles sends no cache
#       headers. Fix: Cache-Control: no-cache on the shell + its two assets, and a ?v= cache-buster in
#       index.html.
#   (2) the job page knew only the SETTING UP / CLEANING UP phases, so the driver's READY, WATCH NOW
#       (the OPEN verdict window), GOT and live per-step `action` events never reached the screen. Fix:
#       jobActivePhase() covers every live phase, vxPhaseBanner() renders each, the setup card resolves
#       the step being staged from the plan (so it switches the moment staging starts), and the card
#       shows the live 'Now in game' action line. The Pass/Fail/Skip buttons stay ENABLED during WATCH.
def test_static_shell_and_assets_revalidate(client):
    """The SPA shell and its two assets carry ``Cache-Control: no-cache`` and the shell cache-busts them
    with a ``?v=`` query param, so an open tab picks up a deploy on its next load; an API response is
    left alone."""
    for path in ("/", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
        assert 'href="/style.css?v=' in r.text and 'src="/app.js?v=' in r.text, path
    for path in ("/app.js", "/style.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
    assert client.get("/api/health").headers.get("cache-control") is None  # not dragged into the set


def test_live_phase_markers_in_app_js(client):
    """The live-phase vocabulary exists in the shipped app.js / style.css."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    assert 'const LIVE_PHASES = new Set(["ready", "setup", "watch", "got", "cleanup"])' in js
    assert "WATCH NOW" in js and 'data-vx-phase="watch"' in js
    assert 'data-vx-phase="got"' in js and "d.banner" in js and "vx-got-" in js
    assert 'data-vx-phase="ready"' in js
    assert "function vxStepInfo(" in js and "function vxPhaseUntil(" in js
    assert "Now in game" in js and "vx-action" in js
    assert "Setting up…" in js and "vx-staging" in js
    assert "reply any time" in js and "Verdict window open" in js
    for sel in (".vx-phase-watch", ".vx-phase-got", ".vx-phase-ready", ".vx-staging", ".vx-action", ".vx-window-open"):
        assert sel in css, sel


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_live_phase_banners_and_action_execute_in_node(client):
    """Lift the shipped renderers and run the real j-336087bd event shapes through them: READY, SETTING
    UP (title resolved from the plan), WATCH NOW (window OPEN, buttons enabled), a live `action` line,
    GOT: PASS, CLEANING UP, then the NEXT step's setup switching the card before its prompt fires."""
    assert NODE is not None
    js = client.get("/app.js").text
    block = "const VERDICT_SET" + js.split("const VERDICT_SET", 1)[1].split("function jobDetail", 1)[0]
    prologue = (
        'const esc = (v) => String(v ?? "");\n'
        'const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;\n'
        'const fmtTime = (iso) => String(iso ?? "—");\n'
        "const parseTs = (iso) => Date.parse(String(iso));\n"
        'const CT_ZONE = "America/Chicago";\n'
    )
    plan = [
        {"id": "s3", "index": 1, "title": "Ring sweep", "strip_name": "RING SWEEP",
         "strip_purpose": "sweep the ring to find the bot", "strip_expect": "the bot in frame"},
        {"id": "s4", "index": 2, "title": "Area presets", "strip_name": "AREA PRESETS",
         "strip_purpose": "cycle the area cameras", "strip_expect": "the presets apply"},
    ]
    ev0 = {"id": 1, "level": "info", "phase": "plan", "message": "plan posted", "data": {"plan": plan}}
    ready = {"id": 2, "level": "info", "phase": "ready",
             "message": "READY -- first test in 8 s -- keep the game up",
             "created_at": "2026-10-16T02:33:40Z",
             "data": {"countdown_s": 8.0, "job_url": "http://10.88.15.208:8812/#/jobs/j-live"}}
    setup1 = {"id": 3, "level": "info", "phase": "setup",
              "message": "SETTING UP TEST 1 of 2 ring sweep (10 s)",
              "data": {"countdown_s": 10.0, "until": "2026-10-16T02:33:48Z", "step": "s3", "next": ""}}
    p1 = {"id": 4, "level": "info", "phase": "s3", "message": "STEP 1/2",
          "data": {"prompt": "ring sweep", "look_for": "bot", "step_index": 1, "step_total": 2,
                   "strip_name": "RING SWEEP", "strip_purpose": "sweep the ring to find the bot",
                   "strip_expect": "the bot in frame", "deadline_utc": "2026-10-16T02:37:03Z",
                   "respond_hint": "!pass / !fail / !skip"}}
    watch = {"id": 5, "level": "info", "phase": "watch", "message": "WATCH NOW -- reply any time (3 min)",
             "data": {"countdown_s": 180.0, "until": "2026-10-16T02:37:03Z", "step": "s3"}}
    action = {"id": 6, "level": "info", "phase": "s3",
              "message": "s3 action: now: camera sweeping to find the bot",
              "data": {"action": "now: camera sweeping to find the bot", "step": "s3"}}
    got = {"id": 7, "level": "info", "phase": "got", "message": "GOT: PASS", "data": {"banner": "GOT: PASS"}}
    cleanup = {"id": 8, "level": "info", "phase": "cleanup",
               "message": "CLEANING UP TEST 1/2 ring sweep (10 s)",
               "data": {"countdown_s": 10.0, "until": "2026-10-16T02:37:20Z", "step": "s3",
                        "next": "next: TEST 2 of 2: AREA PRESETS"}}
    setup2 = {"id": 9, "level": "info", "phase": "setup",
              "message": "SETTING UP TEST 2 of 2 AREA PRESETS (10 s)",
              "data": {"countdown_s": 10.0, "until": "2026-10-16T02:37:30Z", "step": "s4"}}
    cs = {"phase": "s3", "step_index": 1, "step_total": 2, "prompt": "ring sweep", "look_for": "bot",
          "deadline_utc": "2026-10-16T02:37:03Z", "asked_at": "2026-10-16T02:33:50Z",
          "respond_hint": "!pass / !fail / !skip"}

    def job(events):
        return {"id": "j-live", "status": "running", "current_step": cs, "events": events}

    stages = {
        "JREADY": job([ev0, ready]),
        "JSETUP1": job([ev0, ready, setup1]),
        "JWATCH": job([ev0, ready, setup1, p1, watch]),
        "JACTION": job([ev0, ready, setup1, p1, watch, action]),
        "JGOT": job([ev0, ready, setup1, p1, watch, action, got]),
        "JCLEAN": job([ev0, ready, setup1, p1, watch, action, got, cleanup]),
        "JSETUP2": job([ev0, ready, setup1, p1, watch, action, got, cleanup, setup2]),
    }
    script = (
        prologue
        + block
        + "".join(f"\nconst {k} = " + json.dumps(v) + ";" for k, v in stages.items())
        + "\nconst ph = (j) => (jobActivePhase(j) || {}).phase;\n"
        + "console.log(JSON.stringify({\n"
        + "  phase: Object.fromEntries(Object.entries({JREADY,JSETUP1,JWATCH,JACTION,JGOT,JCLEAN,JSETUP2})"
        + ".map(([k,j]) => [k, ph(j)])),\n"
        + "  readyBanner: vxPhaseBanner(JREADY),\n"
        + "  setup1Banner: vxPhaseBanner(JSETUP1),\n"
        + "  watchBanner: vxPhaseBanner(JWATCH),\n"
        + "  actionWatchBanner: vxPhaseBanner(JACTION),\n"
        + "  gotBanner: vxPhaseBanner(JGOT),\n"
        + "  cleanBanner: vxPhaseBanner(JCLEAN),\n"
        + "  setup2Banner: vxPhaseBanner(JSETUP2),\n"
        + "  setup1Card: nowTestingCard(JSETUP1),\n"
        + "  watchCard: nowTestingCard(JWATCH),\n"
        + "  actionCard: nowTestingCard(JACTION),\n"
        + "  gotCard: nowTestingCard(JGOT),\n"
        + "  cleanCard: nowTestingCard(JCLEAN),\n"
        + "  setup2Card: nowTestingCard(JSETUP2),\n"
        + "}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # every live phase is recognised as the run's ACTIVE phase; the `action` event does NOT close WATCH
    assert out["phase"] == {
        "JREADY": "ready", "JSETUP1": "setup", "JWATCH": "watch", "JACTION": "watch",
        "JGOT": "got", "JCLEAN": "cleanup", "JSETUP2": "setup",
    }
    # READY: a banner with a countdown derived from the event's own timestamp (no `until` in the payload)
    assert "READY" in out["readyBanner"] and "first test in 8 s" in out["readyBanner"]
    assert "02:33:48" in out["readyBanner"], out["readyBanner"]
    # SETTING UP: the title is RESOLVED FROM THE PLAN by `data.step` (the event carries no name/index)
    assert "SETTING UP" in out["setup1Banner"]
    assert "TEST 1/2" in out["setup1Banner"] and "RING SWEEP" in out["setup1Banner"]
    assert 'data-cd-sec="2026-10-16T02:33:48Z"' in out["setup1Banner"]
    # WATCH NOW: the open verdict window, its test name and the 3-minute countdown
    assert "WATCH NOW" in out["watchBanner"] and "reply any time" in out["watchBanner"]
    assert "TEST 1/2" in out["watchBanner"] and "RING SWEEP" in out["watchBanner"]
    assert 'data-cd-sec="2026-10-16T02:37:03Z"' in out["watchBanner"]
    # GOT: the driver's own banner text, coloured by outcome
    assert "GOT" in out["gotBanner"] and "PASS" in out["gotBanner"]
    assert 'data-vx-banner="GOT: PASS"' in out["gotBanner"] and "vx-got-ok" in out["gotBanner"]
    # CLEANING UP: the next-test line and the (plan-resolved) test name
    assert "CLEANING UP" in out["cleanBanner"] and "TEST 1/2" in out["cleanBanner"]
    assert "→ next: next: TEST 2 of 2: AREA PRESETS" in out["cleanBanner"]
    # the SECOND test's setup banner resolves from the plan too
    assert "SETTING UP" in out["setup2Banner"] and "TEST 2/2" in out["setup2Banner"]
    assert "AREA PRESETS" in out["setup2Banner"]
    # the card switches to the step being STAGED before its prompt fires (s4 while current_step is s3)
    assert 'data-vx-step="s4"' in out["setup2Card"]
    assert "TEST 2 of 2" in out["setup2Card"] and "AREA PRESETS" in out["setup2Card"]
    assert "Setting up…" in out["setup2Card"] and "vx-staging" in out["setup2Card"]
    assert "Recorded:" not in out["setup2Card"]  # the PREVIOUS test's verdict must not follow it
    assert "disabled" in out["setup2Card"] and "opens in" in out["setup2Card"]
    # ...and the step being staged for the FIRST test resolves too
    assert 'data-vx-step="s3"' in out["setup1Card"] and "TEST 1 of 2" in out["setup1Card"]
    assert "RING SWEEP" in out["setup1Card"] and "opens in" in out["setup1Card"]
    # WATCH: the window is OPEN -> the buttons are ENABLED (no disabled attribute anywhere in the card)
    assert "disabled" not in out["watchCard"], out["watchCard"]
    assert "reply any time" in out["watchCard"] and "vx-window-open" in out["watchCard"]
    # ...and the live in-game action line is shown on the card
    assert "Now in game" in out["actionCard"]
    assert "now: camera sweeping to find the bot" in out["actionCard"]
    assert "disabled" not in out["actionCard"]
    # the `action` event did not close the WATCH banner
    assert "WATCH NOW" in out["actionWatchBanner"]
    # GOT leaves the buttons enabled too; CLEANING UP closes them with 'window closed'
    assert "disabled" not in out["gotCard"]
    assert "disabled" in out["cleanCard"] and "window closed" in out["cleanCard"]

