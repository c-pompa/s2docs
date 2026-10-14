"""cs2_live_test workflow + its two new gates (host_claimed, deploy_verified).

The gates are the point: the server, not the caller, decides whether the host is really claimed by
this project and whether the deployed bytes are really verified. No ssh: host locks are DB-only and
deployments are inserted straight into the registry.
"""

from __future__ import annotations

import base64
import re

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import deploys, evidence, hosts
from s2docs.api.app import create_app
from s2docs.builds import md5_file
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server
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
    # no deploy id artifact -> the gate needs one (no latest-for-host fallback any more)
    with pytest.raises(GateFailed, match="artifact"):
        orch.update(pid, "deploy", "done", notes="pushed")
    # a pushed-but-unverified deployment named as the artifact does not pass
    _insert_deploy(ldb, "d-00000001", "pushed")
    with pytest.raises(GateFailed, match="not 'verified'"):
        orch.update(pid, "deploy", "done", notes="pushed", artifacts=["d-00000001"])
    # the host's latest (verified) deployment is NOT used: the id must still be named
    _insert_deploy(ldb, "d-00000002", "verified")
    with pytest.raises(GateFailed, match="artifact"):
        orch.update(pid, "deploy", "done", notes="pushed")
    # an unknown id is reported as missing
    with pytest.raises(GateFailed, match="no deployment"):
        orch.update(pid, "deploy", "done", notes="x", artifacts=["d-deadbeef"])
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


# --------------------------------------------- deploy step vs the project lease
# The blocker: the claim step makes the project hold the host lease as `project:<id>` for the whole
# run, so an agent that follows the deploy step must hand that owner to deploy_push — otherwise the
# push's own lease arbitration (deploys._take_lease) sees a fresh foreign lock and raises HostLocked.
class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _remote_hashes(argv: str, mapping: dict[str, str]) -> str:
    encoded = argv.rsplit(" ", 1)[-1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    paths = [m.replace("''", "'") for m in re.findall(r"'((?:[^']|'')*)'", script)]
    return "\n".join(f"{mapping.get(p, 'missing')}|{p}" for p in paths)


def _fake_ssh(mapping: dict[str, str]):
    def _run(argv, **_kw):
        if argv[0] == "scp":
            return _Result(returncode=0)
        return _Result(returncode=0, stdout=_remote_hashes(argv[-1], mapping))

    return _run


def _deploy_instructions(orch, pid: str) -> str:
    with orch.db.reader() as conn:
        row = conn.execute(
            "SELECT instructions FROM project_steps WHERE project_id = ? AND step_id = 'deploy'", (pid,)
        ).fetchone()
    return row["instructions"]


def test_deploy_step_instruction_names_the_project_owner(orch):
    """The rendered deploy brief must tell the agent the owner to pass, using the project convention."""
    pid = _start(orch)
    text = _deploy_instructions(orch, pid)
    assert f"project:{pid}" in text  # the workflow's own variable syntax, rendered
    assert "$project_id" not in text  # no unsubstituted template left
    assert "owner" in text.lower()


def test_deploy_push_without_owner_conflicts_with_the_project_lease(orch, ldb, tmp_path, monkeypatch):
    """Regression for the blocker: no `owner` while the project holds the lease is HostLocked (409)."""
    pid = _start(orch)
    hosts.claim_host(ldb, "desktop-pc", f"project:{pid}")
    local = tmp_path / "iso.js"
    local.write_bytes(b"console.log(1)\n")
    monkeypatch.setattr(deploys.subprocess, "run", _fake_ssh({}))
    with pytest.raises(hosts.HostLocked):
        deploys.push(ldb, "desktop-pc", "C:/dst", [{"path": "iso.js", "local": str(local)}])


def test_deploy_step_happy_path_passes_the_gate_end_to_end(orch, ldb, tmp_path, monkeypatch):
    """Drive claim -> deploy (fake transport, owner as the step says) -> deploy_verified, and pass.

    The push runs through the MCP `deploy_push` tool exactly as an agent would call it, with the
    owner the deploy instruction names; the gate then accepts the returned id as the step artifact.
    """
    pid = _start(orch)
    owner = f"project:{pid}"
    hosts.claim_host(ldb, "desktop-pc", owner)
    orch.update(pid, "claim", "done", notes="claimed")
    orch.update(pid, "probe", "done", notes="probed")
    # the owner the instruction tells the agent to pass is in the rendered brief
    assert owner in _deploy_instructions(orch, pid)

    local = tmp_path / "iso.js"
    local.write_bytes(b"console.log(1)\n")
    remote = "C:/dst/iso.js"
    monkeypatch.setattr(deploys.subprocess, "run", _fake_ssh({remote: md5_file(local)}))

    server = create_mcp_server(ldb)

    async def main():
        async with Client(server) as c:
            return await c.call_tool(
                "deploy_push",
                {
                    "host_id": "desktop-pc",
                    "dest_root": "C:/dst",
                    "files": [{"path": "iso.js", "local": str(local)}],
                    "owner": owner,
                },
            )

    result = anyio.run(main)
    pushed = result.structured_content
    assert pushed["status"] == "verified" and pushed["owner"] == owner

    brief = orch.update(pid, "deploy", "done", notes="verified", artifacts=[pushed["id"]])
    assert [s.step_id for s in brief.next_steps] == ["test"]
    # the push reused the project's lease and left it locked for the test step
    lock = hosts.get_host(ldb, "desktop-pc").lock
    assert lock is not None and lock.owner == owner

