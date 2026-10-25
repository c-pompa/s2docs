"""The one place live actions run. REST, MCP and the CLI call act()/status(); none of them talk to netcon."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3

from s2docs import deploys, hosts
from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.live import capabilities, recipes, spawnables
from s2docs.live.intent import IntentEnvelope
from s2docs.live.netcon import NetconClient, NetconError

SANDBOX = "hermes_sandbox"
# The one file whose absence makes `spawn_group_load` kick the client and kill the server.
SANDBOX_VPK = "maps/hermes_sandbox.vpk"
# What "load the sandbox" sends: cheats on, the group streamed in, then the readback round trip.
LOAD_LINES = ["sv_cheats 1", f"spawn_group_load {SANDBOX}", "sleep 3", "status", "hs_ping"]
# `getpos_exact`, measured 2026-10-25 on pomps-pc (CS2 1.41.9.2/14192):
#   setpos_exact -428.000000 -843.000000 95.296288;setang_exact 0.000000 133.500031 0.000000
# That z is the FEET. Plain `getpos` prints the eye at 156.874420 for the same standing sample, so its line
# is deliberately NOT matched here: a +61.58 offset is not the +64 the old recipe assumed.
_POSE = re.compile(r"setpos_exact (-?[\d.]+) (-?[\d.]+) (-?[\d.]+);setang_exact (-?[\d.]+) (-?[\d.]+)")
_SPAWNGROUP = re.compile(r"loaded spawngroup\(\s*\d+\)\s*:\s*SV:\s*\[\d+:\s*([A-Za-z0-9_]+)")
# One `status` PLAYER ROW of a bot, fitted to S0 (cache/scratch/live/s0.out):
# `  10      BOT    0    0     active      0 'Colin'` -- BOT is the 2nd column, the quoted name is last.
# Join lines (`ClientPutInServer ... [Colin]`, `"Colin<10><BOT>..."`) never match.
_BOT_ROW = re.compile(r"^\s*\d+\s+BOT\s+\d+\s+\d+\s+\S+\s+\d+\s+'[^']*'\s*$", re.M)
_ADDR = re.compile(r"^[A-Za-z0-9.-]{1,253}:\d{2,5}$")
READBACK_MAX = 4000


class LiveError(ValueError):
    """The host cannot take a live action right now (no netcon address, leased by a test run)."""


def set_netcon(db: Database, host_id: str, addr: str) -> str:
    hosts.get_host(db, host_id)
    if not _ADDR.fullmatch(addr or ""):
        raise LiveError(f"netcon address must be host:port, got {addr!r}")
    with db.writer() as conn, conn:
        conn.execute("INSERT INTO live_hosts (host_id, netcon_addr) VALUES (?, ?) ON CONFLICT(host_id) DO UPDATE"
                     " SET netcon_addr = excluded.netcon_addr, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
                     (host_id, addr))
    return addr


def client_for(db: Database, host_id: str) -> NetconClient:
    with db.reader() as conn:
        row = conn.execute("SELECT netcon_addr FROM live_hosts WHERE host_id = ?", (host_id,)).fetchone()
    if row is None:
        raise LiveError(
            f"no netcon address for {host_id!r}: PUT /api/live/hosts/{host_id} {{\"netcon_addr\": \"IP:2121\"}}"
        )
    ip, _, port = row["netcon_addr"].rpartition(":")
    return NetconClient(ip, int(port))


def _check_lease(db: Database, host_id: str) -> None:
    host = hosts.get_host(db, host_id)  # raises NotFound for an unknown host
    if host.lock and not str(host.lock.owner).startswith("live:"):
        raise LiveError(f"{host_id} is leased by {host.lock.owner} — a test run owns it; try after it ends")


def _sandbox_live(client) -> bool:
    return "[hs] pong" in client.run(["hs_ping"], drain=0.8, settle=0.3)


def parse_pose(text: str) -> recipes.Pose | None:
    """The player's FEET and yaw from a `getpos_exact` reply, or None when the line is not there.

    `getpos` must not be accepted: it reports the eye (z 156.87 against the feet's 95.30 in the measured
    sample), and subtracting a guessed eye height put every console spawn ~2.4 units underground.
    """
    hit = _POSE.search(text)
    if not hit:
        return None
    x, y, z, _pitch, yaw = (float(v) for v in hit.groups())
    return recipes.Pose(x, y, z, yaw)


def _check(expect: list[dict], reply: str, readback: str) -> tuple[bool, str]:
    """Bot/entity counts are judged on the readback round trip only; reply patterns on the action's own output."""
    for e in expect:
        if e["check"] == "bot_count":
            n = len(_BOT_ROW.findall(readback))
            if n != e["total"]:
                return False, f"expected {e['total']} bots in status, saw {n}"
        elif e["check"] == "reply" and not re.search(e["pattern"], reply):
            # Say what the game actually answered: `[hs] error no human player` is the useful part of a
            # failure, and it used to be buried in `readback` only.
            said = next((ln.strip() for ln in reply.splitlines() if ln.strip().startswith("[hs]")), "")
            wextra = f"; the console said: {said[:200]}" if said else ""
            return False, f"no console line matching {e['pattern']!r}{wextra}"
        elif e["check"] == "clear":
            # Verified by the removal count when it is >= 1 (`Removed 3 chickens` / `[hs] cleared 4`);
            # otherwise nothing was removed (or the line is absent), so the entity rows must be gone.
            hit = re.search(e["count"], reply)
            if hit and int(hit.group(1)) >= 1:
                continue
            classname = e.get("classname")
            if not classname:
                return False, f"no removal count line matching {e['count']!r}"
            row = re.compile(rf"^\s*'{re.escape(classname)}'\s*:", re.M)
            n = len(row.findall(readback))
            if n != 0:
                return False, f"ent_find still lists {n} {classname} after the clear"
        elif e["check"] == "ent_count":
            # Count ENTITY ROWS (`   'chicken' : '' (entindex 354)`, measured in S1), never the bare class name:
            # ent_find's header line `...containing substrings: "chicken"` contains it even when nothing exists.
            row = re.compile(rf"^\s*'{re.escape(e['classname'])}'\s*:", re.M)
            n = len(row.findall(readback))
            if n < e["min"]:
                return False, f"ent_find listed {n} {e['classname']}, expected >= {e['min']}"
    return True, "all readback checks passed"


def _readback_lines(expect: list[dict]) -> list[str]:
    """The second round trip: player rows, and `ent_find` for anything judged by entity rows."""
    out = []
    if any(e["check"] == "bot_count" for e in expect):
        out += ["sleep 3", "status"]
    classes = [e["classname"] for e in expect
               if e["check"] == "ent_count" or (e["check"] == "clear" and e.get("classname"))]
    out += [f"ent_find {c}" for c in dict.fromkeys(classes)]  # one line per class, in order
    return out


def _insert(db: Database, host_id: str, *, source: str, text: str | None, env: IntentEnvelope, lines: list[str],
            status: str, detail: str | None, readback: str | None, request_id: str | None) -> m.LiveAction:
    aid = "la-" + secrets.token_hex(4)
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO live_actions (id, host_id, source, text, intents_json, lines_json, status, detail, readback,"
            " request_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, host_id, source, text, env.model_dump_json(), json.dumps(lines), status, detail,
             (readback or "")[-READBACK_MAX:] or None, request_id),
        )
    return get_action(db, aid)


def act(db: Database, host_id: str, env: IntentEnvelope, *, source: str, text: str | None = None,
        request_id: str | None = None, client=None) -> m.LiveAction:
    _check_lease(db, host_id)
    client = client or client_for(db, host_id)
    stock = spawnables.stock()
    try:
        sandbox = _sandbox_live(client)
        pose = None
        if recipes.needs_pose(env, stock, sandbox=sandbox):
            pose = parse_pose(client.run(["sv_cheats 1", "getpos_exact"], drain=0.8))
        plan = recipes.compile_plan(env, stock, sandbox=sandbox, pose=pose)
    except recipes.LiveUnavailable as exc:
        return _insert(db, host_id, source=source, text=text, env=env, lines=[], status="refused",
                       detail=str(exc), readback=None, request_id=request_id)
    except NetconError as exc:
        raise LiveError(str(exc)) from exc
    try:
        reply = client.run(plan.lines, drain=0.6)
        checks = _readback_lines(plan.expect)
        readback = client.run(checks, drain=0.8, settle=0.3) if checks else ""
    except NetconError as exc:
        return _insert(db, host_id, source=source, text=text, env=env, lines=plan.lines, status="failed",
                       detail=str(exc), readback=None, request_id=request_id)
    ok, why = _check(plan.expect, reply, readback) if plan.expect else (True, "sent; this recipe has no readback")
    status = ("verified" if plan.expect else "sent") if ok else "failed"
    return _insert(db, host_id, source=source, text=text, env=env, lines=plan.lines, status=status,
                   detail="; ".join(plan.summary + plan.warnings + [why]),
                   readback=(reply + "\n--- readback ---\n" + readback), request_id=request_id)


def load_sandbox(db: Database, host_id: str, deploy_id: str, *, client=None) -> dict:
    """Stream the helper map onto the running game, from a verified deployment that carries its vpk.

    The deployment is checked before a single line is sent: a missing `maps/hermes_sandbox.vpk` makes
    `spawn_group_load` kick the client and kill the server. The verdict is the readback (`status`
    spawngroups plus `[hs] pong`), never the fact that the lines went out.
    """
    _check_lease(db, host_id)
    dep = deploys.get_deployment(db, deploy_id)
    if dep.host_id != host_id:
        raise LiveError(f"deployment {deploy_id} is on {dep.host_id}, not {host_id}")
    if dep.status != "verified":
        raise LiveError(f"deployment {deploy_id} is not verified ({dep.status}): push and verify it first")
    if SANDBOX_VPK not in {e.path for e in dep.entries}:
        raise LiveError(f"deployment {deploy_id} has no {SANDBOX_VPK}: a missing vpk makes "
                        "spawn_group_load kick the client and kill the server")
    client = client or client_for(db, host_id)
    try:
        reply = client.run(LOAD_LINES, drain=0.8, settle=0.3)
    except NetconError as exc:
        raise LiveError(str(exc)) from exc
    groups = _SPAWNGROUP.findall(reply)
    if SANDBOX in groups and "[hs] pong" in reply:
        status, detail = "verified", f"spawngroup {SANDBOX} loaded and [hs] pong answered"
    elif SANDBOX not in groups:
        status, detail = "failed", f"{SANDBOX} is not in the loaded spawngroups ({', '.join(groups) or 'none'})"
    else:
        status, detail = "failed", f"spawngroup {SANDBOX} loaded but no [hs] pong (the script did not run)"
    return {"host_id": host_id, "deploy_id": dep.id, "status": status, "detail": detail,
            "spawngroups": groups, "lines": list(LOAD_LINES), "readback": reply[-READBACK_MAX:]}


def plan_only(env: IntentEnvelope, *, sandbox: bool = True) -> dict:
    """Dry run for previews: no host contact; reports needs_sandbox instead of checking it."""
    return recipes.compile_plan(env, spawnables.stock(), sandbox=sandbox).as_dict()


def status(db: Database, host_id: str, *, client=None, listening: bool = False) -> m.LiveHostStatus:
    hosts.get_host(db, host_id)
    client = client or client_for(db, host_id)
    where = f"{client.host}:{client.port}"
    try:
        reply = client.run(["status", "hs_ping"], drain=0.8, settle=0.3)
    except NetconError as exc:
        return m.LiveHostStatus(host_id=host_id, netcon=where, reachable=False, sandbox=False, error=str(exc))
    groups = _SPAWNGROUP.findall(reply)
    try:
        build: str | None = capabilities.cs2_version(reply)
    except capabilities.CapabilityError:
        build = None  # reachable but not in a map yet: no version line to key the capability ledger on
    return m.LiveHostStatus(host_id=host_id, netcon=where, reachable=True, map=groups[0] if groups else None,
                            spawngroups=groups, sandbox="[hs] pong" in reply, listening=listening, cs2_build=build)


def capability_report(db: Database, host_id: str, *, client=None) -> dict:
    """The capability ledger for the build this host is running. A missing build key is not a server fault."""
    hosts.get_host(db, host_id)
    client = client or client_for(db, host_id)
    try:
        reply = client.run(["status"], drain=0.8, settle=0.3)
    except NetconError as exc:
        raise LiveError(f"cannot read the CS2 build on {host_id} over netcon: {exc}") from exc
    try:
        build: str | None = capabilities.cs2_version(reply)
    except capabilities.CapabilityError as exc:
        return {"cs2_build": None, "error": str(exc), "capabilities": capabilities.table(db, "")}
    return {"cs2_build": build, "capabilities": capabilities.table(db, build)}


def _row(r: sqlite3.Row) -> m.LiveAction:
    return m.LiveAction(id=r["id"], host_id=r["host_id"], source=r["source"], text=r["text"],
                        intents=json.loads(r["intents_json"]), lines=json.loads(r["lines_json"]), status=r["status"],
                        detail=r["detail"], readback=r["readback"], request_id=r["request_id"],
                        created_at=r["created_at"])


def get_action(db: Database, action_id: str) -> m.LiveAction:
    with db.reader() as conn:
        r = conn.execute("SELECT * FROM live_actions WHERE id = ?", (action_id,)).fetchone()
    if r is None:
        from s2docs.db.repository import NotFound
        raise NotFound(f"live action {action_id!r}")
    return _row(r)


def list_actions(db: Database, host_id: str | None = None, *, limit: int = 50) -> list[m.LiveAction]:
    sql, args = "SELECT * FROM live_actions", []
    if host_id:
        sql, args = sql + " WHERE host_id = ?", [host_id]
    with db.reader() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    return [_row(r) for r in rows]


def list_netcon(db: Database) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute("SELECT host_id, netcon_addr FROM live_hosts ORDER BY host_id").fetchall()
    return [{"host_id": r["host_id"], "netcon_addr": r["netcon_addr"]} for r in rows]
