"""IntentEnvelope -> console lines. Every line must fullmatch ALLOWED, or compile_plan raises."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from s2docs.live.formation import positions
from s2docs.live.intent import BotsIntent, ClearIntent, IntentEnvelope, SpawnIntent
from s2docs.live.spawnables import Spawnable

TEAM_NUM = {"spectator": 1, "t": 2, "ct": 3}
# The form spike S0 (Task 0.2) proved moves the LOCAL player over netcon. Change only with new evidence.
ME_TEAM_LINE = "jointeam {num} 1"
# MEASURED (pomps-pc, CS2 1.41.9.2/14192, 2026-10-25, standing still, two identical samples):
#   getpos        -> setpos -428 -843 156.874420      (the EYE: +61.58 over the feet, NOT the +64 the old
#                                                      guess subtracted)
#   getpos_exact  -> setpos_exact -428 -843 95.296288 (the FEET)
# So the console spawn reads `getpos_exact` and adds no offset at all. There is no eye-height constant.

_NUM = r"-?\d+(?:\.\d+)?"
# The console's own removal line, measured 2026-10-25 on pomps-pc: `Removed 3 chickens`.
REMOVED_RE = r"Removed (\d+) {cls}\b"
# The sandbox runtime's answer to `hs_clear`: `[hs] cleared 4`.
HS_CLEARED_RE = r"\[hs\] cleared (\d+)"
ALLOWED = re.compile(
    r"sv_cheats 1|mp_autoteambalance 0|mp_limitteams 0|bot_kick|bot_add_t|bot_add_ct|mp_warmup_end"
    r"|mp_restartgame 1|jointeam [123](?: 1)?|sleep \d+|hs_ping|spawn_group_load hermes_sandbox"
    r"|hs_spawn [a-z][a-z0-9_]* \d{1,2} (?:line|circle|stack|grid) \d+(?:\.\d+)? [01]"
    r"|hs_clear [a-z][a-z0-9_]*|hs_model models/[a-z0-9_/]+\.vmdl(?: \d+(?:\.\d+)?)?"
    r"|ent_remove_all [a-z_]+"
    rf'|ent_create [a-z_]+ \{{"origin" "{_NUM} {_NUM} {_NUM}"\}}'
    r"|say Hermes: [A-Za-z0-9 .,:'()/+-]{0,120}"
)


class LiveUnavailable(ValueError):
    """The request is valid but cannot run here (unknown id, sandbox not loaded, follow on console)."""


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    z: float  # FEET height, as getpos_exact prints it
    yaw: float


@dataclass
class Plan:
    lines: list[str] = field(default_factory=list)
    needs_sandbox: bool = False
    summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expect: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"lines": self.lines, "needs_sandbox": self.needs_sandbox, "summary": self.summary,
                "warnings": self.warnings, "expect": self.expect}


def needs_pose(env: IntentEnvelope, spawnables: dict[str, Spawnable], *, sandbox: bool) -> bool:
    """True when a console spawn needs a `getpos_exact` readback before compiling."""
    return not sandbox and any(isinstance(i, SpawnIntent) for i in env.intents)


def _sanitise(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 .,:'()/+-]", " ", text)
    return re.sub(r"\s+", " ", clean).strip()[:100]


def compile_plan(env: IntentEnvelope, spawnables: dict[str, Spawnable], *, sandbox: bool,
                 pose: Pose | None = None) -> Plan:
    plan = Plan()
    restarted = False
    cheats = False

    def cheat() -> None:
        nonlocal cheats
        if not cheats:
            plan.lines.append("sv_cheats 1")
            cheats = True

    for intent in env.intents:
        if isinstance(intent, BotsIntent):
            t, ct = intent.counts()
            plan.lines += ["mp_autoteambalance 0", "mp_limitteams 0", "bot_kick"]
            if intent.me:
                plan.lines.append(ME_TEAM_LINE.format(num=TEAM_NUM[intent.me]))
            plan.lines += ["bot_add_t"] * t + ["bot_add_ct"] * ct
            if intent.restart:
                plan.lines += ["mp_warmup_end", "mp_restartgame 1"]
                restarted = True
            plan.summary.append(f"bots: {t} T + {ct} CT" + (f", you on {intent.me.upper()}" if intent.me else ""))
            plan.expect.append({"check": "bot_count", "total": t + ct})
        elif isinstance(intent, SpawnIntent):
            spec = spawnables.get(intent.what)
            if spec is None:
                raise LiveUnavailable(f"unknown spawnable {intent.what!r}; see GET /api/live/spawnables")
            if restarted:
                plan.lines.append("sleep 3")  # a round restart removes what was spawned before it
                restarted = False
            cheat()
            if sandbox:
                plan.needs_sandbox = True
                plan.lines.append(f"hs_spawn {spec.id} {intent.count} {intent.formation} {intent.spacing:g} "
                                  f"{1 if intent.follow else 0}")
                plan.expect.append({"check": "reply", "pattern": rf"\[hs\] spawned {intent.count} {spec.id}"})
            else:
                if not spec.console_ok:
                    raise LiveUnavailable(f"{spec.label} needs the sandbox (load it from the Live tab)")
                if intent.follow:
                    raise LiveUnavailable("follow needs the sandbox runtime")
                if pose is None:
                    raise LiveUnavailable("a console spawn needs the player's position (getpos_exact)")
                for x, y, z in positions((pose.x, pose.y, pose.z), pose.yaw, intent.count, intent.formation,
                                         intent.spacing, spec.height):
                    plan.lines.append(f'ent_create {spec.classname} {{"origin" "{x} {y} {z}"}}')
                plan.expect.append({"check": "ent_count", "classname": spec.classname, "min": intent.count})
            plan.summary.append(f"spawn: {intent.count} {spec.label} ({intent.formation}"
                                f"{', following you' if intent.follow else ''})")
        elif isinstance(intent, ClearIntent):
            cheat()
            if sandbox:
                plan.needs_sandbox = True
                plan.lines.append(f"hs_clear {intent.what or 'all'}")
                spec = spawnables.get(intent.what) if intent.what else None
                if spec is not None:
                    # the count line, plus ent_find <class> as the fallback when nothing was removed
                    plan.expect.append({"check": "clear", "classname": spec.classname, "count": HS_CLEARED_RE})
                else:
                    # `hs_clear all` names no class to re-check: its own count line is the readback
                    plan.expect.append({"check": "reply", "pattern": r"\[hs\] cleared \d+"})
            else:
                targets = [spawnables[intent.what]] if intent.what in spawnables else list(spawnables.values())
                plan.lines += [f"ent_remove_all {s.classname}" for s in targets]
                plan.expect += [{"check": "clear", "classname": s.classname,
                                 "count": REMOVED_RE.format(cls=re.escape(s.classname))} for s in targets]
            plan.summary.append(f"clear: {intent.what or 'everything spawned'}")
    if env.say:
        plan.lines.append(f"say Hermes: {_sanitise(env.say)}")
    for line in plan.lines:
        if not ALLOWED.fullmatch(line):
            raise AssertionError(f"recipe produced a line outside the allowlist: {line!r}")
    return plan
