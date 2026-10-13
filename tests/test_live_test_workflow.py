"""cs2_live_test workflow + its two new gates (host_claimed, deploy_verified).

The gates are the point: the server, not the caller, decides whether the host is really claimed by
this project and whether the deployed bytes are really verified. No ssh: host locks are DB-only and
deployments are inserted straight into the registry.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from s2docs import evidence, hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.orchestration.engine import GateFailed, Orchestrator
from s2docs.orchestration.workflows import WORKFLOWS


@pytest.fixture
def ldb(tmp_path) -> Database:
    db = Database(tmp_path / "live.db")
    hosts.register_host(db, "desktop-pc", "pompa@100.64.130.72", shell="powershell")
    return db


@pytest.fixture
def orch(ldb, tmp_path) -> Orchestrator:
    return Orchestrator(ldb, tmp_path / "ws")


INPUTS = {"host_id": "desktop-pc", "deploy_manifest": "addon/*.vpk", "test_description": "the trigger fires"}


def _start(orch) -> str:
    return orch.start("cs2_live_test", "smoke the addon on desktop-pc", inputs=INPUTS).project_id


def _insert_deploy(db: Database, did: str, status: str, host_id: str = "desktop-pc") -> str:
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO deployments (id, host_id, dest_root, entries, status) VALUES (?, ?, 'C:/dst', '[]', ?)",
            (did, host_id, status),
        )
    return did


def test_workflow_registered_and_inputs_rendered(orch):
    assert "cs2_live_test" in WORKFLOWS
    brief = orch.start("cs2_live_test", "smoke it", inputs=INPUTS)
    assert [s.step_id for s in brief.next_steps] == ["claim"]
    # workflow-specific inputs and the generated project id render into the instructions
    text = brief.next_steps[0].instructions
    assert "desktop-pc" in text and f"project:{brief.project_id}" in text


def test_missing_required_inputs(orch):
    from s2docs.orchestration.engine import OrchestrationError

    with pytest.raises(OrchestrationError, match="missing inputs"):
        orch.start("cs2_live_test", "smoke it", inputs={"host_id": "desktop-pc"})


def test_claim_gate(orch, ldb):
    pid = _start(orch)
    # no lock at all
    with pytest.raises(GateFailed, match="not locked"):
        orch.update(pid, "claim", "done", notes="did it")
    # someone else holds it
    hosts.claim_host(ldb, "desktop-pc", "ws/other")
    with pytest.raises(GateFailed, match="locked by"):
        orch.update(pid, "claim", "done", notes="did it")
    # the project claims it → gate passes
    hosts.release_host(ldb, "desktop-pc", "ws/other", force=True)
    hosts.claim_host(ldb, "desktop-pc", f"project:{pid}")
    brief = orch.update(pid, "claim", "done", notes="claimed")
    assert [s.step_id for s in brief.next_steps] == ["probe"]


def test_deploy_gate(orch, ldb):
    pid = _start(orch)
    hosts.claim_host(ldb, "desktop-pc", f"project:{pid}")
    orch.update(pid, "claim", "done", notes="ok")
    orch.update(pid, "probe", "done", notes="probed")
    # nothing deployed yet
    with pytest.raises(GateFailed, match="no deployment"):
        orch.update(pid, "deploy", "done", notes="pushed")
    # a pushed-but-unverified deployment (latest-for-host fallback) does not pass
    _insert_deploy(ldb, "d-00000001", "pushed")
    with pytest.raises(GateFailed, match="not 'verified'"):
        orch.update(pid, "deploy", "done", notes="pushed")
    _insert_deploy(ldb, "d-00000002", "verified")
    # the id named in the artifacts is checked, NOT the host's latest (verified) deployment
    with pytest.raises(GateFailed, match="not 'verified'"):
        orch.update(pid, "deploy", "done", notes="x", artifacts=["d-00000001"])
    brief = orch.update(pid, "deploy", "done", notes="verified", artifacts=["d-00000002"])
    assert [s.step_id for s in brief.next_steps] == ["test"]


def test_test_gate_and_full_flow(orch, ldb):
    pid = _start(orch)
    hosts.claim_host(ldb, "desktop-pc", f"project:{pid}")
    orch.update(pid, "claim", "done", notes="ok")
    orch.update(pid, "probe", "done", notes="probed")
    _insert_deploy(ldb, "d-00000003", "verified")
    orch.update(pid, "deploy", "done", notes="verified", artifacts=["d-00000003"])
    # evidence gate: notes alone don't pass
    with pytest.raises(GateFailed, match="add_evidence"):
        orch.update(pid, "test", "done", notes="ran it")
    evidence.add(ldb, kind="console_transcript", project_id=pid, step_id="test", data={"pass": 1})
    brief = orch.update(pid, "test", "done", notes="ran", artifacts=["transcript.log"])
    assert [s.step_id for s in brief.next_steps] == ["release"]
    # release the host and finish
    hosts.release_host(ldb, "desktop-pc", f"project:{pid}")
    brief = orch.update(pid, "release", "done", notes="released")
    assert brief.status == "done" and brief.progress == "5/5"
    assert hosts.get_host(ldb, "desktop-pc").lock is None


def test_live_test_workflow_over_http(ldb, tmp_path):
    with TestClient(create_app(ldb.path, workspace=tmp_path / "ws")) as c:
        workflows = {w["id"] for w in c.get("/api/workflows").json()}
        assert "cs2_live_test" in workflows
        r = c.post("/api/projects", json={"workflow": "cs2_live_test", "goal": "smoke", "inputs": INPUTS})
        assert r.status_code == 201
        pid = r.json()["project_id"]
        # the claim gate is enforced server-side (409 with the gate flag), not trusted from the client
        r = c.post(f"/api/projects/{pid}/steps/claim", json={"status": "done", "notes": "did it"})
        assert r.status_code == 409 and r.json()["gate_failed"]
        hosts.claim_host(ldb, "desktop-pc", f"project:{pid}")
        r = c.post(f"/api/projects/{pid}/steps/claim", json={"status": "done", "notes": "claimed"})
        assert r.status_code == 200 and r.json()["next_steps"][0]["step_id"] == "probe"
