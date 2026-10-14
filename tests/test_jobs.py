"""Test jobs: the ledger + queue, its REST/MCP/CLI facades and the web page.

Synthetic DB, no host/network: jobs are DB rows plus files under the data dir, and the artifact
upload is a raw-body PUT. These pin the plan's contract (docs/TEST_JOBS_PLAN.md): the intake is the
gate (one 422 with a field list), queue order is per host with a lease check on `next`, an event's
`phase` moves the job's phase, artifacts are append-only with a size/name/content-type guard, images
auto-add a screenshot event, a screenshot request clears on upload, and the close-out summary gate
rejects any missing/oversized field. Also: the push jsonl + health field, MCP registration + one
end-to-end round trip, CLI exit codes, and the web route.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import hosts, jobs
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound
from s2docs.mcp_server import create_mcp_server

INTAKE = {
    "kind": "test",
    "title": "occluder wire",
    "target": "ws/occluder-wire@abc123",
    "purpose": "verify the wire is occluded",
    "expected": "the wire is hidden behind the wall",
}


def _png(w: int = 3, h: int = 2, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """A tiny real PNG, built with only zlib/struct (no Pillow) — a valid file for byte compare + md5."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def jdb(tmp_path) -> Database:
    # The same path the CLI and settings resolve (conftest points S2DOCS_DATA_DIR at tmp_path/data).
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    return db


@pytest.fixture
def client(jdb):
    with TestClient(create_app(jdb.path)) as c:
        yield c


def _enq(client, **over):
    return client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", **over})


def _lines() -> list[dict]:
    path = jobs.job_events_jsonl_path()
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------- intake
@pytest.mark.parametrize("field", ["kind", "title", "target", "purpose", "expected", "host_id"])
def test_intake_422_per_missing_required_field(client, field):
    body = {**INTAKE, "host_id": "desktop-pc"}
    body.pop(field)
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 422, r.text
    assert field in r.json()["fields"]
    assert client.get("/api/jobs").json() == []  # the intake gate stored nothing


def test_intake_rules_host_choice_cap_reason_and_unknown_host(client):
    # host_choice='ask' satisfies the host requirement and leaves the job unassigned
    asked = client.post("/api/jobs", json={**INTAKE, "host_choice": "ask"})
    assert asked.status_code == 201 and asked.json()["host_id"] is None
    # a >5 min test needs a reason
    over = client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", "max_minutes": 30})
    assert over.status_code == 422 and "reason" in over.json()["fields"]
    ok = client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", "max_minutes": 30, "reason": "full demo"})
    assert ok.status_code == 201 and ok.json()["params"]["max_minutes"] == 30
    # bad enum values / unknown host are fields too
    assert client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", "kind": "nope"}).status_code == 422
    assert client.post("/api/jobs", json={**INTAKE, "host_id": "ghost"}).status_code == 422
    assert (
        client.post("/api/jobs", json={**INTAKE, "host_id": "desktop-pc", "screenshots": "always"}).status_code == 422
    )


# ------------------------------------------------------------- queue ordering
def test_queue_order_and_next_conflict_when_lease_held(jdb, client):
    a = _enq(client, title="A").json()
    b = _enq(client, title="B").json()
    c = _enq(client, title="C").json()
    # position is the queue order (ascending): put C first, then A, then B
    for job, pos in ((c, 10), (a, 20), (b, 30)):
        assert (
            client.post(f"/api/jobs/{job['id']}/assign", json={"host_id": "desktop-pc", "position": pos}).status_code
            == 200
        )
    assert client.get("/api/jobs/next", params={"host_id": "desktop-pc"}).json()["id"] == c["id"]
    # a fresh lock on the host makes next a 409 naming the holder
    hosts.claim_host(jdb, "desktop-pc", "ws/other", phase="compile")
    r = client.get("/api/jobs/next", params={"host_id": "desktop-pc"})
    assert r.status_code == 409 and "ws/other" in r.json()["detail"]
    hosts.release_host(jdb, "desktop-pc", "ws/other")
    # start moves the head out of the queue
    assert client.post(f"/api/jobs/{c['id']}/start", json={"worker": "ws/x"}).json()["status"] == "running"
    assert client.get("/api/jobs/next", params={"host_id": "desktop-pc"}).json()["id"] == a["id"]
    # an empty host queue is a 404 (host is known, queue is not)
    assert client.get("/api/jobs/next", params={"host_id": "mini"}).status_code == 404
    assert client.get("/api/jobs/next", params={"host_id": "ghost"}).status_code == 400  # unknown host -> HostError
    # reassigning a running job is a conflict
    assert client.post(f"/api/jobs/{c['id']}/assign", json={"host_id": "mini"}).status_code == 409
    assert client.get(f"/api/jobs/{b['id']}").json()["status"] == "queued"  # the third job still queues


def test_jobs_never_claim_the_host_lease(jdb, client):
    jid = _enq(client).json()["id"]
    client.post(f"/api/jobs/{jid}/start", json={"worker": "ws/x"})
    client.post(f"/api/jobs/{jid}/event", json={"level": "milestone", "message": "running"})
    client.post(f"/api/jobs/{jid}/finish", json={"status": "passed", "summary": _summary()})
    assert hosts.get_host(jdb, "desktop-pc").lock is None  # never taken by the job


def test_list_filters(client):
    a = _enq(client, title="A").json()
    b = _enq(client, title="B", host_id="mini").json()
    assert [j["id"] for j in client.get("/api/jobs", params={"host_id": "mini"}).json()] == [b["id"]]
    client.post(f"/api/jobs/{a['id']}/start", json={"worker": "ws/x"})
    active = client.get("/api/jobs", params={"status": "active"}).json()
    assert {j["id"] for j in active} == {a["id"], b["id"]}  # active = queued + running
    assert [j["id"] for j in client.get("/api/jobs", params={"status": "running"}).json()] == [a["id"]]
    assert client.get("/api/jobs", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/jobs", params={"host_id": "mini"}).json()[0]["host_id"] == "mini"


# ---------------------------------------------------------------- timeline
def test_event_phase_update_and_log_tail_cap(client):
    jid = _enq(client).json()["id"]
    r = client.post(f"/api/jobs/{jid}/event", json={"level": "milestone", "message": "launched", "phase": "boot"})
    assert r.status_code == 200 and r.json()["phase"] == "boot"
    assert client.get(f"/api/jobs/{jid}").json()["phase"] == "boot"  # the phase updated the job
    big = "x" * (70 * 1024)
    r = client.post(
        f"/api/jobs/{jid}/event",
        json={"level": "crash", "message": "cs2 died", "data": {"log_tail": big, "exit_code": 1}},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["log_tail"].encode()) <= 65536  # capped at 64 KB
    assert r.json()["data"]["exit_code"] == 1
    # error/crash need a message; an unknown level is rejected
    assert client.post(f"/api/jobs/{jid}/event", json={"level": "crash", "message": "  "}).status_code == 422
    assert client.post(f"/api/jobs/{jid}/event", json={"level": "nope", "message": "x"}).status_code == 422
    assert client.post(f"/api/jobs/{jid}/event", json={"level": "crash", "message": "boom"}).status_code == 200
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["crash_count"] == 2 and j["error_count"] == 0
    assert client.post("/api/jobs/j-ghost/event", json={"level": "info", "message": "x"}).status_code == 404


# --------------------------------------------------------------- artifacts
def test_artifact_put_get_roundtrip_and_guards(client, jdb):
    jid = _enq(client).json()["id"]
    png = _png()
    r = client.put(f"/api/jobs/{jid}/artifacts/hud.png", content=png, headers={"content-type": "image/png"})
    assert r.status_code == 200, r.text
    art = r.json()
    assert art["size"] == len(png) and art["url"] == f"/api/artifacts/{jid}/hud.png"
    assert art["md5"] == hashlib.md5(png).hexdigest()
    got = client.get(f"/api/artifacts/{jid}/hud.png")
    assert got.status_code == 200 and got.content == png
    assert got.headers["content-type"] == "image/png"
    assert hashlib.md5(got.content).hexdigest() == art["md5"]
    # append-only: no overwrite
    again = client.put(f"/api/jobs/{jid}/artifacts/hud.png", content=png, headers={"content-type": "image/png"})
    assert again.status_code == 409
    # unknown file / unknown job are 404
    assert client.get(f"/api/artifacts/{jid}/nope.png").status_code == 404
    assert client.get("/api/artifacts/j-ghost/hud.png").status_code == 404
    # unsupported content type and the 25 MB cap are validation errors
    assert (
        client.put(f"/api/jobs/{jid}/artifacts/x.png", content=png, headers={"content-type": "image/gif"}).status_code
        == 422
    )
    huge = client.put(
        f"/api/jobs/{jid}/artifacts/big.zip",
        content=b"0" * (25 * 1024 * 1024 + 1),
        headers={"content-type": "application/zip"},
    )
    assert huge.status_code == 422
    # bad names (path traversal / regex) are rejected by the service
    for bad in ("..", "../x.png", "a/b.png", "we!rd.png", "x" * 121, ""):
        with pytest.raises(jobs.JobValidationError):
            jobs.save_artifact(jdb, jid, bad, b"x", "image/png")
    # content type is derived from the stored extension on read
    client.put(f"/api/jobs/{jid}/artifacts/notes.txt", content=b"hi", headers={"content-type": "text/plain"})
    assert client.get(f"/api/artifacts/{jid}/notes.txt").headers["content-type"].startswith("text/plain")


def test_screenshot_event_auto_added_for_images(client):
    jid = _enq(client).json()["id"]
    png = _png()
    client.put(f"/api/jobs/{jid}/artifacts/hud.png", content=png, headers={"content-type": "image/png"})
    shots = [e for e in client.get(f"/api/jobs/{jid}").json()["events"] if e["level"] == "screenshot"]
    assert len(shots) == 1
    assert shots[0]["data"] == {
        "url": f"/api/artifacts/{jid}/hud.png",
        "md5": hashlib.md5(png).hexdigest(),
        "bytes": len(png),
    }
    # a non-image adds no screenshot event, but still lands as an artifact
    client.put(f"/api/jobs/{jid}/artifacts/notes.txt", content=b"hi", headers={"content-type": "text/plain"})
    j = client.get(f"/api/jobs/{jid}").json()
    assert len([e for e in j["events"] if e["level"] == "screenshot"]) == 1
    assert {a["name"] for a in j["artifacts"]} == {"hud.png", "notes.txt"}


def test_request_screenshot_then_upload_clears_pending(client):
    jid = _enq(client).json()["id"]
    r = client.post(f"/api/jobs/{jid}/screenshot-request", json={"note": "the HUD after the wall test"})
    assert r.status_code == 200 and r.json()["params"]["pending_shot"] is True
    assert any(e["level"] == "info" and e["message"].startswith("screenshot requested") for e in r.json()["events"])
    # the runner answers by uploading; the flag clears
    client.put(f"/api/jobs/{jid}/artifacts/confirm.png", content=_png(), headers={"content-type": "image/png"})
    assert client.get(f"/api/jobs/{jid}").json()["params"]["pending_shot"] is False


# ------------------------------------------------------------------ finish
def _summary(**over):
    return {"what": "w", "why": "y", "learned": "l", "next": "n", **over}


def test_finish_gate_missing_and_oversized_fields(client):
    jid = _enq(client).json()["id"]
    client.post(f"/api/jobs/{jid}/start", json={"worker": "ws/x"})
    # a wholly missing summary lists all four fields
    r = client.post(f"/api/jobs/{jid}/finish", json={"status": "passed"})
    assert r.status_code == 422 and r.json()["fields"] == ["what", "why", "learned", "next"]
    # a missing single field is reported by name
    r = client.post(
        f"/api/jobs/{jid}/finish", json={"status": "passed", "summary": {"what": "a", "why": "b", "learned": "c"}}
    )
    assert r.status_code == 422 and r.json()["fields"] == ["next"]
    # an empty field fails too
    r = client.post(f"/api/jobs/{jid}/finish", json={"status": "passed", "summary": _summary(why="  ")})
    assert r.status_code == 422 and r.json()["fields"] == ["why"]
    # over 600 chars fails
    r = client.post(f"/api/jobs/{jid}/finish", json={"status": "passed", "summary": _summary(learned="x" * 601)})
    assert r.status_code == 422 and r.json()["fields"] == ["learned"]
    # a rejected gate leaves the job running
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "running"
    r = client.post(f"/api/jobs/{jid}/finish", json={"status": "passed", "summary": _summary()})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "passed" and j["finished_at"] and j["summary"] == _summary()
    # a second finish is a conflict; a bad status is validation
    assert client.post(f"/api/jobs/{jid}/finish", json={"status": "failed", "summary": _summary()}).status_code == 409
    assert client.post("/api/jobs/j-ghost/finish", json={"status": "passed", "summary": _summary()}).status_code == 404


def test_cancel_and_illegal_transitions(client):
    jid = _enq(client).json()["id"]
    r = client.post(f"/api/jobs/{jid}/cancel", json={"reason": "user stopped"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert any("cancelled: user stopped" in e["message"] for e in r.json()["events"])
    assert client.post(f"/api/jobs/{jid}/cancel", json={}).status_code == 409  # already terminal
    # start on a non-queued job is a conflict; a missing worker is validation
    assert client.post(f"/api/jobs/{jid}/start", json={"worker": "ws/x"}).status_code == 409
    other = _enq(client).json()["id"]
    assert client.post(f"/api/jobs/{other}/start", json={}).status_code == 422


def test_link_and_finish_status_validation(client):
    jid = _enq(client).json()["id"]
    r = client.post(f"/api/jobs/{jid}/link", json={"kind": "build", "ref_id": "b-1234abcd"})
    assert r.status_code == 200 and r.json()["links"] == [{"kind": "build", "ref_id": "b-1234abcd"}]
    # linking is idempotent (same pair -> still one link)
    assert client.post(f"/api/jobs/{jid}/link", json={"kind": "build", "ref_id": "b-1234abcd"}).json()["links"] == [
        {"kind": "build", "ref_id": "b-1234abcd"}
    ]
    assert client.post(f"/api/jobs/{jid}/link", json={"kind": "nope", "ref_id": "x"}).status_code == 422
    assert client.post(f"/api/jobs/{jid}/link", json={"kind": "build", "ref_id": ""}).status_code == 422
    client.post(f"/api/jobs/{jid}/start", json={"worker": "ws/x"})
    assert client.post(f"/api/jobs/{jid}/finish", json={"status": "nope", "summary": _summary()}).status_code == 422
    assert client.get("/api/jobs/j-ghost").status_code == 404


# -------------------------------------------------------------- push channel
def test_job_events_push_jsonl_and_health_field(client):
    body = client.get("/api/health").json()
    assert body["job_events_jsonl"] == str(jobs.job_events_jsonl_path())
    assert body["job_events_jsonl"].endswith("job_events.jsonl")
    jid = _enq(client).json()["id"]
    client.post(f"/api/jobs/{jid}/event", json={"level": "info", "message": "one"})
    client.post(f"/api/jobs/{jid}/event", json={"level": "screenshot", "message": "two", "phase": "capture"})
    lines = _lines()
    assert [ln["message"] for ln in lines] == ["one", "two"]
    assert lines[-1]["level"] == "screenshot" and lines[-1]["job_id"] == jid and lines[-1]["phase"] == "capture"


def test_publish_never_raises_on_write_failure(jdb, monkeypatch):
    jid = jobs.enqueue(jdb, **INTAKE, host_id="desktop-pc").id
    event = jobs.event(jdb, jid, level="info", message="x")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(jobs, "job_events_jsonl_path", boom)
    jobs.publish_event(event)  # side channel must not raise


# ------------------------------------------------- service-level edge cases
def test_next_job_not_found_at_service_level(jdb):
    with pytest.raises(NotFound):
        jobs.next_job(jdb, "mini")
    with pytest.raises(hosts.HostError):
        jobs.next_job(jdb, "ghost")


# ------------------------------------------------------------------- MCP
def test_mcp_jobs_registration_and_round_trip(jdb):
    server = create_mcp_server(jdb)

    async def main():
        async with Client(server) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            enq = await c.call_tool("job_enqueue", {**INTAKE, "host_id": "desktop-pc"})
            jid = enq.structured_content["id"]
            listed = await c.call_tool("job_list", {})
            got = await c.call_tool("job_get", {"job_id": jid})
            started = await c.call_tool("job_start", {"job_id": jid, "worker": "ws/mcp"})
            ev = await c.call_tool("job_event", {"job_id": jid, "level": "milestone", "message": "up", "phase": "boot"})
            return tools, jid, listed, got, started, ev

    tools, jid, listed, got, started, ev = anyio.run(main)
    assert {
        "job_enqueue",
        "job_list",
        "job_get",
        "job_next",
        "job_assign",
        "job_start",
        "job_event",
        "job_attach",
        "job_request_screenshot",
        "job_link",
        "job_finish",
        "job_cancel",
    } <= tools
    assert [j["id"] for j in listed.structured_content["result"]] == [jid]
    assert got.structured_content["id"] == jid and got.structured_content["status"] == "queued"
    assert started.structured_content["status"] == "running" and started.structured_content["worker"] == "ws/mcp"
    assert ev.structured_content["phase"] == "boot"


def test_mcp_job_attach_copies_a_local_file(jdb, tmp_path):
    server = create_mcp_server(jdb)
    png = tmp_path / "shot.png"
    png.write_bytes(_png())

    async def main():
        async with Client(server) as c:
            jid = (await c.call_tool("job_enqueue", {**INTAKE, "host_id": "desktop-pc"})).structured_content["id"]
            return await c.call_tool("job_attach", {"job_id": jid, "path": str(png)})

    res = anyio.run(main)
    assert res.structured_content["name"] == "shot.png" and res.structured_content["size"] == len(_png())


# ------------------------------------------------------------------- CLI
def _run_cli(monkeypatch, tmp_path, argv):
    from s2docs import cli
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_job_flow_and_exit_codes(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    hosts.register_host(db, "mini", "mini.local", shell="cmd")
    base = [
        "job",
        "enqueue",
        "--kind",
        "test",
        "--title",
        "t",
        "--target",
        "x@1",
        "--purpose",
        "p",
        "--expected",
        "e",
        "--host",
        "desktop-pc",
    ]
    assert _run_cli(monkeypatch, tmp_path, ["--json", *base]) == 0
    jid = json.loads(capsys.readouterr().out)["job"]["id"]

    # the intake gate is validation (2): a >5 min run without --reason
    assert _run_cli(monkeypatch, tmp_path, [*base, "--max-minutes", "30"]) == 2
    assert "error:" in capsys.readouterr().err

    # next finds the head; an empty host queue is not found (4)
    assert _run_cli(monkeypatch, tmp_path, ["--json", "job", "next", "--host", "desktop-pc"]) == 0
    capsys.readouterr()
    assert _run_cli(monkeypatch, tmp_path, ["job", "next", "--host", "mini"]) == 4

    # start, then a second start is a conflict (3)
    assert _run_cli(monkeypatch, tmp_path, ["job", "start", jid, "--worker", "ws/cli"]) == 0
    capsys.readouterr()
    assert _run_cli(monkeypatch, tmp_path, ["job", "start", jid, "--worker", "ws/cli"]) == 3

    # event with a log tail (tail 200 of 500 lines), attach a file, request a screenshot
    log = tmp_path / "cs2.log"
    log.write_text("\n".join(f"line{i}" for i in range(500)), encoding="utf-8")
    assert (
        _run_cli(
            monkeypatch,
            tmp_path,
            ["--json", "job", "event", jid, "--level", "error", "--message", "died", "--log-file", str(log)],
        )
        == 0
    )
    ev = json.loads(capsys.readouterr().out)["event"]
    assert ev["data"]["log_tail"].splitlines()[0] == "line300"  # last 200 of 500
    png = tmp_path / "hud.png"
    png.write_bytes(_png())
    assert _run_cli(monkeypatch, tmp_path, ["job", "attach", jid, str(png)]) == 0
    capsys.readouterr()
    assert _run_cli(monkeypatch, tmp_path, ["job", "shot-request", jid, "--note", "now"]) == 0
    capsys.readouterr()

    # an oversized summary field is validation (2); a full summary closes the job (0)
    assert (
        _run_cli(
            monkeypatch,
            tmp_path,
            [
                "job",
                "finish",
                jid,
                "--status",
                "passed",
                "--what",
                "a",
                "--why",
                "b",
                "--learned",
                "x" * 601,
                "--next",
                "d",
            ],
        )
        == 2
    )
    assert (
        _run_cli(
            monkeypatch,
            tmp_path,
            [
                "--json",
                "job",
                "finish",
                jid,
                "--status",
                "passed",
                "--what",
                "a",
                "--why",
                "b",
                "--learned",
                "c",
                "--next",
                "d",
            ],
        )
        == 0
    )
    capsys.readouterr()

    # show renders the closed-out job; watch returns immediately on a terminal job (0)
    assert _run_cli(monkeypatch, tmp_path, ["--json", "job", "show", jid]) == 0
    shown = json.loads(capsys.readouterr().out)["job"]
    assert shown["status"] == "passed" and shown["summary"]["learned"] == "c"
    assert _run_cli(monkeypatch, tmp_path, ["job", "watch", jid]) == 0
    capsys.readouterr()  # watch prints the event tail; drain it before the next parse
    # a finished job cannot be cancelled (3); an unknown job is not found (4)
    assert _run_cli(monkeypatch, tmp_path, ["job", "cancel", jid, "--reason", "x"]) == 3
    assert _run_cli(monkeypatch, tmp_path, ["job", "show", "j-ghost"]) == 4
    assert _run_cli(monkeypatch, tmp_path, ["--json", "job", "list", "--active"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == []


# --------------------------------------------------------------- web UI
def test_web_jobs_route_and_static_assets(client):
    js = client.get("/app.js").text
    assert "async jobs([jobId])" in js  # the #/jobs and #/jobs/<id> route
    assert '["jobs", "Jobs"]' in js  # nav entry
    assert 'GLOBAL_SECTIONS = new Set(["hosts", "jobs", "activity"])' in js  # install-wide, not per game
    assert "function jobBoard(" in js and "function jobDetail(" in js
    assert "data-job-shot" in js and "Request screenshot" in js and "Close-out summary" in js
    # the SPA is served as static assets; the hash route is client-side
    assert client.get("/").status_code == 200
    assert client.get("/index.html").status_code == 200
    assert client.get("/app.js").status_code == 200
