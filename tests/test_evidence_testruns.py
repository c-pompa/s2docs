"""Evidence store + test-run records (Phases 4+5) — the gate that checks real artifacts, and the
server-side test session that mirrors the in-game TEST STATUS HUD strip. No real host/network: host
locks are DB-only and the ssh layer is never touched.
"""

from __future__ import annotations

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import evidence, hosts, test_runs
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound
from s2docs.mcp_server import create_mcp_server
from s2docs.orchestration.engine import GateFailed, Orchestrator


@pytest.fixture
def edb(tmp_path) -> Database:
    db = Database(tmp_path / "evidence.db")
    hosts.register_host(db, "desktop-pc", "pompa@100.64.130.72", shell="powershell")
    return db


def _project(db: Database, tmp_path) -> str:
    """A server_config project (no addon) — two steps, the second gated on evidence."""
    orch = Orchestrator(db, tmp_path / "ws")
    return orch.start("server_config", "aim practice", game="cs2").project_id


# ------------------------------------------------------------------ evidence
def test_evidence_add_get_list(edb, tmp_path):
    pid = _project(edb, tmp_path)
    ev = evidence.add(
        edb,
        kind="pixel_diff",
        project_id=pid,
        step_id="validate",
        ref="shots/frame_diff.png",
        data={"pixel_diff_pct": 1.2, "noise_floor": 0.5},
    )
    assert ev.id.startswith("e-") and ev.kind == "pixel_diff" and ev.data["noise_floor"] == 0.5
    assert evidence.get(edb, ev.id).ref == "shots/frame_diff.png"
    assert evidence.list_evidence(edb, project_id=pid, step_id="validate") == [ev]
    assert evidence.list_evidence(edb, kind="pixel_diff")[0].id == ev.id
    assert evidence.list_evidence(edb, kind="note") == []
    # project-level evidence (no step) is allowed too
    ev2 = evidence.add(edb, kind="file_hash", project_id=pid, ref="addon.vpk", data={"md5": "ab" * 16})
    assert ev2.step_id is None and len(evidence.list_evidence(edb, project_id=pid)) == 2
    with pytest.raises(NotFound):
        evidence.get(edb, "e-missing")


def test_evidence_validation_and_caps(edb, tmp_path):
    pid = _project(edb, tmp_path)
    with pytest.raises(evidence.EvidenceError, match="kind must be"):
        evidence.add(edb, kind="vibes", project_id=pid, step_id="draft")
    with pytest.raises(evidence.EvidenceError, match="project_id is required"):
        evidence.add(edb, kind="note", project_id=" ")
    with pytest.raises(evidence.EvidenceError, match="too long"):
        evidence.add(edb, kind="other", project_id=pid, ref="x" * (evidence.MAX_REF_LEN + 1))
    with pytest.raises(evidence.EvidenceError, match="too large"):
        evidence.add(edb, kind="other", project_id=pid, data={"blob": "x" * (evidence.MAX_DATA_BYTES + 1)})
    # a step that does not belong to the project is rejected by the composite FK
    with pytest.raises(evidence.EvidenceError, match="unknown project/step"):
        evidence.add(edb, kind="note", project_id=pid, step_id="nope")
    with pytest.raises(evidence.EvidenceError, match="unknown project/step"):
        evidence.add(edb, kind="note", project_id="p-ghost", step_id="draft")


def test_evidence_gate_requires_non_note_row(edb, tmp_path):
    """The whole point of Phase 4: the `evidence` gate checks a stored row, and notes alone fail."""
    orch = Orchestrator(edb, tmp_path / "ws")
    pid = orch.start("server_config", "smoke", game="cs2").project_id
    orch.update(pid, "draft", "done", notes="drafted")
    # notes/artifacts are not enough any more
    with pytest.raises(GateFailed, match="add_evidence"):
        orch.update(pid, "validate", "done", notes="looks right", artifacts=["cfg.txt"])
    evidence.add(edb, kind="note", project_id=pid, step_id="validate", data={"text": "second opinion"})
    with pytest.raises(GateFailed, match="add_evidence"):
        orch.update(pid, "validate", "done", notes="looks right")
    assert evidence.count_for_step(edb, pid, "validate", exclude_note=True) == 0
    # one real row flips the gate
    evidence.add(edb, kind="console_transcript", project_id=pid, step_id="validate", data={"commands": 12})
    assert evidence.count_for_step(edb, pid, "validate", exclude_note=True) == 1
    brief = orch.update(pid, "validate", "done", notes="validated")
    assert brief.status == "done"


# ------------------------------------------------------------------ test runs
def test_test_run_lifecycle_and_elapsed(edb):
    run = test_runs.start(edb, "iso_test")
    assert run.id.startswith("t-") and run.state == "running" and run.elapsed_sec >= 0
    stepped = test_runs.step(edb, run.id, "wall-occlusion probe")
    assert stepped.step == "wall-occlusion probe"
    # elapsed is computed from started_at
    with edb.writer() as conn, conn:
        conn.execute(
            "UPDATE test_runs SET started_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-100 seconds') WHERE id = ?",
            (run.id,),
        )
    assert 95 <= test_runs.get(edb, run.id).elapsed_sec <= 120
    assert test_runs.current(edb).id == run.id
    finished = test_runs.finish(edb, run.id, "pass", note="clean")
    assert finished.state == "pass" and finished.note == "clean" and finished.finished_at is not None
    assert finished.elapsed_sec == test_runs.get(edb, run.id).elapsed_sec  # frozen at finished_at
    # the HUD still shows the result until it is dismissed with `off`
    assert test_runs.current(edb).state == "pass"
    test_runs.clear(edb, run.id)
    assert test_runs.current(edb) is None


def test_test_run_state_transitions(edb):
    run = test_runs.start(edb, "iso_test")
    with pytest.raises(test_runs.TestRunError, match="result must be"):
        test_runs.finish(edb, run.id, "maybe")
    test_runs.finish(edb, run.id, "pass")
    with pytest.raises(test_runs.TestRunError, match="only a running run can finish"):
        test_runs.finish(edb, run.id, "pass")
    with pytest.raises(test_runs.TestRunError, match="not running"):
        test_runs.step(edb, run.id, "later")
    with pytest.raises(test_runs.TestRunError, match="name is required"):
        test_runs.start(edb, "  ")
    with pytest.raises(NotFound):
        test_runs.get(edb, "t-missing")
    # off / clear
    assert test_runs.clear(edb, run.id).state == "idle"
    with pytest.raises(test_runs.TestRunError, match="already idle"):
        test_runs.clear(edb, run.id)


def test_test_run_claims_and_releases_host(edb, tmp_path):
    pid = _project(edb, tmp_path)
    run = test_runs.start(edb, "iso_test", host_id="desktop-pc", project_id=pid)
    lock = hosts.get_host(edb, "desktop-pc").lock
    assert lock is not None and lock.owner == f"test:{run.id}"
    test_runs.finish(edb, run.id, "pass", note="ok")
    assert hosts.get_host(edb, "desktop-pc").lock is None
    # finishing a project-linked run records a console-transcript evidence row
    rows = evidence.list_evidence(edb, project_id=pid, kind="console_transcript")
    assert len(rows) == 1 and rows[0].data["state"] == "pass" and rows[0].data["name"] == "iso_test"


def test_test_run_finish_does_not_release_another_owners_lock(edb, tmp_path):
    """The review's critical finding: finishing/clearing an orphaned test run must only release the
    lease it still owns — never delete whoever holds the host now."""
    pid = _project(edb, tmp_path)
    run = test_runs.start(edb, "iso_test", host_id="desktop-pc", project_id=pid)
    hosts.claim_host(edb, "desktop-pc", "ws/new-owner", force=True)  # another owner takes over
    test_runs.finish(edb, run.id, "pass", note="orphan finished")
    lock = hosts.get_host(edb, "desktop-pc").lock
    assert lock is not None and lock.owner == "ws/new-owner"  # the new owner's lock survives


def test_test_run_clear_does_not_release_another_owners_lock(edb):
    run = test_runs.start(edb, "iso_test", host_id="desktop-pc")
    hosts.claim_host(edb, "desktop-pc", "ws/new-owner", force=True)
    cleaned = test_runs.clear(edb, run.id)  # `iso_test off` on an orphaned run
    assert cleaned.state == "idle"
    lock = hosts.get_host(edb, "desktop-pc").lock
    assert lock is not None and lock.owner == "ws/new-owner"


def test_test_run_host_contention_and_unknown(edb):
    hosts.claim_host(edb, "desktop-pc", "ws/other")
    with pytest.raises(hosts.HostLocked):
        test_runs.start(edb, "iso_test", host_id="desktop-pc")
    assert test_runs.list_runs(edb) == []  # nothing recorded when the lease can't be taken
    with pytest.raises(hosts.HostError):
        test_runs.start(edb, "iso_test", host_id="ghost")
    with pytest.raises(test_runs.TestRunError, match="unknown project"):
        test_runs.start(edb, "iso_test", project_id="p-ghost")
    assert test_runs.list_runs(edb) == []


def test_test_run_list_filters(edb):
    a = test_runs.start(edb, "a")
    test_runs.start(edb, "b")
    test_runs.finish(edb, a.id, "fail")
    assert {r.name for r in test_runs.list_runs(edb)} == {"a", "b"}
    assert [r.name for r in test_runs.list_runs(edb, state="fail")] == ["a"]
    assert [r.name for r in test_runs.list_runs(edb, state="running")] == ["b"]
    with pytest.raises(test_runs.TestRunError, match="state must be"):
        test_runs.list_runs(edb, state="bogus")


# ---------------------------------------------------------------- facades: MCP
def test_mcp_evidence_and_test_flow(edb, tmp_path):
    server = create_mcp_server(edb, workspace=tmp_path / "ws")

    async def main():
        async with Client(server) as c:
            started = await c.call_tool(
                "start_project", {"workflow": "server_config", "goal": "aim practice", "game": "cs2"}
            )
            pid = started.structured_content["project_id"]
            ev = await c.call_tool(
                "add_evidence",
                {"project_id": pid, "step_id": "draft", "kind": "file_hash", "data": {"md5": "cd" * 16}},
            )
            listed = await c.call_tool("list_evidence", {"project_id": pid})
            t = await c.call_tool("test_start", {"name": "iso_test", "host_id": "desktop-pc", "project_id": pid})
            rid = t.structured_content["id"]
            stepped = await c.call_tool("test_step", {"run_id": rid, "text": "occ probe"})
            finished = await c.call_tool("test_finish", {"run_id": rid, "result": "pass", "note": "clean"})
            status = await c.call_tool("test_status", {"run_id": rid})
            current = await c.call_tool("test_status", {})
            runs = await c.call_tool("list_test_runs", {"state": "pass"})
            bad = await c.call_tool("add_evidence", {"project_id": pid, "step_id": "draft", "kind": "vibes"})
            return ev, listed, t, stepped, finished, status, current, runs, bad

    ev, listed, t, stepped, finished, status, current, runs, bad = anyio.run(main)
    assert not ev.is_error and ev.structured_content["kind"] == "file_hash"
    assert listed.structured_content["result"][0]["step_id"] == "draft"
    assert not t.is_error and t.structured_content["state"] == "running"
    assert stepped.structured_content["step"] == "occ probe"
    assert finished.structured_content["state"] == "pass"
    assert status.structured_content["name"] == "iso_test"
    assert current.structured_content["id"] == t.structured_content["id"]
    assert runs.structured_content["result"][0]["state"] == "pass"
    # the MCP schema (Literal) rejects a bad kind before it ever reaches the store
    assert bad.is_error and "kind" in bad.content[0].text


# --------------------------------------------------------------- facades: HTTP
@pytest.fixture
def client(edb):
    with TestClient(create_app(edb.path)) as c:
        yield c


def test_api_human_confirm_evidence_without_project(edb, client, tmp_path):
    """The director's human-confirm gate POSTs kind='human-confirm' with NO project_id, only a test_run_id
    (the live run t-5e1fb5cc has project_id=NULL). The row must store with project_id NULL + test_run_id
    set, and read back through the kind filter with its payload intact."""
    run = test_runs.start(edb, "iso_test")  # like the live run: no project
    payload = {
        "kind": "human-confirm",
        "test_run_id": run.id,
        "data": {
            "test_run_id": run.id,
            "verdict": "pass",
            "notes": "wall looked clean",
            "evidence_refs": ["shots/a.png", "shots/b.png"],
            "purpose": "accept the overlay",
            "expected": "no gaps",
            "map": "de_dust2",
            "source": "director",
        },
    }
    r = client.post("/api/evidence", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "human-confirm"
    assert body["project_id"] is None  # resolved from a project-less run -> stays NULL
    assert body["test_run_id"] == run.id
    eid = body["id"]
    # GET ?kind=human-confirm returns it, payload intact
    listed = client.get("/api/evidence", params={"kind": "human-confirm"}).json()
    assert [e["id"] for e in listed] == [eid]
    d = listed[0]["data"]
    assert d["verdict"] == "pass" and d["notes"] == "wall looked clean"
    assert d["evidence_refs"] == ["shots/a.png", "shots/b.png"]
    assert d["purpose"] == "accept the overlay" and d["expected"] == "no gaps"
    assert d["map"] == "de_dust2" and d["source"] == "director"
    # and the store round-trips it (durable on disk, not just in the response)
    assert evidence.get(edb, eid).data["verdict"] == "pass"
    assert "human-confirm" in evidence.EVIDENCE_KINDS


def test_api_human_confirm_resolves_project_from_run(edb, client, tmp_path):
    """When the run DOES have a project, the omitted project_id is resolved from it (FK stays valid)."""
    pid = _project(edb, tmp_path)
    run = test_runs.start(edb, "iso_test", project_id=pid)
    r = client.post(
        "/api/evidence", json={"kind": "human-confirm", "test_run_id": run.id, "data": {"verdict": "fail"}}
    )
    assert r.status_code == 201, r.text
    assert r.json()["project_id"] == pid
    assert r.json()["test_run_id"] == run.id
    rows = client.get("/api/evidence", params={"project_id": pid, "kind": "human-confirm"}).json()
    assert [e["id"] for e in rows] == [r.json()["id"]]


def test_api_evidence_needs_project_or_known_run(edb, client):
    """A human-confirm row with neither project_id nor a run is still rejected, as is an unknown run."""
    r = client.post("/api/evidence", json={"kind": "human-confirm", "data": {"verdict": "pass"}})
    assert r.status_code == 400 and "project_id is required" in r.json()["detail"]
    r = client.post("/api/evidence", json={"kind": "human-confirm", "test_run_id": "t-ghost"})
    assert r.status_code == 400 and "unknown test_run" in r.json()["detail"]


def test_api_evidence_and_test_routes(edb, client, tmp_path):
    pid = _project(edb, tmp_path)
    r = client.post(
        "/api/evidence",
        json={"project_id": pid, "step_id": "draft", "kind": "screenshot", "ref": "shots/a.png", "data": {"w": 1920}},
    )
    assert r.status_code == 201 and r.json()["kind"] == "screenshot"
    eid = r.json()["id"]
    assert client.get(f"/api/evidence/{eid}").json()["ref"] == "shots/a.png"
    assert client.get("/api/evidence", params={"project_id": pid, "step_id": "draft"}).json()[0]["id"] == eid
    assert client.post("/api/evidence", json={"project_id": pid, "kind": "bogus"}).status_code == 422

    started = client.post("/api/test-runs", json={"name": "iso_test", "host_id": "desktop-pc", "project_id": pid})
    assert started.status_code == 201 and started.json()["state"] == "running"
    rid = started.json()["id"]
    assert client.post(f"/api/test-runs/{rid}/step", json={"text": "probe"}).json()["step"] == "probe"
    assert client.get("/api/test-runs/current").json()["id"] == rid
    assert (
        client.post(f"/api/test-runs/{rid}/finish", json={"result": "fail", "note": "boom"}).json()["state"] == "fail"
    )
    assert client.get("/api/test-runs", params={"state": "fail"}).json()[0]["id"] == rid
    assert hosts.get_host(edb, "desktop-pc").lock is None
    # a second finish is a domain error (400)
    assert client.post(f"/api/test-runs/{rid}/finish", json={"result": "pass"}).status_code == 400
    assert client.post(f"/api/test-runs/{rid}/clear").json()["state"] == "idle"
    assert client.get("/api/test-runs/current").status_code == 404


# --------------------------------------------------------------- facades: CLI
def _run_cli(monkeypatch, tmp_path, argv):
    from s2docs import cli
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_evidence_and_test(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    pid = _project(db, tmp_path)

    rc = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--json",
            "evidence",
            "add",
            "--project",
            pid,
            "--step",
            "draft",
            "--kind",
            "file_hash",
            "--data",
            '{"md5": "aa"}',
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert '"schema": "s2docs/v1"' in out and '"kind": "file_hash"' in out
    eid = json.loads(out)["evidence"]["id"]

    rc = _run_cli(monkeypatch, tmp_path, ["--json", "evidence", "list", "--project", pid])
    assert rc == 0 and '"evidence"' in capsys.readouterr().out

    rc = _run_cli(monkeypatch, tmp_path, ["evidence", "show", eid])
    assert rc == 0 and "file_hash" in capsys.readouterr().out

    rc = _run_cli(monkeypatch, tmp_path, ["--json", "test", "start", "--name", "iso_test", "--host", "desktop-pc"])
    assert rc == 0
    rid = json.loads(capsys.readouterr().out)["test_run"]["id"]
    rc = _run_cli(monkeypatch, tmp_path, ["--json", "test", "step", rid, "occ probe"])
    assert rc == 0 and '"step": "occ probe"' in capsys.readouterr().out
    rc = _run_cli(monkeypatch, tmp_path, ["--json", "test", "finish", rid, "--result", "pass"])
    assert rc == 0 and '"state": "pass"' in capsys.readouterr().out
    rc = _run_cli(monkeypatch, tmp_path, ["--json", "test", "status"])
    assert rc == 0 and '"test_run"' in capsys.readouterr().out
    assert hosts.get_host(db, "desktop-pc").lock is None


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    with Database(tmp_path / "data" / "s2docs.db").writer() as conn, conn:
        pass  # create + migrate
    # not found
    assert _run_cli(monkeypatch, tmp_path, ["evidence", "show", "e-missing"]) == 3
    capsys.readouterr()
    assert _run_cli(monkeypatch, tmp_path, ["test", "status"]) == 3  # no active run
    capsys.readouterr()
    # validation error (bad --data JSON is a domain error, not an argparse usage error)
    assert (
        _run_cli(monkeypatch, tmp_path, ["evidence", "add", "--project", "p-1", "--kind", "note", "--data", "{nope"])
        == 1
    )
    capsys.readouterr()
    # locked
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x")
    hosts.claim_host(db, "desktop-pc", "ws/other")
    rc = _run_cli(monkeypatch, tmp_path, ["test", "start", "--name", "iso_test", "--host", "desktop-pc"])
    assert rc == 2 and "LOCKED" in capsys.readouterr().err


# ------------------------------------------------- human-confirm verdict/source gate
def test_api_human_confirm_verdict_and_source_validation(edb, client):
    """A human-confirm row IS a verdict: data.verdict is required and in {pass, fail, skip} (else 422),
    and data.source, when present, must be a known source."""
    run = test_runs.start(edb, "iso_test")
    # missing / out-of-set verdicts are 422 naming the field
    for data in ({}, {"verdict": "maybe"}, {"verdict": "PASS"}, {"verdict": True}):
        r = client.post("/api/evidence", json={"kind": "human-confirm", "test_run_id": run.id, "data": data})
        assert r.status_code == 422, r.text
        assert "verdict" in r.json()["fields"]
    # each allowed verdict is accepted
    for v in ("pass", "fail", "skip"):
        r = client.post("/api/evidence", json={"kind": "human-confirm", "test_run_id": run.id, "data": {"verdict": v}})
        assert r.status_code == 201, r.text
    # a source outside the set is 422 naming 'source'; each known source is accepted
    bad = client.post(
        "/api/evidence",
        json={"kind": "human-confirm", "test_run_id": run.id, "data": {"verdict": "pass", "source": "telepathy"}},
    )
    assert bad.status_code == 422 and "source" in bad.json()["fields"]
    for s in ("chat", "console", "key", "web", "coordinator", "director"):
        r = client.post(
            "/api/evidence",
            json={"kind": "human-confirm", "test_run_id": run.id, "data": {"verdict": "pass", "source": s}},
        )
        assert r.status_code == 201, r.text
    # a non-confirm kind is not verdict-gated (no verdict required)
    assert client.post("/api/evidence", json={"kind": "note", "test_run_id": run.id, "data": {}}).status_code == 201
