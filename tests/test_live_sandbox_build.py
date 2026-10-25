"""Compile and deploy the hermes_sandbox helper map (Tasks 4.5/4.6) — offline: every ssh/scp is faked.

Two rules this file pins:
- compiles run on **desktop-pc only** (pomps-pc hard-froze on a compile), and
- the compiled files come back to the Mac over ``hosts.scp_pull_argv`` with an MD5 readback, because the
  deploy service pushes FROM the Mac.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2docs import hosts
from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.live import sandbox_build, service

CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _set_facts(db: Database, host_id: str, facts: dict) -> None:
    with db.writer() as conn, conn:
        conn.execute("UPDATE hosts SET facts = ? WHERE id = ?", (json.dumps(facts), host_id))


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(d, "desktop-pc", "desktop-pc", shell="powershell")
    hosts.register_host(d, "pomps-pc", "pomps-pc", shell="powershell")
    _set_facts(d, "desktop-pc", {"cs2_path": CS2})
    _set_facts(d, "pomps-pc", {"cs2_path": CS2})
    return d


def _run(command="x") -> m.BuildRun:
    return m.BuildRun(id="b-1", host_id="desktop-pc", kind="compile", command=command, status="running",
                      owner="build:b-1", created_at="2026-10-25T00:00:00Z", updated_at="2026-10-25T00:00:00Z")


def _deploy(host_id, dest_root) -> m.Deployment:
    return m.Deployment(id="d-1", host_id=host_id, dest_root=dest_root, status="verified",
                        created_at="2026-10-25T00:00:00Z", updated_at="2026-10-25T00:00:00Z")


def test_build_refuses_any_host_but_desktop_pc(db):
    """pomps-pc hard-froze on a compile: the refusal is code, not a note in a doc."""
    with pytest.raises(service.LiveError, match="desktop-pc only"):
        sandbox_build.build(db, "pomps-pc")
    assert frozenset({"desktop-pc"}) == sandbox_build.COMPILE_HOSTS


def test_build_writes_the_addon_deploys_the_sources_then_starts_the_compile(db, monkeypatch):
    pushed: dict = {}
    started: dict = {}

    def fake_push(db_, host_id, dest_root, files, **kw):
        pushed.update(host_id=host_id, dest_root=dest_root, files=list(files), **kw)
        return _deploy(host_id, dest_root)

    def fake_start(db_, host_id, command, **kw):
        started.update(host_id=host_id, command=command, **kw)
        return _run(command)

    monkeypatch.setattr(sandbox_build.deploys, "push", fake_push)
    monkeypatch.setattr(sandbox_build.builds, "start", fake_start)

    run = sandbox_build.build(db, "desktop-pc")
    assert run.status == "running" and started["host_id"] == "desktop-pc"

    # 1. the sources were rendered into the staging dir and pushed to the addon's content dir
    staging = sandbox_build.staging_dir()
    assert (staging / "maps" / "hermes_sandbox.vmap").is_file()
    assert (staging / "maps" / "scripts" / "hermes_sandbox.js").is_file()
    assert pushed["dest_root"] == CS2.replace("\\", "/") + "/content/csgo_addons/hermes_sandbox"
    assert [f["path"] for f in pushed["files"]] == ["maps/hermes_sandbox.vmap", "maps/scripts/hermes_sandbox.js"]
    assert all(f["local"].startswith(str(staging)) for f in pushed["files"])

    # 2. one PowerShell line, script first then the map (iso_hud_build.ps1 shape)
    command = started["command"]
    assert command.count("resourcecompiler.exe") == 2 and " -nop4 -i " in command
    assert command.index(r"maps\scripts\hermes_sandbox.js") < command.index(r"\maps\hermes_sandbox.vmap")
    assert started["kind"] == "compile" and "map=hermes_sandbox" in started["notes"]


def test_build_needs_the_host_cs2_path(db):
    _set_facts(db, "desktop-pc", {})
    with pytest.raises(service.LiveError, match="cs2_path"):
        sandbox_build.build(db, "desktop-pc")


def _fake_scp_writes(calls: list[list[str]], payload: bytes = b"compiled"):
    def run(argv, **_kw):
        calls.append(argv)
        Path(argv[-1]).write_bytes(payload)
        return _Result()

    return run


def test_pull_compiled_uses_scp_pull_argv_for_both_files(db, monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox_build.subprocess, "run", _fake_scp_writes(calls))
    monkeypatch.setattr(sandbox_build.deploys, "remote_md5", lambda host, paths: dict.fromkeys(paths, "hash"))
    monkeypatch.setattr(sandbox_build, "md5_file", lambda path: "hash")

    out = sandbox_build.pull_compiled(db, "desktop-pc", dest=tmp_path / "compiled")
    host = hosts.get_host(db, "desktop-pc")
    assert [p.name for p in out] == ["hermes_sandbox.vpk", "hermes_sandbox.vjs_c"]
    assert calls == [hosts.scp_pull_argv(host, f"{sandbox_build.game_root(CS2)}/csgo_addons/hermes_sandbox/{rel}", p)
                     for rel, p in zip(sandbox_build.COMPILED, out, strict=True)]


def test_pull_compiled_refuses_a_hash_mismatch(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_build.subprocess, "run", _fake_scp_writes([]))
    monkeypatch.setattr(sandbox_build.deploys, "remote_md5", lambda host, paths: dict.fromkeys(paths, "other"))
    monkeypatch.setattr(sandbox_build, "md5_file", lambda path: "hash")
    with pytest.raises(service.LiveError, match="md5"):
        sandbox_build.pull_compiled(db, "desktop-pc", dest=tmp_path / "compiled")


def test_pull_compiled_refuses_a_copy_that_wrote_nothing(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_build.subprocess, "run", lambda argv, **kw: _Result())  # rc 0, no file
    monkeypatch.setattr(sandbox_build.deploys, "remote_md5", lambda host, paths: dict.fromkeys(paths, "hash"))
    with pytest.raises(service.LiveError, match="wrote no file"):
        sandbox_build.pull_compiled(db, "desktop-pc", dest=tmp_path / "compiled")


def test_pull_compiled_refuses_a_file_the_host_does_not_have(db, monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(sandbox_build.subprocess, "run", _fake_scp_writes(calls))
    monkeypatch.setattr(sandbox_build.deploys, "remote_md5", lambda host, paths: dict.fromkeys(paths, "missing"))
    with pytest.raises(service.LiveError, match="missing"):
        sandbox_build.pull_compiled(db, "desktop-pc", dest=tmp_path / "compiled")
    assert calls == []  # fail fast: nothing is copied for a file the host never produced


def test_deploy_loose_pushes_the_two_files_to_the_game_maps_paths(db, monkeypatch, tmp_path):
    pushed: dict = {}

    def fake_push(db_, host_id, dest_root, files, **kw):
        pushed.update(host_id=host_id, dest_root=dest_root, files=[dict(f) for f in files], **kw)
        return _deploy(host_id, dest_root)

    monkeypatch.setattr(sandbox_build.deploys, "push", fake_push)
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    vpk, vjs = compiled / "hermes_sandbox.vpk", compiled / "hermes_sandbox.vjs_c"
    vpk.write_bytes(b"vpk")
    vjs.write_bytes(b"vjs")

    dep = sandbox_build.deploy_loose(db, "pomps-pc", pulled=[vpk, vjs])
    assert dep.status == "verified" and pushed["host_id"] == "pomps-pc"
    assert pushed["dest_root"] == CS2.replace("\\", "/") + "/game/csgo"
    assert [f["path"] for f in pushed["files"]] == ["maps/hermes_sandbox.vpk", "maps/scripts/hermes_sandbox.vjs_c"]
    assert [f["local"] for f in pushed["files"]] == [str(vpk.resolve()), str(vjs.resolve())]
    assert pushed["owner"] == f"live:{sandbox_build.ADDON}"


def test_deploy_loose_refuses_when_a_compiled_file_is_missing(db, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_build.deploys, "push", lambda *a, **kw: pytest.fail("must not push"))
    with pytest.raises(service.LiveError, match="build"):
        sandbox_build.deploy_loose(db, "pomps-pc", pulled=[tmp_path / "nope.vpk", tmp_path / "nope.vjs_c"])


def test_display_gate_reads_nvidia_smi_over_ssh(db, monkeypatch):
    """An RDP-only session has no display adapter and resourcecompiler fails there (live-cs2-control)."""
    seen: list[list[str]] = []

    def fake_run(reply):
        def run(argv, **_kw):
            seen.append(argv)
            return _Result(stdout=reply)

        return run

    monkeypatch.setattr(sandbox_build.subprocess, "run", fake_run("display_active\nEnabled\n"))
    assert sandbox_build.display_gate(db, "desktop-pc") is True
    assert seen[-1][:-1] == hosts.ssh_argv(hosts.get_host(db, "desktop-pc"))
    assert "nvidia-smi" in seen[-1][-1] and "display_active" in seen[-1][-1]

    for reply in ("display_active\nDisabled\n", "NoDevices\n"):
        monkeypatch.setattr(sandbox_build.subprocess, "run", fake_run(reply))
        with pytest.raises(service.LiveError, match="display"):
            sandbox_build.display_gate(db, "desktop-pc")
