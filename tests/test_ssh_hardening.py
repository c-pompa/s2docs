"""An ssh_target can never become an ssh or scp option.

Review item 1 on NousResearch/hermes-agent#143393 (teknium1, review 5631127162): ``register_host``
stored the model-supplied ``ssh_target`` verbatim, and every ssh/scp call put it as a bare argument
before the remote command. A target such as ``-oProxyCommand=...`` is read as an option there, so a
``register_host`` followed by ``probe_host``, ``build_start`` or ``deploy_push`` ran that command on
the machine the s2docs server runs on. These tests pin the three layers of the fix:

1. registration refuses anything that is not an ssh alias or ``[user@]host[:port]``;
2. every ssh/scp argv comes from ``hosts.ssh_argv`` / ``hosts.scp_argv``, which re-validate the
   stored target (a row written before the fix is refused at use time) and end option parsing with
   ``--`` right before the destination;
3. scp's ``host:path`` operand therefore never starts with ``-``, and the local operand is absolute.

``test_register_host_rejects_unsafe_ssh_target`` is the regression test the reviewer asked for; the
hermes-pr-feedback-ci rule HPF-004 (ci/feedback-rules.yaml) runs it by name and requires the
``-oProxyCommand=`` case, so keep both. No real host is touched: subprocess is faked throughout.
"""

from __future__ import annotations

import threading
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import builds, cli, deploys, hosts
from s2docs.api.app import create_app
from s2docs.builds import md5_file
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server

REVIEWER_TARGET = "-oProxyCommand=touch /tmp/s2docs-pwned"

UNSAFE_TARGETS = [
    REVIEWER_TARGET,  # the reviewer's case: ssh and scp parse it as an option and run the command
    "-oProxyCommand=sh -c id",
    "-F/tmp/evil_ssh_config",
    "-v",
    "--",
    "-",
    "-build-pc",
    "-pompa@build-pc",
    "pompa@-oProxyCommand=id",
    "build-pc -oProxyCommand=id",
    "build-pc\n-oProxyCommand=id",
    "build-pc\t",
    " build-pc",
    "build-pc;id",
    "$(id)@build-pc",
    "`id`",
    "build-pc\x00",
    "b\u00fcild-pc",
    "user@[2001:db8::1]",
    "ssh://pompa@build-pc:22",
    "pompa@build-pc:22:33",
    "build-pc:0",
    "build-pc:65536",
    "build-pc:022",
    "build-pc:",
    "build-pc:ssh",
    ":22",
    "@build-pc",
    "pompa@",
    "a@b@c",
    pytest.param("", id="empty"),
    pytest.param("h" * 300, id="host-longer-than-253"),
]

SAFE_TARGETS = [
    "rtx-pc",
    "x",
    "Desktop2",
    "build-pc.lan",
    "pompa@192.0.2.172",
    "svc_build@host-01.example.com",
    "pompa@build-pc:2222",
    "192.0.2.10:22",
]


@pytest.fixture
def hdb(tmp_path) -> Database:
    db = Database(tmp_path / "hosts.db")
    with db.writer():  # initialise the store so reads of an empty registry work
        pass
    return db


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _store_unvalidated(db: Database, host_id: str, target: str) -> None:
    """Write a host row the way the registry did before validation existed."""
    with db.writer() as conn, conn:
        conn.execute("INSERT INTO hosts (id, ssh_target, shell) VALUES (?, ?, 'powershell')", (host_id, target))


# ------------------------------------------------------------------ 1. registration
@pytest.mark.parametrize("target", UNSAFE_TARGETS)
def test_register_host_rejects_unsafe_ssh_target(hdb, target):
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        hosts.register_host(hdb, "build-pc", target)
    assert hosts.list_hosts(hdb) == [], "a rejected target must not be stored"


@pytest.mark.parametrize("target", SAFE_TARGETS)
def test_register_host_accepts_alias_and_user_host_port(hdb, target):
    assert hosts.register_host(hdb, "build-pc", target).ssh_target == target


def test_rejected_update_keeps_the_previous_target(hdb):
    hosts.register_host(hdb, "build-pc", "pompa@192.0.2.172")
    with pytest.raises(hosts.HostError):
        hosts.register_host(hdb, "build-pc", REVIEWER_TARGET)
    assert hosts.get_host(hdb, "build-pc").ssh_target == "pompa@192.0.2.172"


def test_every_registration_surface_refuses_the_reviewer_target(hdb, tmp_path, monkeypatch, capsys):
    async def via_mcp():
        async with Client(create_mcp_server(hdb)) as client:
            return await client.call_tool("register_host", {"host_id": "rtx-pc", "ssh_target": REVIEWER_TARGET})

    result = anyio.run(via_mcp)
    assert result.is_error and "invalid ssh_target" in result.content[0].text

    with TestClient(create_app(hdb.path)) as client:
        response = client.put("/api/hosts/rtx-pc", json={"ssh_target": REVIEWER_TARGET})
    assert response.status_code == 400 and "invalid ssh_target" in response.json()["detail"]
    assert hosts.list_hosts(hdb) == []

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    assert cli.main(["host", "register", "rtx-pc", "--", REVIEWER_TARGET]) == 1
    assert "invalid ssh_target" in capsys.readouterr().err


# ------------------------------------------------------------------ 2. argv shape
def _host(target: str, key: str | None = None) -> hosts.m.Host:
    return hosts.m.Host(id="build-pc", ssh_target=target, ssh_key=key)


def test_ssh_argv_ends_option_parsing_before_the_destination():
    assert hosts.ssh_argv(_host("pompa@192.0.2.172")) == ["ssh", "-o", "ConnectTimeout=15", "--", "pompa@192.0.2.172"]
    with_key = ["ssh", "-o", "ConnectTimeout=15", "-i", "/keys/id_build", "-o", "IdentitiesOnly=yes", "--", "rtx-pc"]
    assert hosts.ssh_argv(_host("rtx-pc", key="/keys/id_build")) == with_key
    # a [:port] suffix becomes -p: ssh has no host:port destination syntax
    with_port = ["ssh", "-o", "ConnectTimeout=15", "-p", "2222", "--", "pompa@build-pc"]
    assert hosts.ssh_argv(_host("pompa@build-pc:2222")) == with_port


def test_scp_argv_keeps_both_operands_off_the_option_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = Path("-oProxyCommand=id")  # a hostile *relative* local name is made absolute
    argv = hosts.scp_argv(_host("pompa@192.0.2.172"), local, "C:/dst/a.txt")
    absolute = str(tmp_path.resolve() / local)
    assert argv == ["scp", "-o", "ConnectTimeout=15", "--", absolute, "pompa@192.0.2.172:C:/dst/a.txt"]
    assert not argv[-1].startswith("-") and argv[-2].startswith("/")
    assert hosts.scp_argv(_host("pompa@build-pc:2222"), tmp_path / "a", "C:/a")[3:6] == ["-P", "2222", "--"]


def test_scp_pull_argv_keeps_the_remote_operand_first_and_both_off_the_option_parser(tmp_path, monkeypatch):
    """The pull direction (host -> Mac, Task 4.5): same hardening, operands in the scp order."""
    monkeypatch.chdir(tmp_path)
    local = Path("-oProxyCommand=id")  # a hostile *relative* local name is made absolute
    argv = hosts.scp_pull_argv(_host("pompa@192.0.2.172"), "C:/src/hermes_sandbox.vpk", local)
    absolute = str(tmp_path.resolve() / local)
    assert argv == ["scp", "-o", "ConnectTimeout=15", "--", "pompa@192.0.2.172:C:/src/hermes_sandbox.vpk", absolute]
    assert not local.is_absolute() and argv[-1].startswith("/")
    assert hosts.scp_pull_argv(_host("pompa@build-pc:2222"), "C:/a", tmp_path / "a")[3:6] == ["-P", "2222", "--"]


@pytest.mark.parametrize("remote", ["-oProxyCommand=id", "-C:/x", "-", "", "C:/a\nb", "C:/a\rb"])
def test_scp_pull_argv_refuses_an_option_like_empty_or_multiline_remote(tmp_path, remote):
    with pytest.raises(hosts.HostError, match="remote"):
        hosts.scp_pull_argv(_host("build-pc"), remote, tmp_path / "a.vpk")


@pytest.mark.parametrize("target", [REVIEWER_TARGET, "build-pc -oProxyCommand=id", "pompa@-x"])
def test_argv_builders_refuse_a_stored_unsafe_target(tmp_path, target):
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        hosts.ssh_argv(_host(target))
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        hosts.scp_argv(_host(target), tmp_path / "a.txt", "C:/dst/a.txt")


def test_every_ssh_call_site_puts_the_destination_right_after_double_dash(hdb, tmp_path, monkeypatch):
    hosts.register_host(hdb, "build-pc", "pompa@192.0.2.172", shell="powershell")
    local = tmp_path / "a.txt"
    local.write_bytes(b"payload")
    calls: list[list[str]] = []
    # hosts.subprocess and deploys.subprocess are the same module, so one fake answers every call:
    # the probe wants a JSON line, scp wants rc 0, Get-FileHash wants '<md5>|<path>' lines.
    replies = {"probe": '{"host": "DESKTOP-BUILDPC"}\n', "hash": f"{md5_file(local)}|C:/dst/a.txt\n"}
    phase = {"now": "probe"}

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _Result(stdout="" if argv[0] == "scp" else replies[phase["now"]])

    monkeypatch.setattr(hosts.subprocess, "run", fake_run)

    # probe_host
    script = tmp_path / "probe.ps1"
    script.write_text("Write-Output '{}'")
    monkeypatch.setattr(hosts, "PROBE_SCRIPT", script)
    hosts.probe_host(hdb, "build-pc")
    assert calls[-1][-3:-1] == ["--", "pompa@192.0.2.172"]

    # deploy_push: one ssh pass creating the parent dirs, one scp per file, then the Get-FileHash ssh call
    calls.clear()
    phase["now"] = "hash"
    deploys.push(hdb, "build-pc", "C:/dst", [{"path": "a.txt", "local": str(local)}])
    mkdir, scp, ssh = calls
    assert mkdir[0] == "ssh" and mkdir[-3:-1] == ["--", "pompa@192.0.2.172"]
    assert scp[0] == "scp" and scp[-3:] == ["--", str(local.resolve()), "pompa@192.0.2.172:C:/dst/a.txt"]
    assert ssh[0] == "ssh" and ssh[-3:-1] == ["--", "pompa@192.0.2.172"]

    # build_start: a command that looks like an option is still the remote command, because ssh
    # stops reading options at "--" (without it, OpenSSH re-parses the words after the host)
    launched: list[list[str]] = []
    gate = threading.Event()

    class _Proc:
        pid = 4242

        def __init__(self, argv, **_kw):
            launched.append(argv)

        def poll(self):
            return 0 if gate.is_set() else None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            gate.set()

        kill = terminate

    monkeypatch.setattr(builds.subprocess, "Popen", _Proc)
    run = builds.start(hdb, "build-pc", "-oProxyCommand=id")
    gate.set()
    builds.wait(hdb, run.id, timeout_sec=5, poll_sec=0.02)
    assert launched == [["ssh", "-o", "ConnectTimeout=15", "--", "pompa@192.0.2.172", "-oProxyCommand=id"]]


# ------------------------------------------------------------------ 3. rows stored before the fix
def test_a_target_stored_before_the_fix_is_refused_before_any_side_effect(hdb, tmp_path, monkeypatch):
    _store_unvalidated(hdb, "legacy-pc", REVIEWER_TARGET)
    calls: list = []
    monkeypatch.setattr(hosts.subprocess, "run", lambda argv, **_kw: calls.append(argv))
    monkeypatch.setattr(builds.subprocess, "Popen", lambda argv, **_kw: calls.append(argv))
    probe = tmp_path / "probe.ps1"
    probe.write_text("Write-Output '{}'")
    monkeypatch.setattr(hosts, "PROBE_SCRIPT", probe)
    local = tmp_path / "a.txt"
    local.write_bytes(b"x")

    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        hosts.probe_host(hdb, "legacy-pc")
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        deploys.push(hdb, "legacy-pc", "C:/dst", [{"path": "a.txt", "local": str(local)}])
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        builds.start(hdb, "legacy-pc", "compile")

    assert calls == [], "no ssh/scp process may start for an unsafe target"
    assert hosts.get_host(hdb, "legacy-pc").lock is None, "no lease may be taken"
    assert deploys.list_deployments(hdb) == [] and builds.list_runs(hdb) == []


def test_verify_refuses_a_host_whose_target_was_rewritten_unsafe(hdb, tmp_path, monkeypatch):
    hosts.register_host(hdb, "build-pc", "pompa@192.0.2.172", shell="powershell")
    local = tmp_path / "a.txt"
    local.write_bytes(b"payload")
    digest = md5_file(local)
    calls: list = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        return _Result(stdout="" if argv[0] == "scp" else f"{digest}|C:/dst/a.txt\n")

    monkeypatch.setattr(deploys.subprocess, "run", fake_run)
    deployment = deploys.push(hdb, "build-pc", "C:/dst", [{"path": "a.txt", "local": str(local)}])
    assert deployment.status == "verified"
    with hdb.writer() as conn, conn:
        conn.execute("UPDATE hosts SET ssh_target = ? WHERE id = 'build-pc'", (REVIEWER_TARGET,))
    calls.clear()
    with pytest.raises(hosts.HostError, match="invalid ssh_target"):
        deploys.verify(hdb, deployment.id)
    assert calls == []
