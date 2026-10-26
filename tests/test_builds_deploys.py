"""Build runs + deployment registry: run lifecycle, UTF-16 logs, MD5 verification, all three facades.

No real host is touched: the ssh/scp layer (subprocess) is faked, exactly as tests/test_hosts.py
fakes the probe.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import builds, deploys, hosts
from s2docs.api.app import create_app
from s2docs.builds import md5_file
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server


@pytest.fixture
def bdb(tmp_path) -> Database:
    db = Database(tmp_path / "builds.db")
    hosts.register_host(db, "build-pc", "pompa@192.0.2.172", hostname="DESKTOP-BUILDPC", shell="powershell")
    return db


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ------------------------------------------------------------------ build fakes
def fake_popen(payload: bytes = b"", rc: int = 0, hang: bool = False, record: list | None = None, gate=None):
    """A subprocess.Popen stand-in: writes `payload` to the stdout file, then 'finishes'.

    With `gate` (a threading.Event) the process stays alive until the test sets it,
    removing the race where the watcher finalizes the run before the test asserts.
    """

    class _P:
        def __init__(self, argv, stdout=None, stderr=None, **_kw):
            self.argv = argv
            self.pid = 4242
            self._rc = rc
            self._done = not hang and gate is None
            self.terminated = False
            if record is not None:
                record.append(argv)
            if stdout is not None:
                stdout.write(payload)
                stdout.flush()

        def poll(self):
            if self._done:
                return self._rc
            if gate is not None and gate.is_set():
                self._done = True
                return self._rc
            return None

        def wait(self, timeout=None):
            return self._rc

        def terminate(self):
            self._done = True
            self.terminated = True

        def kill(self):
            self.terminate()

    return _P


def _start(bdb, tmp_path, **kw):
    # The log path is derived server-side (inside the data dir); a client cannot point it elsewhere.
    return builds.start(bdb, "build-pc", "resourcecompiler -game csgo -nop4 addon", **kw)


# ------------------------------------------------------------------- build runs
def _lease_released(db, host_id: str) -> bool:
    """The host must be free the instant the run reads terminal.

    Status and lease are written in one transaction (builds._release_in_tx), so there is no window
    to poll for. This deliberately does NOT sleep: if it ever needs a grace period again, the
    atomicity in builds._finalize / builds.cancel has been lost, and the failure mode is the next
    build or deploy on that host answering 409.
    """
    return hosts.get_host(db, host_id).lock is None


def test_build_start_success(bdb, tmp_path, monkeypatch):
    payload = "Compile Summary: 1234 compiled, 2 failed, 0 warnings\r\nDone.\r\n".encode("utf-16")
    gate = threading.Event()
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=payload, rc=0, gate=gate))
    run = _start(bdb, tmp_path)
    assert run.status == "running" and run.handle == "4242"
    gate.set()  # let the fake compiler exit; the watcher then finalizes the run
    finished = builds.wait(bdb, run.id, timeout_sec=5, poll_sec=0.02)
    assert finished.status == "succeeded" and finished.exit_code == 0
    assert finished.summary == {"compiled": 1234, "failed": 2, "warnings": 0}
    # the server released the host lease on the terminal status
    assert _lease_released(bdb, "build-pc")
    # the UTF-16 log decodes (this is what broke the hand-rolled probes)
    tail = builds.log_tail(bdb, run.id)
    assert tail.encoding == "utf-16" and any("1234 compiled" in ln for ln in tail.lines)


def test_build_start_failure_exit_code(bdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload="boom\r\n".encode("utf-16"), rc=1))
    run = builds.wait(bdb, _start(bdb, tmp_path).id, timeout_sec=5, poll_sec=0.02)
    assert run.status == "failed" and run.exit_code == 1
    assert _lease_released(bdb, "build-pc")


def test_build_start_locked_is_server_side(bdb, tmp_path, monkeypatch):
    hosts.claim_host(bdb, "build-pc", "ws/other")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(rc=0))
    with pytest.raises(hosts.HostLocked):
        _start(bdb, tmp_path)
    assert builds.list_runs(bdb) == []  # nothing recorded when the lease can't be taken


def test_build_start_refuses_when_host_already_has_a_running_run(bdb, tmp_path, monkeypatch):
    """One active build run per host: even if the lease looks free (a stale/expired holder), a second
    build_start must not launch a competing compile on the same host."""
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(hang=True))
    first = _start(bdb, tmp_path)
    assert first.status == "running"
    # simulate a lease that expired/freed while the run record is still 'running'
    hosts.release_host(bdb, "build-pc", f"build:{first.id}")
    with pytest.raises(builds.BuildError, match="running build"):
        _start(bdb, tmp_path)
    assert [r.id for r in builds.list_runs(bdb, active=True)] == [first.id]
    builds.cancel(bdb, first.id)
    builds.wait(bdb, first.id, timeout_sec=5, poll_sec=0.02)


def test_build_cancel_terminates_and_releases(bdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(hang=True))
    run = _start(bdb, tmp_path)
    cancelled = builds.cancel(bdb, run.id)
    assert cancelled.status == "cancelled"
    builds.wait(bdb, run.id, timeout_sec=5, poll_sec=0.02)
    assert hosts.get_host(bdb, "build-pc").lock is None


def test_build_wait_timeout(bdb):
    with bdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner) "
            "VALUES ('b-manual', 'build-pc', 'build', 'x', 'running', 'build:b-manual')"
        )
    with pytest.raises(builds.BuildTimeout):
        builds.wait(bdb, "b-manual", timeout_sec=0.2, poll_sec=0.05)


def test_build_list_filters(bdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(rc=0))
    a = builds.wait(bdb, _start(bdb, tmp_path).id, timeout_sec=5, poll_sec=0.02)
    b = builds.wait(bdb, _start(bdb, tmp_path).id, timeout_sec=5, poll_sec=0.02)
    assert {r.id for r in builds.list_runs(bdb)} == {a.id, b.id}
    assert builds.list_runs(bdb, active=True) == []
    assert [r.id for r in builds.list_runs(bdb, host_id="nope")] == []


def test_build_missing_run(bdb):
    from s2docs.db.repository import NotFound

    with pytest.raises(NotFound):
        builds.get_run(bdb, "b-nope")


def test_parse_summary_unit():
    assert builds.parse_summary("10 compiled, 1 failed") == {"compiled": 10, "failed": 1}
    assert builds.parse_summary("5,000 compiled, 0 errors, 3 warnings") == {
        "compiled": 5000,
        "errors": 0,
        "warnings": 3,
    }
    assert builds.parse_summary("no counts here") == {}


def test_log_tail_utf16_no_bom(bdb, tmp_path):
    log = tmp_path / "no_bom.log"
    log.write_bytes("alpha\r\nbeta\r\ngamma\r\n".encode("utf-16-le"))  # NUL-heavy, no BOM
    with bdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, log_path) "
            "VALUES ('b-log', 'build-pc', 'build', 'x', 'running', 'build:b-log', ?)",
            (str(log),),
        )
    tail = builds.log_tail(bdb, "b-log", lines=2)
    assert tail.encoding == "utf-16" and tail.truncated
    assert tail.lines == ["beta", "gamma"]


def test_record_artifacts_hashes_local_files(bdb, tmp_path):
    with bdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner) "
            "VALUES ('b-art', 'build-pc', 'build', 'x', 'succeeded', 'build:b-art')"
        )
    art = tmp_path / "addon.vpk"
    art.write_bytes(b"payload")
    run = builds.record_artifacts(bdb, "b-art", [str(art), "C:/remote/other.vpk"])
    assert run.artifacts[0].md5 == hashlib.md5(b"payload").hexdigest() and run.artifacts[0].size == 7
    assert run.artifacts[1].md5 is None  # remote path recorded by name only


# ------------------------------------------------------------------ deploy fakes
def _remote_hashes(script_argv: str, mapping: dict[str, str]) -> str:
    """Decode the EncodedCommand PowerShell and return '<hash>|<path>' lines for the paths in it."""
    encoded = script_argv.rsplit(" ", 1)[-1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    paths = [m.replace("''", "'") for m in re.findall(r"'((?:[^']|'')*)'", script)]
    return "\n".join(f"{mapping.get(p, 'missing')}|{p}" for p in paths)


def fake_ssh(mapping: dict[str, str], scp_rc: int = 0, fail_scp: bool = False, calls: list | None = None):
    def _run(argv, **_kw):
        if calls is not None:
            calls.append(argv)
        if argv[0] == "scp":
            if fail_scp:
                return _Result(returncode=1, stderr="permission denied")
            return _Result(returncode=scp_rc)
        return _Result(returncode=0, stdout=_remote_hashes(argv[-1], mapping))

    return _run


def _local(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_deploy_push_verifies_md5(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "jump_arena.js", b"console.log(1)\n")
    dest = "C:/iso_build/addons"
    remote = "C:/iso_build/addons/scripts/jump_arena.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    deploy = deploys.push(bdb, "build-pc", dest, [{"path": "scripts/jump_arena.js", "local": str(local)}])
    assert deploy.status == "verified"
    entry = deploy.entries[0]
    assert entry.verified and entry.md5 == md5_file(local) and entry.size == local.stat().st_size
    assert [d.id for d in deploys.list_deployments(bdb, host_id="build-pc")] == [deploy.id]


def test_deploy_push_mismatch_fails(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "cfg.cfg", b"sv_cheats 1\n")
    dest = "C:/dst"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({"C:/dst/cfg/a.cfg": "0" * 32}))
    with pytest.raises(deploys.DeployError):
        deploys.push(bdb, "build-pc", dest, [{"path": "cfg/a.cfg", "local": str(local)}])
    recorded = deploys.list_deployments(bdb)[0]
    assert recorded.status == "failed" and not recorded.entries[0].verified


def test_deploy_push_dry_run_does_not_copy(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "m.txt", b"x")
    calls: list = []
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({}, calls=calls))
    deploy = deploys.push(bdb, "build-pc", "C:/dst", [{"path": "m.txt", "local": str(local)}], dry_run=True)
    assert deploy.id.startswith("d-") and deploy.notes == "dry-run: nothing copied"
    assert calls == [] and deploys.list_deployments(bdb) == []


def test_deploy_push_partial_failure_returns_record(bdb, tmp_path, monkeypatch):
    good = _local(tmp_path, "good.txt", b"good")
    bad = _local(tmp_path, "bad.txt", b"bad")
    dest = "C:/dst"

    def flaky(argv, **_kw):
        if argv[0] == "scp" and argv[-2].endswith("bad.txt"):
            return _Result(returncode=1, stderr="no")
        if argv[0] == "scp":
            return _Result(returncode=0)
        return _Result(returncode=0, stdout=_remote_hashes(argv[-1], {f"{dest}/good.txt": md5_file(good)}))

    monkeypatch.setattr(deploys.subprocess, "run", flaky)
    deploy = deploys.push(
        bdb,
        "build-pc",
        dest,
        [{"path": "good.txt", "local": str(good)}, {"path": "bad.txt", "local": str(bad)}],
    )
    assert deploy.status == "failed" and "copy errors" in deploy.notes


def test_deploy_verify_recheck(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "v.txt", b"verify me")
    dest = "C:/dst"
    remote = f"{dest}/v.txt"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    deploy = deploys.push(bdb, "build-pc", dest, [{"path": "v.txt", "local": str(local)}])
    assert deploy.status == "verified"
    # now the host copy drifts
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: "deadbeef" * 4}))
    rechecked = deploys.verify(bdb, deploy.id)
    assert rechecked.status == "failed" and not rechecked.entries[0].verified


def test_deploy_missing_local_file(bdb):
    with pytest.raises(deploys.DeployError):
        deploys.push(bdb, "build-pc", "C:/dst", [{"path": "x", "local": "/no/such/file"}])


# ------------------------------------------------------------- deploy leases
def test_deploy_push_blocked_by_another_owner_copies_nothing(bdb, tmp_path, monkeypatch):
    """A host a fresh lock already holds is a conflict: no lease is taken, nothing is copied."""
    local = _local(tmp_path, "a.js", b"code")
    calls: list = []
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({}, calls=calls))
    hosts.claim_host(bdb, "build-pc", "ws/other")
    with pytest.raises(hosts.HostLocked):
        deploys.push(bdb, "build-pc", "C:/dst", [{"path": "a.js", "local": str(local)}])
    assert calls == []  # the copy never ran
    assert hosts.get_host(bdb, "build-pc").lock.owner == "ws/other"  # untouched
    assert deploys.list_deployments(bdb) == []


def test_deploy_push_free_host_takes_and_releases_the_lease(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "b.js", b"code")
    remote = "C:/dst/b.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    seen: dict = {}
    real_claim = hosts.claim_host

    def spy_claim(db, host_id, owner, **kw):
        seen["owner"] = owner
        return real_claim(db, host_id, owner, **kw)

    monkeypatch.setattr(deploys.hosts, "claim_host", spy_claim)
    deploy = deploys.push(bdb, "build-pc", "C:/dst", [{"path": "b.js", "local": str(local)}])
    assert deploy.status == "verified"
    assert seen["owner"].startswith("deploy:") and deploy.owner == seen["owner"]
    assert hosts.get_host(bdb, "build-pc").lock is None  # released after the copy window


def test_deploy_push_owner_holding_the_lease_proceeds_without_releasing(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "c.js", b"code")
    remote = "C:/dst/c.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    hosts.claim_host(bdb, "build-pc", "ws/tester")
    deploy = deploys.push(
        bdb, "build-pc", "C:/dst", [{"path": "c.js", "local": str(local)}], owner="ws/tester"
    )
    assert deploy.status == "verified" and deploy.owner == "ws/tester"
    lock = hosts.get_host(bdb, "build-pc").lock
    assert lock is not None and lock.owner == "ws/tester"  # the caller's lease is kept


def test_deploy_push_releases_the_lease_even_on_failure(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "d.js", b"code")
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({}, fail_scp=True))
    with pytest.raises(deploys.DeployError):
        deploys.push(bdb, "build-pc", "C:/dst", [{"path": "d.js", "local": str(local)}])
    assert hosts.get_host(bdb, "build-pc").lock is None


# ------------------------------------------------------------------ facades: MCP
def test_mcp_build_and_deploy_flow(bdb, tmp_path, monkeypatch):
    payload = "7 compiled, 0 failed\r\n".encode("utf-16")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=payload, rc=0))
    local = _local(tmp_path, "s.js", b"code")
    remote = "C:/dst/s.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    server = create_mcp_server(bdb)

    async def main():
        async with Client(server) as c:
            started = await c.call_tool("build_start", {"host_id": "build-pc", "command": "compile"})
            run_id = started.structured_content["id"]
            waited = await c.call_tool("build_wait", {"run_id": run_id, "timeout_sec": 5})
            log = await c.call_tool("build_log", {"run_id": run_id})
            listed = await c.call_tool("build_list", {})
            pushed = await c.call_tool(
                "deploy_push",
                {"host_id": "build-pc", "dest_root": "C:/dst", "files": [{"path": "s.js", "local": str(local)}]},
            )
            verified = await c.call_tool("deploy_verify", {"deploy_id": pushed.structured_content["id"]})
            deployment_list = await c.call_tool("deploy_list", {})
            return started, waited, log, listed, pushed, verified, deployment_list

    started, waited, log, listed, pushed, verified, dlist = anyio.run(main)
    assert not started.is_error and waited.structured_content["status"] == "succeeded"
    assert waited.structured_content["summary"] == {"compiled": 7, "failed": 0}
    assert log.structured_content["encoding"] == "utf-16"
    assert listed.structured_content["result"][0]["id"] == started.structured_content["id"]
    assert pushed.structured_content["status"] == "verified"
    assert verified.structured_content["status"] == "verified"
    assert dlist.structured_content["result"][0]["id"] == pushed.structured_content["id"]


def test_mcp_build_start_unknown_host(bdb):
    server = create_mcp_server(bdb)

    async def main():
        async with Client(server) as c:
            return await c.call_tool("build_start", {"host_id": "ghost", "command": "x"})

    result = anyio.run(main)
    assert result.is_error and "unknown host" in result.content[0].text


def test_mcp_deploy_push_respects_the_host_lease(bdb, tmp_path, monkeypatch):
    local = _local(tmp_path, "m.js", b"code")
    remote = "C:/dst/m.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))
    hosts.claim_host(bdb, "build-pc", "ws/tester")
    server = create_mcp_server(bdb)

    async def main():
        async with Client(server) as c:
            blocked = await c.call_tool(
                "deploy_push",
                {"host_id": "build-pc", "dest_root": "C:/dst", "files": [{"path": "m.js", "local": str(local)}]},
            )
            ok = await c.call_tool(
                "deploy_push",
                {
                    "host_id": "build-pc",
                    "dest_root": "C:/dst",
                    "files": [{"path": "m.js", "local": str(local)}],
                    "owner": "ws/tester",
                },
            )
            return blocked, ok

    blocked, ok = anyio.run(main)
    assert blocked.is_error and "locked" in blocked.content[0].text.lower()
    assert not ok.is_error and ok.structured_content["status"] == "verified"
    assert ok.structured_content["owner"] == "ws/tester"


# ------------------------------------------------------------------ facades: HTTP
@pytest.fixture
def client(bdb):
    with TestClient(create_app(bdb.path)) as c:
        yield c


def test_api_build_and_deploy_routes(bdb, client, tmp_path, monkeypatch):
    payload = "3 compiled, 1 failed\r\n".encode("utf-16")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=payload, rc=1))
    local = _local(tmp_path, "a.js", b"abc")
    remote = "C:/dst/a.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))

    started = client.post("/api/hosts/build-pc/builds", json={"command": "compile"})
    assert started.status_code == 201
    run_id = started.json()["id"]
    assert client.get(f"/api/builds/{run_id}/log").json()["encoding"] == "utf-16"
    assert client.post(f"/api/builds/{run_id}/wait", json={"timeout_sec": 5}).json()["status"] == "failed"
    assert client.get("/api/builds", params={"host_id": "build-pc"}).json()[0]["id"] == run_id
    assert client.get(f"/api/builds/{run_id}").json()["summary"]["compiled"] == 3

    pushed = client.post(
        "/api/deploys",
        json={"host_id": "build-pc", "dest_root": "C:/dst", "files": [{"path": "a.js", "local": str(local)}]},
    )
    assert pushed.status_code == 201 and pushed.json()["status"] == "verified"
    assert client.post(f"/api/deploys/{pushed.json()['id']}/verify").json()["status"] == "verified"
    assert client.get("/api/deploys").json()[0]["id"] == pushed.json()["id"]


def test_api_build_locked_is_409(bdb, client, monkeypatch):
    hosts.claim_host(bdb, "build-pc", "ws/other")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(rc=0))
    r = client.post("/api/hosts/build-pc/builds", json={"command": "compile"})
    assert r.status_code == 409 and r.json()["host_locked"]


def test_api_deploy_locked_is_409(bdb, client, tmp_path, monkeypatch):
    local = _local(tmp_path, "z.js", b"x")
    calls: list = []
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({}, calls=calls))
    hosts.claim_host(bdb, "build-pc", "ws/other")
    r = client.post(
        "/api/deploys",
        json={"host_id": "build-pc", "dest_root": "C:/dst", "files": [{"path": "z.js", "local": str(local)}]},
    )
    assert r.status_code == 409 and r.json()["host_locked"]
    assert calls == [] and deploys.list_deployments(bdb) == []


# ------------------------------------------------------------------ facades: CLI
def _run_cli(monkeypatch, tmp_path, argv):
    """Run the CLI against a scratch data dir containing our db, with ssh faked."""
    from s2docs import cli

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    from s2docs.config import get_settings

    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_build_and_deploy(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")
    payload = "9 compiled, 0 failed\r\n".encode("utf-16")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=payload, rc=0))
    local = _local(tmp_path, "c.js", b"code")
    remote = "C:/dst/c.js"
    monkeypatch.setattr(deploys.subprocess, "run", fake_ssh({remote: md5_file(local)}))

    rc = _run_cli(
        monkeypatch,
        tmp_path,
        ["--json", "build", "start", "my_addon", "--host", "build-pc", "--command", "compile", "--wait"],
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert '"schema": "s2docs/v1"' in out and '"status": "succeeded"' in out

    rc = _run_cli(
        monkeypatch,
        tmp_path,
        ["--json", "build", "list", "--host", "build-pc"],
    )
    assert rc == 0 and '"runs"' in capsys.readouterr().out


def test_cli_locked_exit_code(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "build-pc", "pompa@x")
    hosts.claim_host(db, "build-pc", "ws/other")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(rc=0))
    rc = _run_cli(monkeypatch, tmp_path, ["build", "start", "a", "--host", "build-pc", "--command", "x"])
    assert rc == 2 and "LOCKED" in capsys.readouterr().err


def test_cli_not_found_exit_code(tmp_path, monkeypatch, capsys):
    with Database(tmp_path / "data" / "s2docs.db").writer() as conn, conn:
        pass  # create + migrate the db so the read path finds the build_runs table
    rc = _run_cli(monkeypatch, tmp_path, ["build", "status", "b-missing"])
    assert rc == 3 and "no build run" in capsys.readouterr().err


# -------------------------------------------------------- material guard
# The 2026-10-12 de_dust2_iso incident: a rebuild finished (exit 0) but referenced missing/reserved
# materials; the map then error-rendered as a red/black checkerboard. Logs are UTF-16 like the real
# resourcecompiler log — no real compile is run.
_INCIDENT_LINES = [
    "Compiling addon iso_build...",
    'ERROR: Material "maps/de_dust2/_vrf/physics_surfaces/tile_10_nodraw.vmat" referencing missing material',
    'ERROR: Material "materials/custom/wall.vmat" referencing missing material',
    "Failed to compile maps/de_dust2/_vrf/physics_surfaces/tile_10_nodraw.vmat",
    "Compile Summary: 1164 compiled, 15 failed, 0 warnings",
    "Done.",
]


def _utf16_log(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-16")


def test_scan_materials_incident_class_unit():
    guard = builds.scan_materials("\n".join(_INCIDENT_LINES), map_name="de_dust2_iso")
    assert guard["clean"] is False and guard["map"] == "de_dust2_iso"
    assert guard["counts"]["missing_material"] == 2
    assert guard["counts"]["failed_compile"] == 1
    assert guard["counts"]["reserved_path"] >= 1
    assert any("_vrf/" in p for p in guard["paths"]["reserved_path"])
    assert any("wall.vmat" in p for p in guard["paths"]["missing_material"])
    # a clean log reports nothing
    clean = builds.scan_materials("2000 compiled, 0 failed", map_name="de_dust2_iso")
    assert clean == {"clean": True, "map": "de_dust2_iso"}
    # the runtime's *own* map subtree is legal; another map's is reserved
    assert builds.scan_materials("ref maps/de_dust2_iso/models/rock.vmdl", map_name="de_dust2_iso")["clean"] is True
    foreign = builds.scan_materials("ref maps/de_dust2/_vrf/x.vmat", map_name="de_dust2_iso")
    assert foreign["clean"] is False
    assert builds.map_name_from_notes("target=iso map=de_dust2_iso") == "de_dust2_iso"
    assert builds.map_name_from_notes(None, "resourcecompiler map=aim_map addon") == "aim_map"


def test_build_material_warnings_surface_in_summary(bdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=_utf16_log(_INCIDENT_LINES), rc=0))
    run = builds.start(
        bdb,
        "build-pc",
        "resourcecompiler -game csgo addon",
        notes="target=de_dust2_iso map=de_dust2_iso",
    )
    finished = builds.wait(bdb, run.id, timeout_sec=5, poll_sec=0.02)
    # a material warning is NOT a failed run: the resourcecompiler exits 0 (its own semantics)
    assert finished.status == "succeeded" and finished.exit_code == 0
    assert finished.summary["compiled"] == 1164 and finished.summary["failed"] == 15
    guard = finished.summary["material_guard"]
    assert guard["counts"]["missing_material"] == 2 and guard["counts"]["failed_compile"] == 1
    assert finished.warnings is not None and not finished.warnings.clean
    assert builds.has_material_warnings(finished)
    # build_log surfaces the same findings over the full log
    tail = builds.log_tail(bdb, run.id, lines=2_000_000)
    assert tail.warnings is not None and tail.warnings.counts["reserved_path"] >= 1
    # a genuinely clean run has no warnings field at all (existing behavior preserved)
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=_utf16_log(["7 compiled, 0 failed"]), rc=0))
    clean_run = builds.wait(bdb, _start(bdb, tmp_path).id, timeout_sec=5, poll_sec=0.02)
    assert clean_run.warnings is None and "material_guard" not in clean_run.summary


def test_cli_strict_materials_exit_code(tmp_path, monkeypatch, capsys):
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=_utf16_log(_INCIDENT_LINES), rc=0))
    base = [
        "--json",
        "build",
        "start",
        "iso",
        "--host",
        "build-pc",
        "--command",
        "compile",
        "--map",
        "de_dust2_iso",
        "--wait",
    ]

    # default: finishes 'succeeded' but the warnings are surfaced in the JSON summary
    rc = _run_cli(monkeypatch, tmp_path, base)
    assert rc == 0
    out = capsys.readouterr().out
    assert '"status": "succeeded"' in out and '"material_guard"' in out and '"reserved_path"' in out

    # --strict-materials turns the same run into a non-zero exit (8 = material warnings)
    rc = _run_cli(monkeypatch, tmp_path, [*base, "--strict-materials"])
    assert rc == 8 and "material guard" in capsys.readouterr().err


def test_api_build_material_guard_surfaces(bdb, client, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=_utf16_log(_INCIDENT_LINES), rc=0))
    started = client.post("/api/hosts/build-pc/builds", json={"command": "compile", "notes": "map=de_dust2_iso"})
    assert started.status_code == 201
    run_id = started.json()["id"]
    assert client.post(f"/api/builds/{run_id}/wait", json={"timeout_sec": 5}).json()["status"] == "succeeded"
    record = client.get(f"/api/builds/{run_id}").json()
    assert record["summary"]["material_guard"]["counts"]["missing_material"] == 2
    assert record["warnings"]["counts"]["reserved_path"] >= 1
    log = client.get(f"/api/builds/{run_id}/log").json()
    assert log["warnings"]["counts"]["failed_compile"] == 1


def test_mcp_material_guard_surfaces_and_strict(bdb, tmp_path, monkeypatch):
    monkeypatch.setattr(builds.subprocess, "Popen", fake_popen(payload=_utf16_log(_INCIDENT_LINES), rc=0))
    server = create_mcp_server(bdb)

    async def main():
        async with Client(server) as c:
            started = await c.call_tool("build_start", {"host_id": "build-pc", "command": "compile"})
            rid = started.structured_content["id"]
            waited = await c.call_tool("build_wait", {"run_id": rid, "timeout_sec": 5})
            status = await c.call_tool("build_status", {"run_id": rid})
            log = await c.call_tool("build_log", {"run_id": rid})
            strict = await c.call_tool("build_wait", {"run_id": rid, "timeout_sec": 5, "strict_materials": True})
            return waited, status, log, strict

    waited, status, log, strict = anyio.run(main)
    assert waited.structured_content["status"] == "succeeded"  # still 'succeeded' without strict
    assert status.structured_content["warnings"]["counts"]["missing_material"] == 2
    assert log.structured_content["warnings"]["counts"]["failed_compile"] == 1
    assert strict.is_error and "material guard" in strict.content[0].text
