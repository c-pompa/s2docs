"""Parser for the FGD files emitted by DumpSource2 (`entities/server.fgd`, `client.fgd`).

These are machine-generated and more regular than hand-written Hammer FGDs::

    // CBaseEntity                         <- C++ class (optional)
    @PointClass base(CBaseEntity) = func_button
    [
        speed(float) // FIELD_FLOAT32 m_flSpeed
        platform_type(intchoices) : "platform_type" : : "" = // FIELD_UINT8 ...
        [
            0 : "ENTITY_NOT_PLATFORM"
        ]
        input Lock(void) : "Optional description" // PVAL_... notes, Pulse node
        output OnPressed(void)
        // component CBodyComponent
            // m_pSceneNode (CGameSceneNode)
                origin(vector) // FIELD_POSITION_VECTOR m_vecAbsOrigin
    ]
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_HEADER = re.compile(
    r"^@(?P<type>\w+)(?:\s+base\((?P<bases>[^)]*)\))?.*?=\s*(?P<name>[\w.\-]+)\s*(?::\s*\"(?P<desc>[^\"]*)\")?"
)
_IO = re.compile(r"^(?P<kind>input|output)\s+(?P<name>[\w.\-]+)\((?P<type>[^)]*)\)(?P<rest>.*)$")
_KV = re.compile(r"^(?P<name>[A-Za-z_][\w.\-]*)\((?P<type>\w+)\)(?P<rest>.*)$")
_CHOICE = re.compile(r'^(?P<value>-?[\w.]+)\s*:\s*"(?P<label>[^"]*)"')
_COMPONENT = re.compile(r"^//\s*component\s+(?P<name>\w+)")


@dataclass(slots=True)
class EntityMember:
    kind: str  # keyvalue | input | output
    name: str
    type: str
    description: str | None = None
    notes: str | None = None  # trailing // comment (C++ field, PVAL types, ...)
    component: str | None = None
    choices: list[tuple[str, str]] | None = None


@dataclass(slots=True)
class FgdEntity:
    classname: str
    class_type: str  # BaseClass | PointClass | SolidClass ...
    side: str
    bases: list[str] = field(default_factory=list)
    cpp_class: str | None = None
    description: str | None = None
    members: list[EntityMember] = field(default_factory=list)


def _split_rest(rest: str) -> tuple[str | None, str | None]:
    """Split `` : "desc" ... // notes`` into (description, notes)."""
    notes = None
    if "//" in rest:
        rest, _, notes = rest.partition("//")
        notes = notes.strip() or None
    quoted = re.findall(r'"([^"]*)"', rest)
    desc = next((q for q in reversed(quoted) if q.strip()), None)
    return desc, notes


def parse_fgd(text: str, side: str) -> Iterator[FgdEntity]:
    pending_comment: str | None = None
    entity: FgdEntity | None = None
    depth = 0  # bracket depth inside an entity
    component: str | None = None
    choices_for: EntityMember | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if entity is None:
            if line.startswith("@"):
                m = _HEADER.match(line)
                if not m:
                    pending_comment = None
                    continue
                bases = [b.strip() for b in (m["bases"] or "").split(",") if b.strip()]
                entity = FgdEntity(
                    classname=m["name"],
                    class_type=m["type"],
                    side=side,
                    bases=bases,
                    cpp_class=pending_comment,
                    description=m["desc"] or None,
                )
                depth, component, choices_for = 0, None, None
                pending_comment = None
            elif line.startswith("//"):
                comment = line[2:].strip()
                # Only simple identifiers are C++ class names; ignore banner comments.
                pending_comment = comment if re.fullmatch(r"[\w:]+", comment) else None
            continue

        if line == "[":
            depth += 1
            continue
        if line == "]":
            depth -= 1
            if depth == 1:
                choices_for = None
            if depth <= 0:
                yield entity
                entity = None
            continue

        if depth >= 2 and choices_for is not None:
            cm = _CHOICE.match(line)
            if cm:
                choices_for.choices = (choices_for.choices or []) + [(cm["value"], cm["label"])]
            continue

        if line.startswith("//"):
            cm = _COMPONENT.match(line)
            if cm and raw.startswith("\t") and not raw.startswith("\t\t"):
                component = cm["name"]
            continue

        indent = len(raw) - len(raw.lstrip("\t"))
        member_component = component if indent > 1 else None

        m = _IO.match(line)
        if m:
            desc, notes = _split_rest(m["rest"])
            entity.members.append(EntityMember(m["kind"], m["name"], m["type"] or "void", desc, notes))
            continue

        m = _KV.match(line)
        if m:
            desc, notes = _split_rest(m["rest"])
            if desc == m["name"]:
                desc = None  # generated label equal to the key adds nothing
            member = EntityMember("keyvalue", m["name"], m["type"], desc, notes, member_component)
            entity.members.append(member)
            choices_for = member if m["type"] in ("choices", "intchoices", "flags") else None

    if entity is not None:
        yield entity
