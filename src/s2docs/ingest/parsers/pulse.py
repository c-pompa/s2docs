"""Parser for DumpSource2 `module_metadata/<module>/pulse_bindings.txt`.

Format::

    // fingerprint = 2545903890
    CAmbientGeneric_API
    {
        // m_FriendlyName = "Fade In"
        // hidden_in_tool
        input FadeIn(PVAL_EHANDLE:ambient_generic _Target "ambient_generic", PVAL_FLOAT param "param");
    }
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_STMT = re.compile(r"^(?P<kind>input|output|function|class|event|cursor)\s+(?P<name>[\w:]+)(?P<rest>.*);$")


@dataclass(slots=True)
class PulseBinding:
    library: str
    kind: str
    name: str
    signature: str
    module: str
    friendly_name: str | None = None
    description: str | None = None
    metadata: dict[str, str | bool] = field(default_factory=dict)


def _meta(comment: str) -> tuple[str, str | bool]:
    key, sep, value = comment.partition("=")
    if not sep:
        return comment.strip(), True
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return key.strip(), value


def parse_pulse_bindings(text: str, module: str) -> Iterator[PulseBinding]:
    library: str | None = None
    pending: dict[str, str | bool] = {}
    stmt_buf: list[str] = []
    last_ident: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if library is None:
            if line == "{" and last_ident:
                library, pending = last_ident, {}
            elif not line.startswith("//"):
                last_ident = line
            continue

        if line == "}":
            library, last_ident, pending, stmt_buf = None, None, {}, []
            continue
        if line.startswith("//") and not stmt_buf:
            k, v = _meta(line[2:].strip())
            pending[k] = v
            continue

        stmt_buf.append(line)
        if not line.endswith(";"):
            continue
        stmt = " ".join(stmt_buf)
        stmt_buf = []
        m = _STMT.match(stmt)
        if m:
            friendly = pending.pop("m_FriendlyName", None)
            desc = pending.pop("m_Description", None) or pending.pop("description", None)
            yield PulseBinding(
                library=library,
                kind=m["kind"],
                name=m["name"],
                signature=stmt.rstrip(";"),
                module=module,
                friendly_name=friendly if isinstance(friendly, str) else None,
                description=desc if isinstance(desc, str) else None,
                metadata=pending,
            )
        pending = {}
