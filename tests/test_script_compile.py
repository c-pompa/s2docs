"""The cs_script `.js` -> `.vjs_c` compile path — offline: every ssh/scp/build is faked.

Two rules this file pins:

- a `.js` compile is allowed on **desktop-pc and pomps-pc** (it takes ~1 s, needs no display adapter and
  works from ssh's session 0), while the *vmap* policy (`sandbox_build.COMPILE_HOSTS`) stays desktop-pc
  only — so this path refuses every input that is not exactly one `.js` file;
- a succeeded run is only a build when it produced a **fresh** `.vjs_c` (rc exits 0 after a crash, and a
  leftover file from an earlier compile must never be pulled back as this run's output).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2docs import hosts
from s2docs import models as m
from s2docs.build import script_compile
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.live import sandbox_build

CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"
ADDON = "vjs_probe"
NAME = "pinger"


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _set_facts(db: Database, host_id: str, facts: dict) -> None:
    with db.writer() as conn, conn:
        conn.execute("UPDATE hosts SET facts = ? WHERE id = ?", (json.dumps(facts), host_id))


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "data" / "s2docs.db")
    with d.writer():
        pass
    for host_id, shell in (("desktop-pc", "powershell"), ("pomps-pc", "cmd"), ("build-pc", "powershell")):
        hosts.register_host(d, host_id, host_id, shell=shell)
        _set_facts(d, host_id, {"probe": {"cs2": {"path": CS2}}})
    return d


def _run(*, status: m.BuildStatus = "succeeded", exit_code: int | None = 0,
         host_id: str = "pomps-pc") -> m.BuildRun:
    return m.BuildRun(
        id="b-script", host_id=host_id, kind="compile", command=script_compile.compile_command(CS2, ADDON, NAME),
        status=status, owner="build:b-script", exit_code=exit_code,
        log_path="/tmp/fake-build.log", created_at="2026-10-27T00:00:00Z", updated_at="2026-10-27T00:00:00Z",
    )


def _patch_build(monkeypatch, *, run: m.BuildRun, log: str, seen: dict | None = None):
    """Fake the server-owned run: start/wait/log_tail/record_artifacts/get_run all in-process."""

    def fake_start(_db, host_id, command, **kw):
        if seen is not None:
            seen["start"] = {"host_id": host_id, "command": command, **kw}
        return _run(host_id=host_id)

    def fake_wait(_db, run_id, **_kw):
        return run

    def fake_log_tail(_db, run_id, *, lines=200):
        return m.BuildLogTail(run_id=run_id, lines=log.splitlines(), encoding="utf-8",
                              size=len(log), truncated=False)

    def fake_record(_db, run_id, paths):
        if seen is not None:
            seen["artifacts"] = list(paths)
        return run

    monkeypatch.setattr(script_compile.builds, "start", fake_start)
    monkeypatch.setattr(script_compile.builds, "wait", fake_wait)
    monkeypatch.setattr(script_compile.builds, "log_tail", fake_log_tail)
    monkeypatch.setattr(script_compile.builds, "record_artifacts", fake_record)
    monkeypatch.setattr(script_compile.builds, "get_run", lambda _db, _id: run)


def _patch_push(monkeypatch, seen: dict, *, status: str = "verified"):
    def fake_push(_db, host_id, dest_root, files, **kw):
        seen["push"] = {"host_id": host_id, "dest_root": dest_root,
                       "files": [dict(f) for f in files], **kw}
        return m.Deployment(id="d-src", host_id=host_id, addon=ADDON, dest_root=dest_root,
                            status=status, created_at="2026-10-27T00:00:00Z",
                            updated_at="2026-10-27T00:00:00Z")

    monkeypatch.setattr(script_compile.deploys, "push", fake_push)


def _patch_pull(monkeypatch, seen: dict, *, on_host: str = "abc123"):
    def fake_remote_md5(host, paths):
        seen["remote_md5"] = list(paths)
        return dict.fromkeys(paths, on_host)

    def fake_run(argv, **_kw):
        seen["scp"] = argv
        Path(argv[-1]).write_bytes(b"vjs_c")
        return _Result()

    monkeypatch.setattr(script_compile.deploys, "remote_md5", fake_remote_md5)
    monkeypatch.setattr(script_compile.subprocess, "run", fake_run)
    monkeypatch.setattr(script_compile, "md5_file", lambda path: on_host)


# ------------------------------------------------------------------ host policy
def test_script_compiles_are_allowed_on_both_hosts_and_nowhere_else(db):
    """Measured: a .js compile is ~1 s on pomps-pc and works from session 0 — the vmap rule does not apply."""
    assert frozenset({"desktop-pc", "pomps-pc"}) == script_compile.SCRIPT_COMPILE_HOSTS
    for host_id in ("desktop-pc", "pomps-pc"):
        assert script_compile.check_host(host_id) == host_id
    with pytest.raises(script_compile.ScriptCompileError, match="desktop-pc, pomps-pc only"):
        script_compile.check_host("build-pc")


def test_the_vmap_policy_is_untouched_desktop_pc_only():
    """The cheap script pass must never be a back door to the map pass that froze pomps-pc."""
    assert frozenset({"desktop-pc"}) == sandbox_build.COMPILE_HOSTS


def test_compile_refuses_anything_that_is_not_one_js_file(tmp_path):
    vmap = tmp_path / "probe.vmap"
    vmap.write_text("versioninfo", encoding="utf-8")
    vmdl = tmp_path / "probe.vmdl"
    vmdl.write_text("<!-- kv3 -->", encoding="utf-8")
    folder = tmp_path / "scripts"
    folder.mkdir()
    js = tmp_path / "pinger.js"
    js.write_text("export {};\n", encoding="utf-8")

    for bad in (vmap, vmdl, folder):
        with pytest.raises(script_compile.ScriptCompileError):
            script_compile.check_script_source(bad)
    assert script_compile.check_script_source(js) == js
    with pytest.raises(script_compile.ScriptCompileError, match="does not exist"):
        script_compile.check_script_source(tmp_path / "nope.js")


def test_script_names_can_only_ever_name_one_file_under_scripts(db, tmp_path):
    js = tmp_path / "pinger.js"
    js.write_text("export {};\n", encoding="utf-8")
    for name in ("../escape", "maps/hermes_sandbox", "pinger.js", "Pinger", ""):
        with pytest.raises(script_compile.ScriptCompileError, match="invalid script name"):
            script_compile.check_name(name)
    assert script_compile.source_rel("pinger") == "scripts/pinger.js"
    assert script_compile.compiled_rel("pinger") == "scripts/pinger.vjs_c"


def test_compile_js_refuses_a_vmap_before_touching_the_host(db, tmp_path, monkeypatch):
    vmap = tmp_path / "probe.vmap"
    vmap.write_text("versioninfo", encoding="utf-8")
    monkeypatch.setattr(script_compile.deploys, "push", lambda *a, **kw: pytest.fail("must not push"))
    with pytest.raises(script_compile.ScriptCompileError, match="only a cs_script .js"):
        script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=vmap)


# ------------------------------------------------------------------ the command
def test_compile_script_runs_rc_on_only_the_js_and_requires_a_fresh_output():
    script = script_compile.compile_script(CS2, ADDON, NAME)
    # one compiler invocation, one file, no task/display gymnastics (a script pass needs neither)
    assert script.count("resourcecompiler.exe") == 1
    assert " -nop4 -i $src" in script
    assert "ScheduledTask" not in script and "query user" not in script and "nvidia-smi" not in script
    assert rf"content\csgo_addons\{ADDON}\scripts\{NAME}.js" in script
    assert rf"game\csgo_addons\{ADDON}\scripts\{NAME}.vjs_c" in script
    # exit 0 is not proof: the artifact must exist AND postdate this run's start
    assert "$started = Get-Date" in script
    assert "LastWriteTime -lt $started" in script
    assert "STALE" in script and script_compile.SUCCESS_TOKEN in script
    # rc only creates some output dirs, and a redirect into a missing one fails BEFORE rc runs
    assert "CreateDirectory($outDir)" in script
    assert "$null -eq $code" in script, "no exit code means the compiler never ran"
    # ... and the encoded form goes through the one PowerShell builder
    assert script_compile.compile_command(CS2, ADDON, NAME) == hosts.powershell_command(script)


# ------------------------------------------------------------------ the flow
def test_compile_js_pushes_compiles_pulls_and_records_the_artifact(db, monkeypatch, tmp_path):
    seen: dict = {}
    source = tmp_path / "pinger.js"
    source.write_text("export {};\n", encoding="utf-8")
    _patch_push(monkeypatch, seen)
    _patch_build(monkeypatch, run=_run(), log=f"{script_compile.SUCCESS_TOKEN} C:/x/scripts/pinger.vjs_c", seen=seen)
    _patch_pull(monkeypatch, seen)

    compiled = script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=source, poll_sec=0)

    # 1. the source went to the compile host's content tree, verified before anything ran
    assert seen["push"]["host_id"] == "pomps-pc"
    assert seen["push"]["dest_root"] == CS2.replace("\\", "/") + f"/content/csgo_addons/{ADDON}"
    assert [f["path"] for f in seen["push"]["files"]] == ["scripts/pinger.js"]
    # 2. the run is a server-owned compile of exactly that file
    assert seen["start"]["host_id"] == "pomps-pc" and seen["start"]["kind"] == "compile"
    assert f"script={ADDON}/scripts/pinger.js" in seen["start"]["notes"]
    # 3. the vjs_c came back through scp_pull_argv, checked against the host's md5
    assert seen["remote_md5"] == [CS2.replace("\\", "/") + f"/game/csgo_addons/{ADDON}/scripts/pinger.vjs_c"]
    assert seen["scp"] == hosts.scp_pull_argv(hosts.get_host(db, "pomps-pc"), seen["remote_md5"][0], compiled.path)
    assert compiled.path == get_settings().data_dir / "scripts" / ADDON / "pinger.vjs_c"
    assert compiled.path.is_file() and compiled.md5 == "abc123" and compiled.size == 5
    # 4. the artifact is attached to the run, not pasted into a message
    assert seen["artifacts"] == [str(compiled.path)]
    assert compiled.run.id == "b-script"


def test_compile_js_rejects_a_stale_vjs_c_even_when_rc_exits_zero(db, monkeypatch, tmp_path):
    """The live failure this guards: rc exits 0 after a crash and an old .vjs_c looks like output."""
    source = tmp_path / "pinger.js"
    source.write_text("export {};\n", encoding="utf-8")
    seen: dict = {}
    _patch_push(monkeypatch, seen)
    _patch_build(monkeypatch, run=_run(exit_code=0),
                 log=f"resourcecompiler exit 0\nSTALE {script_compile.SCRIPT_SUFFIX}: it predates this run")
    monkeypatch.setattr(script_compile.deploys, "remote_md5",
                        lambda *a, **kw: pytest.fail("a stale artifact must not be pulled"))

    with pytest.raises(script_compile.ScriptNotFresh, match="no fresh .vjs_c was proven"):
        script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=source, poll_sec=0)


def test_compile_js_rejects_a_negative_exit_code(db, monkeypatch, tmp_path):
    """A crash's NTSTATUS is negative; it is a failed build, never a success."""
    source = tmp_path / "pinger.js"
    source.write_text("export {};\n", encoding="utf-8")
    seen: dict = {}
    _patch_push(monkeypatch, seen)
    _patch_build(monkeypatch, run=_run(status="failed", exit_code=-1073741819), log="boom")
    monkeypatch.setattr(script_compile.deploys, "remote_md5",
                        lambda *a, **kw: pytest.fail("a failed run must not be pulled"))

    with pytest.raises(script_compile.ScriptCompileError, match="exit -1073741819") as exc:
        script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=source, poll_sec=0)
    assert not isinstance(exc.value, script_compile.ScriptNotFresh)
    assert exc.value.run_id == "b-script", "the run stays linkable even when it failed"


def test_compile_js_refuses_a_pull_whose_md5_does_not_match_the_host(db, monkeypatch, tmp_path):
    source = tmp_path / "pinger.js"
    source.write_text("export {};\n", encoding="utf-8")
    seen: dict = {}
    _patch_push(monkeypatch, seen)
    _patch_build(monkeypatch, run=_run(), log=f"{script_compile.SUCCESS_TOKEN} C:/x")
    _patch_pull(monkeypatch, seen, on_host="aaa")
    monkeypatch.setattr(script_compile, "md5_file", lambda path: "bbb")

    with pytest.raises(script_compile.ScriptCompileError, match="pulled md5"):
        script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=source, poll_sec=0)


def test_compile_js_refuses_a_source_the_host_did_not_verify(db, monkeypatch, tmp_path):
    source = tmp_path / "pinger.js"
    source.write_text("export {};\n", encoding="utf-8")
    seen: dict = {}
    _patch_push(monkeypatch, seen, status="failed")
    monkeypatch.setattr(script_compile.builds, "start", lambda *a, **kw: pytest.fail("must not compile"))

    with pytest.raises(script_compile.ScriptCompileError, match="did not verify"):
        script_compile.compile_js(db, "pomps-pc", addon=ADDON, name=NAME, source=source)


def test_deploy_loose_copies_the_vjs_c_under_game_csgo_scripts(db, monkeypatch, tmp_path):
    seen: dict = {}
    _patch_push(monkeypatch, seen)
    compiled = tmp_path / "pinger.vjs_c"
    compiled.write_bytes(b"vjs_c")

    dep = script_compile.deploy_loose(db, "desktop-pc", addon=ADDON, name=NAME, compiled=compiled)

    assert dep.status == "verified"
    assert seen["push"]["host_id"] == "desktop-pc"
    assert seen["push"]["dest_root"] == CS2.replace("\\", "/") + "/game/csgo"
    assert [f["path"] for f in seen["push"]["files"]] == ["scripts/pinger.vjs_c"]
    assert seen["push"]["files"][0]["local"] == str(compiled.resolve())


def test_deploy_loose_refuses_when_nothing_was_compiled(db, monkeypatch, tmp_path):
    monkeypatch.setattr(script_compile.deploys, "push", lambda *a, **kw: pytest.fail("must not push"))
    with pytest.raises(script_compile.ScriptCompileError, match="compile and pull it first"):
        script_compile.deploy_loose(db, "desktop-pc", addon=ADDON, name=NAME,
                                    compiled=tmp_path / "nope.vjs_c")
