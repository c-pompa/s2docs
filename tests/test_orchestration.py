"""Orchestration: workflows, persistent projects, dependency ordering and server-enforced gates."""

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import evidence
from s2docs import models as m
from s2docs.api.app import create_app
from s2docs.build import workspace as ws
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server
from s2docs.orchestration.engine import GateFailed, OrchestrationError, Orchestrator, slug_addon
from s2docs.orchestration.workflows import WORKFLOWS
from s2docs.prompts import PROMPTS
from tests.test_build import BAD, GOOD, needs_tsc


@pytest.fixture
def orch(db, tmp_path):
    return Orchestrator(db, tmp_path / "ws")


def _attest(db, pid: str, step: str, **data) -> None:
    """Satisfy the `evidence` gate the way an agent would: one non-note evidence row for the step."""
    evidence.add(db, kind="console_transcript", project_id=pid, step_id=step, data=data or {"observed": "ok"})


def test_workflow_registry_references_real_tools_and_prompts(db, tmp_path):
    server = create_mcp_server(db, workspace=tmp_path)

    async def tools():
        async with Client(server) as c:
            return {t.name for t in (await c.list_tools()).tools}

    names = anyio.run(tools)
    for w in WORKFLOWS.values():
        for s in w.steps:
            assert set(s.tools) <= names, f"{w.id}.{s.id} references unknown tools {set(s.tools) - names}"
            if s.prompt:
                assert s.prompt["name"] in PROMPTS, f"{w.id}.{s.id} unknown prompt"


def test_slug_addon():
    assert slug_addon("Isometric Diablo-3 style viewer!") == "isometric_diablo_3_style_viewer"
    assert slug_addon("3D thing") == "addon_3d_thing"


def test_camera_project_flow_and_gates(orch, tmp_path, repo, db):
    brief = orch.start("cs2_camera_addon", "isometric diablo style match viewer", inputs={"view_style": "iso 55/45"})
    pid, addon = brief.project_id, brief.addon
    assert addon == "isometric_diablo_style_match_viewer"
    assert [s.step_id for s in brief.next_steps] == ["research"] and brief.progress == "0/10"
    assert "iso 55/45" in brief.next_steps[0].instructions  # inputs rendered into instructions

    with pytest.raises(OrchestrationError, match="depends on unfinished"):
        orch.update(pid, "design_rig", "done", notes="x")
    # notes alone no longer pass the evidence gate — a real (non-note) evidence row is required
    with pytest.raises(GateFailed, match="add_evidence"):
        orch.update(pid, "research", "done", notes="looked it up")
    _attest(db, pid, "research", api="CustomPlayerCamera + FOLLOW_POSITION")
    brief = orch.update(pid, "research", "done", notes="CustomPlayerCamera + FOLLOW_POSITION", artifacts=["api.md"])

    design = brief.next_steps[0]
    assert design.step_id == "design_rig" and design.prompt_name == "cs2_script"
    assert "isometric diablo style match viewer" in design.prompt_text  # prompt rendered with live context
    _attest(db, pid, "design_rig", rig="CONTROLLED, iso 55/45")
    orch.update(pid, "design_rig", "done", notes="rig chosen")

    with pytest.raises(GateFailed, match="does not exist"):
        orch.update(pid, "scaffold", "done")
    ws.scaffold(tmp_path / "ws", repo, game="cs2", name=addon)
    brief = orch.update(pid, "scaffold", "done", notes="scaffolded")
    # implement_camera and config are independent: both ready at once
    assert {s.step_id for s in brief.next_steps} == {"implement_camera", "config"}

    with pytest.raises(GateFailed, match="no cfg/"):
        orch.update(pid, "config", "done", notes="x")
    # a non-console .cfg outside cfg/ (GSI VDF) is not a console cfg and doesn't satisfy or fail the gate
    ws.write_file(tmp_path / "ws", "cs2", addon, "gsi/gamestate_integration_x.cfg", '"x" { "uri" "http://h" }')
    with pytest.raises(GateFailed, match="no cfg/"):
        orch.update(pid, "config", "done", notes="x")
    ws.write_file(tmp_path / "ws", "cs2", addon, "cfg/test.cfg", "mp_roundtme 3\n")
    with pytest.raises(GateFailed, match="unknown_command"):
        orch.update(pid, "config", "done", notes="x")
    ws.write_file(tmp_path / "ws", "cs2", addon, "cfg/test.cfg", "mp_roundtime 3\n", overwrite=True)
    orch.update(pid, "config", "done", notes="cfg ok")

    project = orch.get(pid)
    assert {s.id: s.status for s in project.steps}["config"] == "done"
    assert any(e.event == "done" and e.step_id == "config" for e in project.recent_events)


@needs_tsc
def test_scripts_typecheck_gate(orch, tmp_path, repo, db):
    pid = orch.start("cs2_script_feature", "jump boost", addon="jump_boost").project_id
    for step in ("research", "design"):
        _attest(db, pid, step, ok=True)
        orch.update(pid, step, "done", notes="ok")
    ws.scaffold(tmp_path / "ws", repo, game="cs2", name="jump_boost")
    orch.update(pid, "scaffold", "done", notes="ok")
    # The scaffolded starter uses real-API methods absent from the test fixture d.ts, so replace it.
    ws.write_file(tmp_path / "ws", "cs2", "jump_boost", "scripts/jump_boost.js", BAD, overwrite=True)
    # Node tooling outside scripts/ is not cs_script and must not be type-checked against point_script.d.ts
    ws.write_file(tmp_path / "ws", "cs2", "jump_boost", "tools/helper.js", "require('net').connect(1);")
    orch.update(pid, "implement", "done", notes="written")
    with pytest.raises(GateFailed, match="unknown_api") as exc:
        orch.update(pid, "validate", "done", notes="checked")
    assert "scripts/jump_boost.js:2" in str(exc.value)
    ws.write_file(tmp_path / "ws", "cs2", "jump_boost", "scripts/jump_boost.js", GOOD, overwrite=True)
    brief = orch.update(pid, "validate", "done", notes="clean")
    assert [s.step_id for s in brief.next_steps] == ["hammer_setup"]


def test_skip_block_and_completion(orch):
    pid = orch.start("server_config", "aim practice", game="cs2").project_id
    with pytest.raises(OrchestrationError, match="needs `notes`"):
        orch.update(pid, "draft", "blocked")
    orch.update(pid, "draft", "blocked", notes="waiting on user")
    assert orch.brief(pid).blocked_steps == ["draft"]
    orch.update(pid, "draft", "done", notes="sv_cheats 1")
    brief = orch.update(pid, "validate", "skipped", notes="user will validate")
    assert brief.status == "done" and brief.progress == "2/2"
    # A notes-only update on a finished project is allowed (ledger as you go): same status, no reopen.
    after = orch.update(pid, "validate", "skipped", notes="post-check: user confirmed in game")
    assert after.status == "done"
    project = orch.get(pid)
    validate = next(s for s in project.steps if s.id == "validate")
    assert validate.status == "skipped" and "confirmed in game" in (validate.notes or "")
    assert any(e.event == "note" and e.step_id == "validate" for e in project.recent_events)
    # ...but a status-changing update on a done project is still rejected.
    with pytest.raises(OrchestrationError, match="done"):
        orch.update(pid, "draft", "pending")
    with pytest.raises(OrchestrationError, match="done"):
        orch.update(pid, "validate", "in_progress")
    assert orch.list("done")[0].id == pid


def test_done_project_accepts_notes_and_evidence(orch, db, tmp_path):
    pid = orch.start("server_config", "aim practice", game="cs2").project_id
    orch.update(pid, "draft", "done", notes="sv_cheats 1")
    orch.update(pid, "validate", "skipped", notes="user validates")
    assert orch.get(pid).status == "done"
    # Evidence still records against the finished project (the evidence gate is not re-run).
    ev = evidence.add(db, kind="console_transcript", project_id=pid, step_id="validate", data={"ok": True})
    assert ev.id and evidence.get(db, ev.id).project_id == pid
    # A follow-up note appends without reopening the step.
    orch.update(pid, "draft", "done", notes="follow-up note")
    step = next(s for s in orch.get(pid).steps if s.id == "draft")
    assert step.status == "done" and "follow-up note" in (step.notes or "")


def test_engine_reads_do_not_take_the_write_lock(orch, monkeypatch):
    """GET-only paths (project list/get/brief) must read through db.reader(), not the writer."""
    pid = orch.start("server_config", "x", game="cs2").project_id

    def boom(self):
        raise AssertionError("a read path took the write lock (db.writer)")

    monkeypatch.setattr(Database, "writer", boom)
    assert orch.list()[0].id == pid
    assert orch.get(pid).id == pid
    assert orch.brief(pid).project_id == pid


def test_custom_workflow_validation(orch):
    with pytest.raises(OrchestrationError, match="needs `steps`"):
        orch.start("custom", "x")
    bad_dep = [m.CustomStep(id="a", title="A", instructions="do", depends_on=["zzz"])]
    with pytest.raises(OrchestrationError, match="unknown dependency"):
        orch.start("custom", "x", custom_steps=bad_dep)
    bad_gate = [m.CustomStep(id="a", title="A", instructions="do", gate="vibes")]
    with pytest.raises(OrchestrationError, match="unknown gate"):
        orch.start("custom", "x", custom_steps=bad_gate)
    ok = [
        m.CustomStep(id="a", title="A", instructions="do $goal"),
        m.CustomStep(id="b", title="B", instructions="then", depends_on=["a"]),
    ]
    brief = orch.start("custom", "my goal", custom_steps=ok)
    assert [s.step_id for s in brief.next_steps] == ["a"] and brief.next_steps[0].instructions == "do my goal"
    with pytest.raises(OrchestrationError, match="unknown workflow"):
        orch.start("nope", "x")
    with pytest.raises(OrchestrationError, match="supports"):
        orch.start("cs2_camera_addon", "x", game="deadlock")


def test_orchestration_api(db, tmp_path):
    with TestClient(create_app(db.path, workspace=tmp_path / "ws")) as c:
        assert {w["id"] for w in c.get("/api/workflows").json()} >= {"cs2_camera_addon", "cs2_game_template"}
        r = c.post("/api/projects", json={"workflow": "cs2_game_template", "goal": "iso arpg arena"})
        assert r.status_code == 201
        pid = r.json()["project_id"]
        r = c.post(f"/api/projects/{pid}/steps/research", json={"status": "done"})
        assert r.status_code == 409 and r.json()["gate_failed"]
        # a note kind alone must not satisfy the gate
        note = c.post("/api/evidence", json={"project_id": pid, "step_id": "research", "kind": "note", "data": {}})
        assert note.status_code == 201
        r = c.post(f"/api/projects/{pid}/steps/research", json={"status": "done", "notes": "limits listed"})
        assert r.status_code == 409 and r.json()["gate_failed"]
        # a real evidence row does
        ev = c.post(
            "/api/evidence",
            json={"project_id": pid, "step_id": "research", "kind": "console_transcript", "data": {"limits": 3}},
        )
        assert ev.status_code == 201
        r = c.post(f"/api/projects/{pid}/steps/research", json={"status": "done", "notes": "limits listed"})
        assert r.status_code == 200 and r.json()["next_steps"][0]["step_id"] == "architecture"
        assert c.get(f"/api/projects/{pid}").json()["steps"][0]["status"] == "done"
        assert c.get("/api/evidence", params={"project_id": pid, "step_id": "research"}).json()[0]["kind"] in (
            "note",
            "console_transcript",
        )
        assert c.get("/api/projects/p-missing").status_code == 404


def test_orchestration_mcp(db, tmp_path):
    server = create_mcp_server(db, workspace=tmp_path / "ws")

    async def main():
        async with Client(server) as c:
            started = await c.call_tool(
                "start_project", {"workflow": "cs2_camera_addon", "goal": "isometric viewer", "addon": "iso_view"}
            )
            pid = started.structured_content["project_id"]
            gate = await c.call_tool("update_step", {"project_id": pid, "step_id": "research", "status": "done"})
            ev = await c.call_tool(
                "add_evidence",
                {
                    "project_id": pid,
                    "step_id": "research",
                    "kind": "console_transcript",
                    "data": {"api": ["CustomPlayerCamera"]},
                },
            )
            ok = await c.call_tool(
                "update_step", {"project_id": pid, "step_id": "research", "status": "done", "notes": "found API"}
            )
            listed = await c.call_tool("list_evidence", {"project_id": pid})
            text = await c.call_tool("get_prompt_text", {"name": "orchestrate", "arguments": {"goal": "iso view"}})
            kickoff = await c.get_prompt("orchestrate", {"goal": "isometric viewer"})
            return started, gate, ev, ok, listed, text, kickoff

    started, gate, ev, ok, listed, text, kickoff = anyio.run(main)
    assert started.structured_content["addon"] == "iso_view"
    assert gate.is_error and "evidence" in gate.content[0].text
    assert not ev.is_error and ev.structured_content["kind"] == "console_transcript"
    assert ok.structured_content["next_steps"][0]["prompt_name"] == "cs2_script"
    assert listed.structured_content["result"][0]["step_id"] == "research"
    assert "cs2_camera_addon" in text.structured_content["result"]
    assert "start_project" in kickoff.messages[0].content.text
