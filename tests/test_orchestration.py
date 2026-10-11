"""Orchestration: workflows, persistent projects, dependency ordering and server-enforced gates."""

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import models as m
from s2docs.api.app import create_app
from s2docs.build import workspace as ws
from s2docs.mcp_server import create_mcp_server
from s2docs.orchestration.engine import GateFailed, OrchestrationError, Orchestrator, slug_addon
from s2docs.orchestration.workflows import WORKFLOWS
from s2docs.prompts import PROMPTS
from tests.test_build import BAD, GOOD, needs_tsc


@pytest.fixture
def orch(db, tmp_path):
    return Orchestrator(db, tmp_path / "ws")


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


def test_camera_project_flow_and_gates(orch, tmp_path, repo):
    brief = orch.start("cs2_camera_addon", "isometric diablo style match viewer", inputs={"view_style": "iso 55/45"})
    pid, addon = brief.project_id, brief.addon
    assert addon == "isometric_diablo_style_match_viewer"
    assert [s.step_id for s in brief.next_steps] == ["research"] and brief.progress == "0/10"
    assert "iso 55/45" in brief.next_steps[0].instructions  # inputs rendered into instructions

    with pytest.raises(OrchestrationError, match="depends on unfinished"):
        orch.update(pid, "design_rig", "done", notes="x")
    with pytest.raises(GateFailed, match="evidence"):
        orch.update(pid, "research", "done")
    brief = orch.update(pid, "research", "done", notes="CustomPlayerCamera + FOLLOW_POSITION", artifacts=["api.md"])

    design = brief.next_steps[0]
    assert design.step_id == "design_rig" and design.prompt_name == "cs2_script"
    assert "isometric diablo style match viewer" in design.prompt_text  # prompt rendered with live context
    orch.update(pid, "design_rig", "done", notes="rig chosen")

    with pytest.raises(GateFailed, match="does not exist"):
        orch.update(pid, "scaffold", "done")
    ws.scaffold(tmp_path / "ws", repo, game="cs2", name=addon)
    brief = orch.update(pid, "scaffold", "done", notes="scaffolded")
    # implement_camera and config are independent: both ready at once
    assert {s.step_id for s in brief.next_steps} == {"implement_camera", "config"}

    with pytest.raises(GateFailed, match="no .cfg files"):
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
def test_scripts_typecheck_gate(orch, tmp_path, repo):
    pid = orch.start("cs2_script_feature", "jump boost", addon="jump_boost").project_id
    for step in ("research", "design"):
        orch.update(pid, step, "done", notes="ok")
    ws.scaffold(tmp_path / "ws", repo, game="cs2", name="jump_boost")
    orch.update(pid, "scaffold", "done", notes="ok")
    # The scaffolded starter uses real-API methods absent from the test fixture d.ts, so replace it.
    ws.write_file(tmp_path / "ws", "cs2", "jump_boost", "scripts/jump_boost.js", BAD, overwrite=True)
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
    with pytest.raises(OrchestrationError, match="is done"):
        orch.update(pid, "draft", "pending")
    assert orch.list("done")[0].id == pid


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
        r = c.post(f"/api/projects/{pid}/steps/research", json={"status": "done", "notes": "limits listed"})
        assert r.status_code == 200 and r.json()["next_steps"][0]["step_id"] == "architecture"
        assert c.get(f"/api/projects/{pid}").json()["steps"][0]["status"] == "done"
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
            ok = await c.call_tool(
                "update_step", {"project_id": pid, "step_id": "research", "status": "done", "notes": "found API"}
            )
            text = await c.call_tool("get_prompt_text", {"name": "orchestrate", "arguments": {"goal": "iso view"}})
            kickoff = await c.get_prompt("orchestrate", {"goal": "isometric viewer"})
            return started, gate, ok, text, kickoff

    started, gate, ok, text, kickoff = anyio.run(main)
    assert started.structured_content["addon"] == "iso_view"
    assert gate.is_error and "evidence" in gate.content[0].text
    assert ok.structured_content["next_steps"][0]["prompt_name"] == "cs2_script"
    assert "cs2_camera_addon" in text.structured_content["result"]
    assert "start_project" in kickoff.messages[0].content.text
