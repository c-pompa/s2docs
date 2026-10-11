"""Parser for DumpSource2 `convars.txt` / `commands.txt`.

Format (one record per block, blank-line separated)::

    mp_roundtime 5 (min: 0.1, max: 60, gamedll notify replicated release)
    \tDescription line 1
    \tDescription line 2

    +lookatweapon (clientdll release)
    \t<no description>

The header is ``name [default] (attrs)``. ``attrs`` is a comma separated list
where ``min:``/``max:`` items are bounds and the last item is the space
separated flag list.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

NO_DESCRIPTION = "<no description>"


@dataclass(slots=True)
class ConVar:
    name: str
    kind: str  # "convar" | "command"
    default_value: str | None = None
    min_value: str | None = None
    max_value: str | None = None
    flags: list[str] = field(default_factory=list)
    description: str | None = None


def _parse_header(line: str, kind: str) -> ConVar:
    line = line.rstrip()
    attrs = ""
    head = line
    if line.endswith(")"):
        idx = line.rfind(" (")
        if idx == -1 and line.startswith("("):
            idx = 0
        if idx != -1:
            head, attrs = line[:idx], line[idx + 2 : -1]

    name, _, default = head.partition(" ")
    var = ConVar(name=name, kind=kind, default_value=_unquote(default) if default else None)

    for part in (p.strip() for p in attrs.split(",")):
        if not part:
            continue
        if part.startswith("min:"):
            var.min_value = part[4:].strip()
        elif part.startswith("max:"):
            var.max_value = part[4:].strip()
        else:
            var.flags.extend(part.split())
    return var


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_convars(text: str, kind: str = "convar") -> Iterator[ConVar]:
    if kind not in ("convar", "command"):
        raise ValueError(f"invalid kind {kind!r}")
    current: ConVar | None = None
    desc: list[str] = []

    def finish() -> ConVar | None:
        if current is None:
            return None
        body = "\n".join(d for d in desc if d != NO_DESCRIPTION).strip()
        current.description = body or None
        return current

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(("\t", " ")):
            if current is not None:
                desc.append(raw.strip())
            continue
        done = finish()
        if done:
            yield done
        current, desc = _parse_header(raw, kind), []

    done = finish()
    if done:
        yield done
