"""web_url everywhere + the job-screenshots endpoint.

Covers the contract: every list/single REST row for a job, test run, build, deploy and project
carries an absolute `web_url` (request-derived origin), GET /api/health carries `public_url`, the
S2DOCS_PUBLIC_URL setting overrides the derived origin, and GET /api/jobs/{id}/screenshots returns
screenshots tied to their step/verdict (new event fields) while old events read 'unassigned'.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import hosts, jobs, test_runs, weburls
from s2docs.api.app import create_app
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server

INTAKE = {
    "kind": "test",
    "title": "web url probe",
    "target": "ws/web-url@abc123",
    "purpose": "check web_url",
    "expected": "a URL is present",
}


@pytest.fixture
def wdb(tmp_path) -> Database:
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    return db


@pytest.fixture
def client(wdb):
    with TestClient(create_app(wdb.path)) as c:
        yield c


def _enq(client, **over):
    return client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", **over})


# ------------------------------------------------------------------- weburls
def test_public_base_url_precedence(monkeypatch):
    """The setting wins; else the request's scheme://host; else the LAN fallback."""
    get_settings.cache_clear()
    # no setting, no request -> LAN fallback
    assert weburls.public_base_url(None) == "http://10.88.15.208:8812"

    class _Req:
        headers = {"host": "10.88.15.208:8812"}

        class url:  # noqa: N801 - mimic the starlette request.url.scheme access
            scheme = "http"

    assert weburls.public_base_url(_Req()) == "http://10.88.15.208:8812"

    monkeypatch.setenv("S2DOCS_PUBLIC_URL", "https://docs.example.com")
    get_settings.cache_clear()
    try:
        # the setting wins even when a request is present (trailing slash stripped)
        assert weburls.public_base_url(_Req()) == "https://docs.example.com"
        assert weburls.public_base_url(None) == "https://docs.example.com"
    finally:
        monkeypatch.delenv("S2DOCS_PUBLIC_URL", raising=False)
        get_settings.cache_clear()


def test_weburls_route_shapes():
    base = "http://host:8812"
    assert weburls.job_url(base, "j-1") == "http://host:8812/#/jobs/j-1"
    assert weburls.project_url(base, "cs2", "p-1") == "http://host:8812/#/cs2/projects/p-1"
    # runs/builds/deploys have no dedicated page: they point at Activity
    assert weburls.test_run_url(base, "t-1") == "http://host:8812/#/activity"
    assert weburls.build_url(base, "b-1") == "http://host:8812/#/activity"
    assert weburls.deploy_url(base, "d-1") == "http://host:8812/#/activity"
    assert weburls.artifact_url(base, "j-1", "a.png") == "http://host:8812/api/artifacts/j-1/a.png"


# --------------------------------------------------------------------- health
def test_health_reports_request_derived_public_url(client):
    info = client.get("/api/health").json()
    assert info["public_url"] == "http://testserver"  # TestClient origin, no setting
    assert info["db_path"]  # the DB-identity fields are untouched


def test_public_url_setting_overrides_request(monkeypatch, wdb):
    monkeypatch.setenv("S2DOCS_PUBLIC_URL", "http://10.88.15.208:8812/")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(wdb.path)) as c:
            assert c.get("/api/health").json()["public_url"] == "http://10.88.15.208:8812"
            jid = c.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc"}).json()["id"]
            assert c.get(f"/api/jobs/{jid}").json()["web_url"] == f"http://10.88.15.208:8812/#/jobs/{jid}"
    finally:
        monkeypatch.delenv("S2DOCS_PUBLIC_URL", raising=False)
        get_settings.cache_clear()


# ----------------------------------------------------------------- jobs / lists
def test_job_web_url_single_and_list(client):
    jid = _enq(client).json()["id"]
    assert client.get(f"/api/jobs/{jid}").json()["web_url"] == f"http://testserver/#/jobs/{jid}"
    row = next(j for j in client.get("/api/jobs").json() if j["id"] == jid)
    assert row["web_url"] == f"http://testserver/#/jobs/{jid}"


def test_test_run_web_url(client):
    rid = client.post("/api/test-runs", json={"name": "iso_web"}).json()["id"]
    assert client.get(f"/api/test-runs/{rid}").json()["web_url"] == "http://testserver/#/activity"
    assert client.get("/api/test-runs").json()[0]["web_url"] == "http://testserver/#/activity"


def test_build_and_deploy_web_url(wdb, client):
    with wdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, created_at, updated_at) "
            "VALUES ('b-1', 'desktop-pc', 'compile', 'x', 'running', 'build:b-1', "
            "'2026-10-15T10:00:00Z', '2026-10-15T10:00:00Z')"
        )
        conn.execute(
            "INSERT INTO deployments (id, host_id, dest_root, entries, status, created_at, updated_at) "
            "VALUES ('d-1', 'desktop-pc', 'C:/dst', '[]', 'verified', '2026-10-15T10:00:00Z', '2026-10-15T10:00:30Z')"
        )
    assert client.get("/api/builds/b-1").json()["web_url"] == "http://testserver/#/activity"
    assert client.get("/api/builds").json()[0]["web_url"] == "http://testserver/#/activity"
    assert client.get("/api/deploys/d-1").json()["web_url"] == "http://testserver/#/activity"
    assert client.get("/api/deploys").json()[0]["web_url"] == "http://testserver/#/activity"


def test_project_web_url(client):
    pid = client.post("/api/projects", json={"workflow": "cs2_camera_addon", "goal": "iso goal", "game": "cs2"}).json()[
        "project_id"
    ]
    assert client.get(f"/api/projects/{pid}").json()["web_url"] == f"http://testserver/#/cs2/projects/{pid}"
    row = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert row["web_url"] == f"http://testserver/#/cs2/projects/{pid}"


# ----------------------------------------------------------------- screenshots
def _shot(client, jid, **data):
    return client.post(
        f"/api/jobs/{jid}/event",
        json={
            "level": "screenshot",
            "message": f"shot {data.get('seq', '')}".strip(),
            "phase": data.get("step"),
            "data": data,
        },
    )


def test_screenshots_endpoint_ties_step_and_verdict(client):
    jid = _enq(client).json()["id"]
    _shot(
        client,
        jid,
        url=f"/api/artifacts/{jid}/ready.png",
        md5="aa",
        bytes=100,
        step="s1",
        step_index=1,
        step_total=2,
        reason="ready",
        seq=1,
        full_res=False,
        width=640,
        height=360,
    )
    _shot(
        client,
        jid,
        url=f"/api/artifacts/{jid}/verdict.png",
        md5="bb",
        bytes=200,
        step="s1",
        step_index=1,
        step_total=2,
        reason="verdict",
        seq=2,
        verdict={"verdict": "pass", "source": "chat", "confirm_id": "e-1", "late": False},
        full_res=True,
        width=1920,
        height=1080,
    )
    _shot(client, jid, url=f"/api/artifacts/{jid}/old.jpg", md5="cc", bytes=50)  # no new fields

    shots = client.get(f"/api/jobs/{jid}/screenshots").json()
    assert [s["seq"] for s in shots] == [1, 2, 3]
    assert [s["reason"] for s in shots] == ["ready", "verdict", None]
    assert [s["step"] for s in shots] == ["s1", "s1", None]  # the old event is unassigned
    v = shots[1]
    assert (
        v["verdict"]["verdict"] == "pass" and v["verdict"]["source"] == "chat" and v["verdict"]["confirm_id"] == "e-1"
    )
    assert v["full_res"] is True and v["width"] == 1920 and v["height"] == 1080
    for s in shots:
        assert s["web_url"] == f"http://testserver/#/jobs/{jid}"
        assert s["name"] and s["url"]
    # filters
    assert [s["seq"] for s in client.get(f"/api/jobs/{jid}/screenshots", params={"step": "s1"}).json()] == [1, 2]
    assert [s["seq"] for s in client.get(f"/api/jobs/{jid}/screenshots", params={"reason": "verdict"}).json()] == [2]
    assert [s["seq"] for s in client.get(f"/api/jobs/{jid}/screenshots", params={"step": "unassigned"}).json()] == [3]
    # a bogus reason is a 422 (JobValidationError)
    assert client.get(f"/api/jobs/{jid}/screenshots", params={"reason": "nope"}).status_code == 422
    # unknown job -> 404
    assert client.get("/api/jobs/j-ghost/screenshots").status_code == 404


def test_screenshots_service_derives_seq_and_skips_urlless(client):
    jid = _enq(client).json()["id"]
    _shot(client, jid, url=f"/api/artifacts/{jid}/a.png")  # no seq -> positional 1
    _shot(client, jid, message="no url")  # dropped (no url)
    _shot(client, jid, url=f"/api/artifacts/{jid}/b.png")  # -> 2
    shots = client.get(f"/api/jobs/{jid}/screenshots").json()
    assert [s["seq"] for s in shots] == [1, 2]
    assert [s["name"] for s in shots] == ["a.png", "b.png"]


# ----------------------------------------------------------------------- MCP
def test_mcp_job_get_carries_web_url_and_screenshots(wdb):
    jid = jobs.enqueue(
        wdb, kind="test", title="mcp url", target="ws/x@1", purpose="p", expected="e", host_id="desktop-pc"
    ).id
    run = test_runs.start(wdb, "iso_mcp")
    jobs.link(wdb, jid, kind="test_run", ref_id=run.id)
    jobs.event(
        wdb,
        jid,
        level="screenshot",
        message="shot",
        data={"url": f"/api/artifacts/{jid}/s.png", "step": "s2", "reason": "request", "seq": 1},
    )
    server = create_mcp_server(wdb)

    async def main():
        async with Client(server) as c:
            got = await c.call_tool("job_get", {"job_id": jid})
            shots = await c.call_tool("job_screenshots", {"job_id": jid})
            return got, shots

    got, shots = anyio.run(main)
    # MCP has no request: the URL is the setting/LAN fallback, still absolute and usable
    assert got.structured_content["web_url"] == f"http://10.88.15.208:8812/#/jobs/{jid}"
    rows = shots.structured_content["result"]
    assert len(rows) == 1 and rows[0]["step"] == "s2" and rows[0]["reason"] == "request"
    assert rows[0]["web_url"] == f"http://10.88.15.208:8812/#/jobs/{jid}"
