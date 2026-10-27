"""Run a user cs_script on a live host: bundle -> compile -> loose deploy -> reload -> readback verdict.

Why this route and not a map compile (measured 2026-10-27):

* `hermes_sandbox.vpk` carries the world/entity lumps and **no script**; its `point_script` names
  `maps/scripts/hermes_sandbox.vjs`, which the engine resolves to the LOOSE
  `<CS2>/game/csgo/maps/scripts/hermes_sandbox.vjs_c` — a file replaced by one `resourcecompiler` pass of
  that single `.js` (~1 s, no display adapter, works from ssh's session 0);
* a `.vmap` compile needs an Active session on desktop-pc, and pomps-pc (the host that plays) must never
  compile a map — so bundling is the only route that can put a *new* script in game today.

The verdict is mechanical and readback-only: the bundle prints `[user:<name>] ready <nonce>` from the module
and nonce of THIS build, so that line can only appear if this build is what the engine loaded. `verified`
needs that line **and** a `[hs] pong` from the stock runtime in the same readback; an `error` line, a pong
without a ready line (the script ran, the module did not) and silence are all `failed`. A console line alone
never closes anything as a pass — the job still asks a human.
"""

from __future__ import annotations

import re
from pathlib import Path

from s2docs import models as m
from s2docs.build import script_compile
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.live import recipes, sandbox_map, sandbox_user, spawnables
from s2docs.live.service import SANDBOX, LiveError

#: The stock map the helper group is streamed onto. Never modified: the group carries its own entities.
MAP = "de_dust2"

#: `map de_dust2` again is the safe reload: it re-reads the loose `.vjs_c` from disk, which is the point.
#: (`ent_fire`/`script_reload` do not re-load the file.) Sent ONLY after the spawn readback below.
RELOAD_LINES = (f"map {MAP}",)

#: The engine drops commands sent while a map is spawning (measured pitfall), so the wait is a readback:
#: `status` prints `@ Current  :  game` once the game is up and lists the map as `1: de_dust2`.
MAP_READY = re.compile(r"@\s*Current\s*:\s*game\b")
MAP_NAMED = re.compile(rf"\b1:\s*{re.escape(MAP)}\b")
PONG = "[hs] pong"
READBACK_MAX = 4000

#: How long a reload may take before the verdict is "the engine did not finish loading".
LOAD_TIMEOUT_SEC = 90


def _ready(name: str, nonce: str) -> re.Pattern[str]:
    return re.compile(rf"^\[user:{re.escape(name)}\] ready {re.escape(nonce)}\s*$", re.M)


def _error(name: str) -> re.Pattern[str]:
    return re.compile(rf"^\[user:{re.escape(name)}\] error (.+)$", re.M)


def render(user: sandbox_user.UserModule | None) -> str:
    """The script to ship: the stock runtime with the user's module bundled in (or plain, for a restore)."""
    return sandbox_map.render_script(spawnables.stock(), user)


def stage(user: sandbox_user.UserModule | None) -> Path:
    """Write the bundle into the data dir (server-derived path) and return it."""
    out = get_settings().data_dir / "live_sandbox_run" / "hermes_sandbox.js"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render(user).encode("utf-8"))
    return out


def compile_bundle(db: Database, *, host_id: str, user: sandbox_user.UserModule | None,
                   owner: str | None = None, timeout_sec: int = 120,
                   poll_sec: float = 1.0) -> script_compile.CompiledScript:
    """Render + compile the bundle as hermes_sandbox's own map script on ``host_id``.

    The target is the ONE non-``scripts/`` path :mod:`s2docs.build.script_compile` accepts
    (``maps/scripts/hermes_sandbox``), which keeps this path from ever becoming a map compile.
    """
    source = stage(user)
    try:
        return script_compile.compile_js(
            db, host_id, addon=script_compile.SANDBOX_ADDON, name=script_compile.SANDBOX_ADDON,
            source=source, rel=script_compile.SANDBOX_REL, timeout_sec=timeout_sec, poll_sec=poll_sec,
            owner=owner,
        )
    except script_compile.ScriptCompileError as exc:
        raise LiveError(f"compiling the sandbox bundle failed: {exc}") from exc


def deploy_bundle(db: Database, *, host_id: str, compiled: script_compile.CompiledScript,
                  owner: str | None = None) -> m.Deployment:
    """Copy the compiled bundle LOOSE to ``<CS2>/game/csgo/maps/scripts/hermes_sandbox.vjs_c``."""
    try:
        return script_compile.deploy_loose(
            db, host_id, addon=script_compile.SANDBOX_ADDON, name=script_compile.SANDBOX_ADDON,
            compiled=compiled.path, rel=script_compile.SANDBOX_REL, owner=owner,
        )
    except script_compile.ScriptCompileError as exc:
        raise LiveError(f"deploying the sandbox bundle failed: {exc}") from exc


def ship(db: Database, *, host_id: str, compile_host_id: str | None, user: sandbox_user.UserModule | None,
         owner: str | None = None, timeout_sec: int = 120, poll_sec: float = 1.0) -> dict:
    """Compile the bundle, then copy it loose to ``host_id`` — the two halves the pipeline records apart.

    A deploy that is not `verified` is an error here, because the reload would otherwise prove nothing.
    """
    compiled = compile_bundle(db, host_id=compile_host_id or host_id, user=user, owner=owner,
                              timeout_sec=timeout_sec, poll_sec=poll_sec)
    deployment = deploy_bundle(db, host_id=host_id, compiled=compiled, owner=owner)
    if deployment.status != "verified":
        raise LiveError(
            f"the bundle did not verify on {host_id} (deploy {deployment.status}): the host does not have "
            "the .vjs_c the reload would run"
        )
    return {
        "host_id": host_id, "build_id": compiled.run.id, "md5": compiled.md5, "size": compiled.size,
        "deploy_id": deployment.id, "deploy_status": deployment.status, "source": str(compiled.path),
        "remote": compiled.remote,
    }


def reload(db: Database, host_id: str, *, user: sandbox_user.UserModule | None,
           client=None, timeout_sec: int = LOAD_TIMEOUT_SEC) -> dict:
    """Reload the map, stream the sandbox group back in and read the console until the verdict is known.

    Two polls, each with its own connection: (1) `map de_dust2` then `status` until the map is up and named
    — nothing else may be sent before that (the engine drops it); (2) `sv_cheats 1` + `spawn_group_load
    hermes_sandbox` then `status`/`hs_ping` until the user module's `ready` line (or its `error`) arrives.
    The console stream only reaches CONNECTED clients, which is why each wait keeps one connection open.
    """
    from s2docs.live import service  # local: service imports live modules this one extends

    client = client or service.client_for(db, host_id)
    for line in (*RELOAD_LINES, "sv_cheats 1", f"spawn_group_load {SANDBOX}", "status", "hs_ping"):
        if not recipes.ALLOWED.fullmatch(line):
            raise LiveError(f"refusing a line outside the allowlist: {line!r}")

    spawned, first = client.poll(
        list(RELOAD_LINES), ["status"],
        lambda text: bool(MAP_READY.search(text) and MAP_NAMED.search(text)),
        timeout=timeout_sec,
    )
    lines = list(RELOAD_LINES)
    if not spawned:
        return {"status": "failed", "lines": lines,
                "detail": f"{MAP} did not finish spawning within {timeout_sec}s "
                          f"(`status` never showed '@ Current : game' + '1: {MAP}')",
                "readback": first[-READBACK_MAX:]}

    lines += ["sv_cheats 1", f"spawn_group_load {SANDBOX}", "status", "hs_ping"]
    if user is not None:
        ready = _ready(user.name, user.nonce)
        error = _error(user.name)

        def done(text: str) -> bool:
            return bool(ready.search(text) or error.search(text))
    else:
        def done(text: str) -> bool:
            return PONG in text

    _, second = client.poll(["sv_cheats 1", f"spawn_group_load {SANDBOX}"], ["status", "hs_ping"],
                            done, timeout=timeout_sec)
    readback = (first[-READBACK_MAX // 2:] + "\n--- reload ---\n" + second)[-READBACK_MAX:]
    status, detail = _judge(user, second)
    return {"status": status, "detail": detail, "lines": lines, "readback": readback}


def _judge(user: sandbox_user.UserModule | None, text: str) -> tuple[str, str]:
    """The verdict rule, in one place: a ready line AND a live runtime, never a lone console line."""
    pong = PONG in text
    if user is None:
        return ("verified", "the stock sandbox reloaded and hs_ping answered") if pong else (
            "failed", "no '[hs] pong' after the reload: the stock runtime did not come back")
    hit = _ready(user.name, user.nonce).search(text)
    error = _error(user.name).search(text)
    if hit and pong:
        return "verified", (f"[user:{user.name}] ready {user.nonce} and [hs] pong: this build is what the "
                            "engine loaded, and the stock sandbox still answers")
    if hit:
        return "failed", (f"[user:{user.name}] ready {user.nonce} but no [hs] pong: the module ran and the "
                          "stock runtime did not answer (hs_ping is broken in this bundle)")
    if error:
        return "failed", f"[user:{user.name}] error {error.group(1)[:300]}"
    if pong:
        return "failed", (f"[hs] pong answered but no '[user:{user.name}] ready {user.nonce}' line: the "
                          "script ran, the bundled module did not")
    return "failed", (f"neither '[user:{user.name}] ready {user.nonce}' nor '[hs] pong' within the timeout: "
                      "the reloaded script did not run")


def run_user_script(db: Database, *, host_id: str, name: str, code: str,
                    compile_host_id: str | None = None, nonce: str | None = None,
                    client=None, timeout_sec: int = LOAD_TIMEOUT_SEC, owner: str | None = None,
                    build_timeout_sec: int = 120, poll_sec: float = 1.0) -> dict:
    """Check, bundle, ship and run one user module on ``host_id``; returns the readback verdict.

    ``status`` is ``verified`` only on `[user:<name>] ready <nonce>` **and** `[hs] pong`. A module that
    breaks the contract raises :class:`s2docs.live.sandbox_user.UserScriptRefused` before any host is
    touched; a ship/reload that cannot run raises :class:`LiveError`.
    """
    user = sandbox_user.check_user_module(code, name=name, nonce=nonce)
    shipped = ship(db, host_id=host_id, compile_host_id=compile_host_id, user=user, owner=owner,
                   timeout_sec=build_timeout_sec, poll_sec=poll_sec)
    verdict = reload(db, host_id, user=user, client=client, timeout_sec=timeout_sec)
    return {**shipped, **verdict, "name": user.name, "nonce": user.nonce, "bundled": True}


def restore_sandbox(db: Database, *, host_id: str, compile_host_id: str | None = None, client=None,
                    timeout_sec: int = LOAD_TIMEOUT_SEC, owner: str | None = None,
                    build_timeout_sec: int = 120, poll_sec: float = 1.0) -> dict:
    """Put the STOCK script back on ``host_id``: same bundle, no user module, proved by `[hs] pong`.

    A host left running somebody's module is a host whose `hs_ping` answers with behaviour nobody asked for,
    so the pipeline restores automatically when told to and reports whether the restore verified.
    """
    shipped = ship(db, host_id=host_id, compile_host_id=compile_host_id, user=None, owner=owner,
                   timeout_sec=build_timeout_sec, poll_sec=poll_sec)
    verdict = reload(db, host_id, user=None, client=client, timeout_sec=timeout_sec)
    return {**shipped, **verdict, "name": None, "nonce": None, "bundled": False}
