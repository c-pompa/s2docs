"""Validate a .cfg (console script) against the indexed convars/commands of a game build.

Checks per command:
- unknown name                      error, with closest indexed names as suggestions
- numeric value outside min/max     error
- flag `cheat` without sv_cheats 1  warning
- flag `developmentonly`            warning (usually unavailable in retail builds)
- client-only convar in a server cfg / server-only in a client cfg   warning
- convar without a value            info (prints the current value instead of setting it)
"""

from __future__ import annotations

import difflib
import re
from typing import Literal

from s2docs import models as m
from s2docs.db.repository import NotFound, Repository

# Commands whose arguments are other commands or free text: don't validate their arguments.
_PASSTHROUGH_ARGS = {"alias", "bind", "echo", "say", "say_team", "exec", "execifexists", "toggle", "incrementvar"}


def _split_commands(line: str) -> list[str]:
    """Split one cfg line on ';' outside quotes, dropping // comments."""
    out, cur, in_q, i = [], [], False, 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            in_q = not in_q
        elif not in_q and line.startswith("//", i):
            break
        elif not in_q and ch == ";":
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


def _tokens(cmd: str) -> list[str]:
    return [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', cmd)]


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def validate_cfg(
    repo: Repository, text: str, *, game: str, context: Literal["server", "client", "any"] = "server"
) -> m.CfgValidation:
    diags: list[m.Diagnostic] = []
    names: list[str] | None = None  # loaded lazily for typo suggestions
    cheats_on = False
    count = 0

    def add(line: int, sev: m.Severity, cat: str, msg: str, hint: str | None = None, code: str = "CFG") -> None:
        diags.append(m.Diagnostic(line=line, code=code, severity=sev, category=cat, message=msg, hint=hint))

    for lineno, line in enumerate(text.splitlines(), start=1):
        for cmd in _split_commands(line):
            toks = _tokens(cmd)
            if not toks:
                continue
            count += 1
            name, args = toks[0], toks[1:]
            try:
                cv = repo.get_convar(game, name)
            except NotFound:
                names = names if names is not None else repo.convar_names(game)
                close = difflib.get_close_matches(name.lower(), names, n=3, cutoff=0.75)
                if not close:
                    close = [h.name for h in repo.search(name, game=game, kinds=["convar", "command"], limit=3).items]
                add(
                    lineno,
                    "error",
                    "unknown_command",
                    f"'{name}' is not a known convar or command in {game}",
                    ("Did you mean: " + ", ".join(close)) if close else None,
                )
                continue
            flags = set(cv.flags)

            if cv.name == "sv_cheats" and args:
                cheats_on = args[0].strip().lower() in ("1", "true")
            if "cheat" in flags and not cheats_on:
                add(
                    lineno,
                    "warning",
                    "requires_cheats",
                    f"{cv.name} is a cheat convar/command",
                    "It only works with `sv_cheats 1` set first (not allowed on matchmaking/official servers).",
                )
            if "developmentonly" in flags:
                add(
                    lineno,
                    "warning",
                    "development_only",
                    f"{cv.name} is flagged developmentonly",
                    "Usually hidden or ignored in retail builds.",
                )
            if context == "server" and "clientdll" in flags and not ({"gamedll", "replicated"} & flags):
                add(
                    lineno,
                    "warning",
                    "wrong_context",
                    f"{cv.name} is a client-side setting",
                    "It has no effect when executed in a server config.",
                )
            if context == "client" and "gamedll" in flags and "clientdll" not in flags:
                add(
                    lineno,
                    "warning",
                    "wrong_context",
                    f"{cv.name} is server-side",
                    "Set it on the server (or in a listen-server config), not in a client config.",
                )

            if cv.kind == "command" or cv.name in _PASSTHROUGH_ARGS:
                continue
            if not args:
                add(
                    lineno,
                    "info",
                    "no_value",
                    f"{cv.name} has no value",
                    f"Without a value it prints the current value (default {cv.default_value!r}).",
                )
                continue
            value = args[0]
            lo, hi, v = _num(cv.min_value), _num(cv.max_value), _num(value)
            if v is not None and ((lo is not None and v < lo) or (hi is not None and v > hi)):
                add(
                    lineno,
                    "error",
                    "out_of_range",
                    f"{cv.name} {value} is outside [{cv.min_value}, {cv.max_value}]",
                    "The engine clamps or rejects out-of-range values.",
                )
            elif v is None and _num(cv.default_value) is not None and value.lower() not in ("true", "false"):
                add(
                    lineno,
                    "warning",
                    "type_mismatch",
                    f"{cv.name} expects a number (default {cv.default_value}), got {value!r}",
                )

    errors = sum(1 for d in diags if d.severity == "error")
    return m.CfgValidation(
        ok=errors == 0,
        errors=errors,
        warnings=sum(1 for d in diags if d.severity == "warning"),
        commands=count,
        diagnostics=diags,
    )
