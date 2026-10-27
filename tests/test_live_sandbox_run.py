"""Bundling a user module into hermes_sandbox and judging the reload by its console readback (offline).

Everything here is faked: no ssh, no resourcecompiler, no netcon. What is NOT faked is the judgement —
these tests exist so that `verified` can only ever come from `[user:<name>] ready <nonce>` **plus**
`[hs] pong`, and so a reload that never finished spawning sends nothing else to the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.live import sandbox_run, sandbox_user
from s2docs.live.service import LiveError

MAP_OK = """\
----- Status -----
@ Current  :  game
1: de_dust2
"""
MAP_SPAWNING = """\
----- Status -----
@ Current  :  game
"""

MODULE = "demo"
CODE = """\
function start(api) { api.msg("hello"); }
"""


class FakeClient:
    """A netcon stand-in that answers `poll` with canned console text, per phase."""

    def __init__(self, *, map_text: str = MAP_OK, load_text: str = "") -> None:
        self.map_text, self.load_text = map_text, load_text
        self.sent: list[list[str]] = []
        self.probes: list[list[str]] = []

    def poll(self, lines, probe, ready, *, timeout=90.0, **_kw):
        self.sent.append(list(lines))
        self.probes.append(list(probe))
        text = self.map_text if any(line.startswith("map ") for line in lines) else self.load_text
        return bool(ready(text)), text


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(tmp_path) -> Database:
    from s2docs import hosts

    d = Database(tmp_path / "data" / "s2docs.db")
    with d.writer():
        pass
    hosts.register_host(d, "pomps-pc", "pomps-pc", shell="cmd")
    return d


def _module(nonce: str = "abc123") -> sandbox_user.UserModule:
    return sandbox_user.check_user_module(CODE, name=MODULE, nonce=nonce)


# ------------------------------------------------------------------- the reload
def test_reload_is_verified_on_the_ready_line_AND_a_pong(db):
    client = FakeClient(load_text=f"[user:{MODULE}] ready abc123\n[hs] pong 0\n")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert out["status"] == "verified" and "ready abc123" in out["detail"]
    # the reload is the map load, cheats, the group, then a status/hs_ping probe loop
    assert client.sent[0] == ["map de_dust2"]
    assert client.sent[1] == ["sv_cheats 1", "spawn_group_load hermes_sandbox"]
    assert out["lines"] == ["map de_dust2", "sv_cheats 1", "spawn_group_load hermes_sandbox", "status",
                            "hs_ping"]
    assert "hs_ping" in client.probes[1]


def test_the_wait_does_not_stop_on_the_ready_line_before_the_pong(db):
    """Measured live: the ready line lands while the group spawns, the pong one probe later."""

    class Streaming(FakeClient):
        def poll(self, lines, probe, ready, *, timeout=90.0, **_kw):
            self.sent.append(list(lines))
            self.probes.append(list(probe))
            if any(line.startswith("map ") for line in lines):
                return bool(ready(self.map_text)), self.map_text
            early = f"[user:{MODULE}] ready abc123\n"
            self.stopped_early = ready(early)
            late = early + "[hs] pong 0\n"
            return bool(ready(late)), late

    client = Streaming()
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert client.stopped_early is False, "the poll must keep probing until hs_ping answers"
    assert out["status"] == "verified"


def test_a_stale_nonce_is_not_a_ready_line(db):
    client = FakeClient(load_text=f"[user:{MODULE}] ready deadbee\n[hs] pong 0\n")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    # the RIGHT shape but the wrong nonce: this is not the build that is running
    assert out["status"] == "failed" and "bundled module did not" in out["detail"]


def test_a_module_error_line_fails_the_verdict_with_its_message(db):
    client = FakeClient(load_text=f"[user:{MODULE}] error boom\n[hs] pong 0\n")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert out["status"] == "failed" and "boom" in out["detail"]


def test_a_pong_without_a_ready_line_means_the_script_ran_and_the_module_did_not(db):
    client = FakeClient(load_text="[hs] pong 0\n")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert out["status"] == "failed" and "bundled module did not" in out["detail"]


def test_silence_is_a_failure_never_a_pass(db):
    client = FakeClient(load_text="")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert out["status"] == "failed" and "did not run" in out["detail"]


def test_a_map_that_did_not_finish_spawning_sends_nothing_else(db):
    """The engine DROPS commands sent mid-spawn: the reload must stop, not fire the group into the void."""
    client = FakeClient(map_text=MAP_SPAWNING, load_text="[user:demo] ready abc123\n[hs] pong 0\n")
    out = sandbox_run.reload(db, "pomps-pc", user=_module(), client=client)
    assert out["status"] == "failed" and "did not finish spawning" in out["detail"]
    assert client.sent == [["map de_dust2"]]


def test_the_stock_restore_is_verified_on_a_pong_alone(db):
    client = FakeClient(load_text="[hs] pong 2\n")
    out = sandbox_run.reload(db, "pomps-pc", user=None, client=client)
    assert out["status"] == "verified" and "stock sandbox reloaded" in out["detail"]
    assert "spawn_group_load hermes_sandbox" in out["lines"]


# ---------------------------------------------------------------- ship / restore
def _patch_compile(monkeypatch, seen: dict, *, deploy_status: str = "verified"):
    from s2docs.build import script_compile

    def fake_compile(_db, host_id, *, addon, name, source, rel, **_kw):
        seen["compile"] = {"host_id": host_id, "addon": addon, "name": name, "rel": rel,
                           "source": Path(source).read_text(encoding="utf-8")}
        artifact = Path(_kw.get("_dir", seen["dir"])) / "hermes_sandbox.vjs_c"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"vjs_c")
        return script_compile.CompiledScript(
            run=m.BuildRun(id="b-bundle", host_id=host_id, command="rc", status="succeeded", owner="build:x",
                           exit_code=0, summary={}, created_at="2026-10-27T00:00:00Z",
                           updated_at="2026-10-27T00:00:00Z"),
            path=artifact, md5="b" * 32, size=5,
            remote="C:/cs2/game/csgo_addons/hermes_sandbox/maps/scripts/hermes_sandbox.vjs_c",
        )

    def fake_deploy(_db, host_id, *, addon, name, compiled, rel, **_kw):
        seen["deploy"] = {"host_id": host_id, "addon": addon, "name": name, "rel": rel,
                          "compiled": str(compiled)}
        return m.Deployment(
            id="d-bundle", host_id=host_id, addon=addon, dest_root="C:/cs2/game/csgo",
            entries=[m.DeployEntry(path=f"{rel}.vjs_c", size=5, md5="b" * 32,
                                   verified=deploy_status == "verified")],
            status=deploy_status, owner="deploy:x", created_at="2026-10-27T00:00:00Z",
            updated_at="2026-10-27T00:00:00Z",
        )

    monkeypatch.setattr(script_compile, "compile_js", fake_compile)
    monkeypatch.setattr(script_compile, "deploy_loose", fake_deploy)


def test_ship_compiles_the_one_sandbox_target_and_copies_it_loose(db, monkeypatch, tmp_path):
    from s2docs.build import script_compile

    seen: dict = {"dir": str(tmp_path)}
    _patch_compile(monkeypatch, seen)
    out = sandbox_run.ship(db, host_id="pomps-pc", compile_host_id="pomps-pc", user=_module())
    assert seen["compile"]["rel"] == script_compile.SANDBOX_REL
    assert seen["compile"]["addon"] == script_compile.SANDBOX_ADDON
    assert '[user:" + "demo" + "] ready " + "abc123"' in seen["compile"]["source"]
    assert 'hsUserApi("demo", "abc123")' in seen["compile"]["source"]
    assert seen["deploy"]["rel"] == script_compile.SANDBOX_REL and seen["deploy"]["host_id"] == "pomps-pc"
    assert out["deploy_status"] == "verified" and out["md5"] == "b" * 32


def test_a_bundle_that_did_not_verify_is_an_error_not_a_silent_run(db, monkeypatch, tmp_path):
    seen: dict = {"dir": str(tmp_path)}
    _patch_compile(monkeypatch, seen, deploy_status="failed")
    with pytest.raises(LiveError, match="did not verify"):
        sandbox_run.ship(db, host_id="pomps-pc", compile_host_id="pomps-pc", user=_module())


def test_run_user_script_refuses_a_bad_module_before_touching_a_host(db, monkeypatch):
    monkeypatch.setattr(sandbox_run, "ship", lambda *a, **kw: pytest.fail("must not ship"))

    with pytest.raises(sandbox_user.UserScriptRefused, match="Instance"):
        sandbox_run.run_user_script(db, host_id="pomps-pc", name=MODULE,
                                    code="Instance.Msg('x'); function start(api) {}")


def test_run_user_script_ships_and_then_reads_back(db, monkeypatch, tmp_path):
    seen: dict = {"dir": str(tmp_path)}
    _patch_compile(monkeypatch, seen)
    client = FakeClient(load_text="[user:demo] ready abc123\n[hs] pong 1\n")
    out = sandbox_run.run_user_script(db, host_id="pomps-pc", name=MODULE, code=CODE, nonce="abc123",
                                      client=client)
    assert out["status"] == "verified" and out["nonce"] == "abc123" and out["bundled"] is True


def test_restore_ships_the_stock_script_and_needs_only_a_pong(db, monkeypatch, tmp_path):
    seen: dict = {"dir": str(tmp_path)}
    _patch_compile(monkeypatch, seen)
    client = FakeClient(load_text="[hs] pong 0\n")
    out = sandbox_run.restore_sandbox(db, host_id="pomps-pc", compile_host_id="pomps-pc", client=client)
    assert out["status"] == "verified" and out["bundled"] is False
    assert 'hsUserApi("' not in seen["compile"]["source"] and " ready " not in seen["compile"]["source"]


def test_the_bundle_is_staged_inside_the_data_dir(db):
    path = sandbox_run.stage(_module())
    assert path.parent == get_settings().data_dir / "live_sandbox_run"
    assert '[user:" + "demo" + "] ready " + "abc123"' in path.read_text(encoding="utf-8")
