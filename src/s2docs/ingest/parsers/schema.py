"""Parser for DumpSource2 schema headers (`schemas/<module>/<Name>.h`).

Each file holds one class or enum::

    // MNetworkVarNames            <- class metadata
    class CBaseEntity : public CEntityInstance
    {
        // MNotSaved                <- field metadata (applies to next field)
        int32 m_iHealth;
        CUtlVector< thinkfunc_t > m_aThinkFunctions;
    };

    enum MoveType_t : uint8_t
    {
        MOVETYPE_NONE = 0,
    };
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DECL = re.compile(r"^(?P<kind>class|struct|enum|union)\s+(?P<name>(?:\w|::|<[^>]*>)+)\s*(?::(?!:)\s*(?P<bases>.+))?$")
_FIELD = re.compile(r"^(?P<type>.+?)\s+(?P<name>[A-Za-z_]\w*)(?P<array>(?:\[[^\]]*\])*)\s*(?::\s*\d+)?\s*;$")
_ENUM_MEMBER = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<value>[^,]+?))?\s*,?$")


@dataclass(slots=True)
class SchemaField:
    name: str
    type: str | None
    value: str | None = None  # enum member value, or field default
    metadata: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SchemaClass:
    name: str
    kind: str  # class | enum
    module: str
    bases: list[str] = field(default_factory=list)
    underlying_type: str | None = None
    metadata: list[str] = field(default_factory=list)
    fields: list[SchemaField] = field(default_factory=list)


def parse_schema_header(text: str, module: str) -> SchemaClass | None:
    cls: SchemaClass | None = None
    pending: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in ("{", "};", "}"):
            continue
        if line.startswith("//"):
            pending.append(line[2:].strip())
            continue

        if cls is None:
            m = _DECL.match(line)
            if not m:
                continue
            kind = "enum" if m["kind"] == "enum" else "class"
            cls = SchemaClass(name=m["name"].strip(), kind=kind, module=module, metadata=pending)
            pending = []
            if m["bases"]:
                if kind == "enum":
                    cls.underlying_type = m["bases"].strip()
                else:
                    cls.bases = [
                        re.sub(r"^(public|private|protected)\s+", "", b.strip()) for b in _split_top_level(m["bases"])
                    ]
            continue

        # Trailing comments carry default values: `float32 m_flMax; // = 1`
        trailing = None
        if "//" in line:
            line, _, trailing = line.partition("//")
            line, trailing = line.strip(), trailing.strip()
        default = trailing[1:].strip() if trailing and trailing.startswith("=") else None
        meta = pending + ([trailing] if trailing and default is None else [])

        if cls.kind == "enum":
            m = _ENUM_MEMBER.match(line)
            if m:
                cls.fields.append(SchemaField(m["name"], None, (m["value"] or "").strip() or None, meta))
        else:
            m = _FIELD.match(line)
            if m:
                ftype = m["type"].strip() + (m["array"] or "")
                cls.fields.append(SchemaField(m["name"], ftype, default, meta))
        pending = []

    return cls


def _split_top_level(s: str) -> list[str]:
    """Split on commas not nested in template brackets."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]
