"""The user-module contract + the bundle it is wrapped into (offline; node for the behaviour half).

Two rules this file pins:

- a user module never touches ``Instance``: the ``api`` object is the whole surface, because a second
  ``Instance.OnPlayerChat`` registration would overwrite the runtime's own. The checker scans code with
  comments and string literals blanked, so a message that says "Instance" is fine and a comment that
  hides it is not;
- the stock sandbox commands keep working inside the bundle, and the module reports ``ready <nonce>`` or
  ``error <message>`` on load — that line is the only mechanical proof a build is the one the engine ran.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from s2docs.live import recipes, sandbox_map, sandbox_user, spawnables

NODE = shutil.which("node")

GOOD = """\
// a demo module
function start(api) {
  api.msg("hello");
  api.command("hello", (args) => api.msg("hi " + (args || "world")));
  api.onChat((e) => { if (e.text === "!hi") api.msg("you said hi"); });
}
"""


def _check(code: str, name: str = "demo", nonce: str = "abc123") -> sandbox_user.UserModule:
    return sandbox_user.check_user_module(textwrap.dedent(code), name=name, nonce=nonce)


# ------------------------------------------------------------------ the contract
@pytest.mark.parametrize("snippet, token", [
    ("Instance.Msg('x');", "Instance"),
    ("import { Instance } from 'cs_script/point_script';", "import"),
    ("export function start(api) {}", "export"),
    ("const x = require('fs');", "require"),
    ("eval('1');", "eval"),
    ("const f = new Function('return 1');", "Function"),
    ("globalThis.x = 1;", "globalThis"),
    ("process.exit(0);", "process"),
    ("console.log('x');", "console"),
    ("window.alert('x');", "window"),
    ("document.title = 'x';", "document"),
])
def test_the_checker_refuses_every_token_outside_the_contract(snippet, token):
    code = f"function start(api) {{ {snippet} }}\n"
    with pytest.raises(sandbox_user.UserScriptRefused) as err:
        _check(code)
    assert any(r.startswith(token + ":") for r in err.value.reasons), err.value.reasons


def test_the_checker_refuses_a_missing_start_and_says_what_to_write():
    with pytest.raises(sandbox_user.UserScriptRefused) as err:
        _check("function go(api) { api.msg('x'); }\n")
    assert "function start(api)" in err.value.reasons[0]


def test_the_checker_refuses_template_literals_instead_of_blanking_them():
    """`${...}` is code: blanking it would let `Instance` hide inside a literal."""
    with pytest.raises(sandbox_user.UserScriptRefused) as err:
        _check("function start(api) { const s = `${api}`; api.msg(s); }\n")
    assert any(r.startswith("template literal") for r in err.value.reasons)


def test_the_checker_refuses_an_empty_module_a_bad_name_and_an_oversize_one():
    with pytest.raises(sandbox_user.UserScriptRefused, match="empty"):
        _check("   \n")
    with pytest.raises(sandbox_user.UserScriptRefused, match="invalid name"):
        _check(GOOD, name="Demo!")
    big = "function start(api) {\n" + "  api.msg('x');\n" * 5000 + "}\n"
    with pytest.raises(sandbox_user.UserScriptRefused, match="limit"):
        _check(big)


def test_a_name_in_a_comment_or_a_string_is_not_an_access():
    code = """\
    // Instance is what the runtime owns; this module does not touch it
    /* Instance.OnPlayerChat would overwrite the runtime's registration */
    function start(api) { api.msg("Instance is not mine to use"); }
    """
    module = _check(code)
    assert module.name == "demo" and module.nonce == "abc123"


def test_stop_is_optional_and_a_minted_nonce_is_per_build():
    first = sandbox_user.check_user_module(GOOD, name="demo")
    second = sandbox_user.check_user_module(GOOD, name="demo")
    assert first.nonce != second.nonce and len(first.nonce) >= 8
    assert sandbox_user.has_stop(first) is False
    with_stop = _check(GOOD + "function stop(api) { api.msg('bye'); }\n")
    assert sandbox_user.has_stop(with_stop) is True
    assert with_stop.ready_line == f"[user:demo] ready {with_stop.nonce}"


# -------------------------------------------------------------------- the bundle
def test_the_bundle_carries_the_runtime_the_module_and_the_nonce():
    module = _check(GOOD)
    bundle = sandbox_map.render_script(spawnables.stock(), module)
    assert "Instance.OnPlayerChat" in bundle and 'Instance.RegisterCheatCommand("hs_ping"' in bundle
    assert 'hsUserApi("demo", "abc123")' in bundle and "function start(api)" in bundle
    assert '[user:" + "demo" + "] ready " + "abc123"' in bundle
    assert "import { Instance } from \"cs_script/point_script\";" in bundle


def test_the_stock_bundle_is_the_runtime_without_any_module():
    stock = sandbox_map.render_script(spawnables.stock())
    assert "/*USER*/" not in stock
    # the runtime defines hsUserApi, but no module is invoked and nothing reports ready
    assert not re.search(r'hsUserApi\("[a-z]', stock) and " ready " not in stock
    assert stock == sandbox_map.render_script(spawnables.stock(), None)
    module = _check(GOOD)
    assert sandbox_map.render_script(spawnables.stock(), module).startswith(stock.rstrip())


def test_write_addon_writes_the_bundle_when_a_module_is_given(tmp_path):
    module = _check(GOOD)
    out = sandbox_map.write_addon(tmp_path / "hermes_sandbox", spawnables.stock(), module)
    assert out[1].read_text(encoding="utf-8") == sandbox_map.render_script(spawnables.stock(), module)


def test_the_allowlist_takes_the_reload_lines_and_the_prefixed_user_commands():
    assert recipes.ALLOWED.fullmatch("map de_dust2")
    assert recipes.ALLOWED.fullmatch("status")
    assert recipes.ALLOWED.fullmatch("hu_hello") and recipes.ALLOWED.fullmatch("hu_hello bob")
    assert recipes.ALLOWED.fullmatch("spawn_group_load hermes_sandbox")
    # ... and nothing wider: one map, no other console command, lowercase command names only
    assert not recipes.ALLOWED.fullmatch("map de_inferno")
    assert not recipes.ALLOWED.fullmatch("hu_Hello")
    assert not recipes.ALLOWED.fullmatch("hu_hello;quit")
    assert not recipes.ALLOWED.fullmatch("exec autoexec.cfg")


# ------------------------------------------------------------- node behaviour test
STUB = """\
const out = [];
const cmds = {};
let chat = null;
const Instance = {
  Msg(t) { out.push(String(t)); },
  RegisterCheatCommand(name, fn) { cmds[name] = fn; },
  OnPlayerChat(cb) { chat = cb; },
  GetAllPlayerControllers() { return []; },
  SetThink(fn) { globalThis.__think = fn; },
  SetNextThink() {},
  GetGameTime() { return 0; },
  OnActivate() {},
  FindEntityByName() { return undefined; },
  Precache() { return undefined; },
};
"""

HARNESS = """\

const say = (k, v) => console.log(k + ":" + v);
const call = (label, name, args) => {
  if (!cmds[name]) { say(label, "MISSING " + name); return; }
  cmds[name](args); say(label, out[out.length - 1]);
};
say("CMDS", Object.keys(cmds).sort().join(","));
say("OUT", out.join("|"));
call("PING", "hs_ping");
call("HELLO", "hu_hello");
call("HELLO2", "hu_hello", "bob");
chat({ text: "!hi", player: undefined });
say("CHAT", out[out.length - 1]);
chat({ text: "!hermes a normal message", player: undefined });
say("ECHO", out[out.length - 1]);
"""


def _run_bundle(tmp_path: Path, module: sandbox_user.UserModule) -> dict[str, str]:
    body = "\n".join(
        line for line in sandbox_map.render_script(spawnables.stock(), module).splitlines()
        if not line.startswith("import ")
    )
    script = STUB + body + HARNESS
    path = tmp_path / "run.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return dict(line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_bundle_runs_the_module_and_keeps_the_stock_commands(tmp_path):
    seen = _run_bundle(tmp_path, _check(GOOD))
    assert "hs_ping" in seen["CMDS"] and "hs_spawn" in seen["CMDS"] and "hs_model" in seen["CMDS"]
    assert "hu_hello" in seen["CMDS"] and "hu_stop" not in seen["CMDS"]  # no stop() -> no hu_stop
    assert "[user:demo] ready abc123" in seen["OUT"].split("|")
    assert "[user:demo] hello" in seen["OUT"].split("|")
    # the stock runtime still answers, and the module's own command/chat hooks are wired
    assert seen["PING"] == "[hs] pong 0"
    assert seen["HELLO"] == "[user:demo] hi world"
    assert seen["HELLO2"] == "[user:demo] hi bob"
    assert seen["CHAT"] == "[user:demo] you said hi"
    assert seen["ECHO"] == "[hs] chat -1 a normal message"  # the runtime's !h echo still fires


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_throwing_start_reports_error_and_never_the_ready_line(tmp_path):
    boom = _check("function start(api) { throw new Error('boom'); }\n")
    seen = _run_bundle(tmp_path, boom)
    assert seen["OUT"] == "[user:demo] error boom"
    assert "ready" not in seen["OUT"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_module_defining_stop_gets_a_prefixed_hu_stop_command(tmp_path):
    module = _check(GOOD + "function stop(api) { api.msg('bye'); }\n")
    seen = _run_bundle(tmp_path, module)
    assert "hu_stop" in seen["CMDS"]


def test_a_start_only_module_gets_no_stop_call_in_the_bundle():
    """tsc reads a bare `stop` as the lib's 0-arg global: never emit `stop(__hsApi)` for a start-only module."""
    bundle = sandbox_map.render_script(spawnables.stock(), _check(GOOD))
    assert "stop(__hsApi)" not in bundle and "hu_stop" not in bundle
    with_stop = sandbox_map.render_script(spawnables.stock(), _check(GOOD + "function stop(api) { api.msg('bye'); }\n"))
    assert "stop(__hsApi)" in with_stop


def test_a_stop_in_a_comment_or_string_does_not_wire_hu_stop():
    module = _check(GOOD + "// function stop(api) {}\nconst note = 'function stop(api) {}';\n")
    assert "hu_stop" not in sandbox_map.render_script(spawnables.stock(), module)
