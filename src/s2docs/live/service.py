"""The one place live actions run. REST, MCP and the CLI call act()/status(); none of them talk to netcon."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3

from s2docs import hosts
from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.live import recipes, spawnables
from s2docs.live.intent import IntentEnvelope
from s2docs.live.netcon import NetconClient, NetconError

SANDBOX = "hermes_sandbox"
_POSE = re.compile(r"setpos (-?[\d.]+) (-?[\d.]+) (-?[\d.]+);setang (-?[\d.]+) (-?[\d.]+)")
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


def _check(expect: list[dict], reply: str, readback: str) -> tuple[bool, str]:
    """Bot/entity counts are judged on the readback round trip only; reply patterns on the action's own output."""
    for e in expect:
        if e["check"] == "bot_count":
            n = len(_BOT_ROW.findall(readback))
            if n != e["total"]:
                return False, f"expected {e['total']} bots in status, saw {n}"
        elif e["check"] == "reply" and not re.search(e["pattern"], reply):
            return False, f"no console line matching {e['pattern']!r}"
        elif e["check"] == "ent_count":
            n = readback.count(e["classname"])
            if n < e["min"]:
                return False, f"ent_find listed {n} {e['classname']}, expected >= {e['min']}"
    return True, "all readback checks passed"


def _readback_lines(expect: list[dict]) -> list[str]:
    out = []
    if any(e["check"] == "bot_count" for e in expect):
        out += ["sleep 3", "status"]
    out += [f"ent_find {e['classname']}" for e in expect if e["check"] == "ent_count"]
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
            hit = _POSE.search(client.run(["sv_cheats 1", "getpos"], drain=0.8))
            if hit:
                x, y, z, _pitch, yaw = (float(v) for v in hit.groups())
                pose = recipes.Pose(x, y, z, yaw)
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
    return m.LiveHostStatus(host_id=host_id, netcon=where, reachable=True, map=groups[0] if groups else None,
                            spawngroups=groups, sandbox="[hs] pong" in reply, listening=listening)


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
