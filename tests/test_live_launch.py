"""Launch CS2 on a host (Task 4.6) — offline: ssh is faked, the netcon poll is a fake client.

The two pitfalls this pins (``references/live-cs2-control.md``):
- the PowerShell script goes on **stdin**, never into an inline ``-Command "…"`` string, and
- the game has to start through a scheduled task with an **interactive** principal (a service-spawned
  cs2.exe has no desktop), which is deleted again right after.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.live import launch, service
from s2docs.live.netcon import NetconError

CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"
STATUS = "version  : 1.41.9.2/14192 10955 insecure  public\nloaded spawngroup(  1) : SV: [0: hermes_sandbox]"


class _Result:
    def __init__(self, returncode=0, stdout="launched", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _Client:
    """Answers netcon only after `fail_times` refused connections."""

    def __init__(self, fail_times: int = 0, reply: str = STATUS):
        self.fail_times, self.reply, self.calls = fail_times, reply, 0

    def run(self, lines, **_kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise NetconError("netcon unreachable")
        return self.reply


def _set_facts(db: Database, host_id: str, facts: dict) -> None:
    with db.writer() as conn, conn:
        conn.execute("UPDATE hosts SET facts = ? WHERE id = ?", (json.dumps(facts), host_id))


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(d, "pomps-pc", "pomps-pc", shell="powershell")
    _set_facts(d, "pomps-pc", {"cs2_path": CS2})
    return d


def _launch(db, monkeypatch, *, client=None, timeout_sec=120, map_name="de_dust2"):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return _Result()

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)
    out = launch.launch(db, "pomps-pc", map_name, timeout_sec=timeout_sec,
                        client_factory=lambda: client or _Client(), sleep=lambda _s: None)
    return out, calls


def test_launch_sends_the_script_on_stdin_and_polls_netcon(db, monkeypatch):
    out, calls = _launch(db, monkeypatch, client=_Client(fail_times=2))
    argv, kw = calls[0]
    host = hosts.get_host(db, "pomps-pc")
    assert argv[:-1] == hosts.ssh_argv(host)
    assert argv[-1].endswith("-Command -") and "powershell" in argv[-1].lower()  # stdin, not an inline string

    script = kw["input"]
    assert "Register-ScheduledTask" in script and "Start-ScheduledTask" in script
    assert "Unregister-ScheduledTask" in script and "-LogonType Interactive" in script
    assert "-netconport 2121" in script and "+map de_dust2" in script and "-insecure" in script
    assert CS2 + r"\game\bin\win64\cs2.exe" in script
    assert out["netcon"] is True and out["map"] == "de_dust2" and out["cs2_build"] == "1.41.9.2/14192"


@pytest.mark.parametrize("bad", ["de_dust2; quit", "de_dust2 ", "dust2", "", "de_dust2\nquit", "c:/de_x"])
def test_launch_refuses_anything_but_a_de_map(db, monkeypatch, bad):
    def no_ssh(*_a, **_kw):
        pytest.fail("must not touch a host for a rejected map")

    monkeypatch.setattr(launch.subprocess, "run", no_ssh)
    with pytest.raises(ValueError, match="de_"):
        launch.launch(db, "pomps-pc", bad, client_factory=lambda: _Client())


def test_launch_refuses_a_leased_host(db, monkeypatch):
    hosts.claim_host(db, "pomps-pc", "verify_demo/j-1", ttl_sec=600)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: pytest.fail("must not touch a leased host"))
    with pytest.raises(service.LiveError, match="verify_demo/j-1"):
        launch.launch(db, "pomps-pc", "de_dust2", client_factory=lambda: _Client())


def test_launch_needs_the_cs2_path_fact(db, monkeypatch):
    _set_facts(db, "pomps-pc", {})
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: pytest.fail("must not ssh without a path"))
    with pytest.raises(service.LiveError, match="cs2_path"):
        launch.launch(db, "pomps-pc", "de_dust2", client_factory=lambda: _Client())


def test_launch_times_out_when_netcon_never_answers(db, monkeypatch):
    with pytest.raises(service.LiveError, match="netcon"):
        _launch(db, monkeypatch, client=_Client(fail_times=10_000), timeout_sec=0)


def test_launch_fails_when_ssh_fails(db, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: _Result(returncode=1, stderr="Access denied"))
    with pytest.raises(service.LiveError, match="Access denied"):
        launch.launch(db, "pomps-pc", "de_dust2", client_factory=lambda: _Client(), sleep=lambda _s: None)


def test_launch_rest_route(db, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: _Result())
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(service, "client_for", lambda db_, host_id: _Client())
    with TestClient(create_app(db.path)) as c:
        assert c.post("/api/live/launch", json={"host_id": "pomps-pc", "map": "de_dust2;quit"}).status_code == 422
        ok = c.post("/api/live/launch", json={"host_id": "pomps-pc", "map": "de_dust2"})
        assert ok.status_code == 200 and ok.json()["map"] == "de_dust2"
