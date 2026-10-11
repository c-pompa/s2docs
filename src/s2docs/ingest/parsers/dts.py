"""Parser for TypeScript declaration files (`point_script.d.ts`).

Not a general TypeScript parser: it handles the declaration subset Valve uses
(classes, interfaces, enums, type aliases, consts; members one per statement,
JSDoc comments before declarations). Members whose types span lines are
handled by bracket-balancing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DECL = re.compile(
    r"^(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?P<kind>class|interface|enum|type|const|let|function|namespace)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)(?P<generics><[^>]*>)?"
    r"(?:\s+extends\s+(?P<extends>[\w$.<>, ]+?))?(?:\s+implements\s+[\w$.<>, ]+?)?\s*(?P<tail>[{=:;(].*)?$"
)
_MEMBER_NAME = re.compile(
    r"^(?:(?:readonly|static|public|private|protected|abstract|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*|\[[^\]]+\]|\"[^\"]+\")(?P<opt>\?)?\s*(?P<generics><[^(]*?>)?\s*(?P<next>[(:=,;]|$)"
)
_MODULE = re.compile(r'^declare\s+module\s+"(?P<name>[^"]+)"')


@dataclass(slots=True)
class ApiSymbol:
    name: str
    kind: str  # class | interface | enum | type | const | function | method | property | enum_member
    signature: str
    parent: str | None = None
    doc: str | None = None
    extends: str | None = None
    deprecated: bool = False
    module: str | None = None
    ordinal: int = 0


@dataclass(slots=True)
class DtsFile:
    module_doc: str | None = None
    module: str | None = None
    symbols: list[ApiSymbol] = field(default_factory=list)


def _clean_doc(lines: list[str]) -> str | None:
    out = []
    for ln in lines:
        s = ln.strip()
        s = re.sub(r"^/\*\*?", "", s)
        s = re.sub(r"\*/$", "", s)
        s = re.sub(r"^\*\s?", "", s)
        out.append(s.rstrip())
    text = "\n".join(out).strip()
    return text or None


def _balance(s: str) -> int:
    depth = 0
    in_str: str | None = None
    prev = ""
    for ch in s:
        if in_str:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == ">" and prev == "=":
            pass  # arrow =>, not a closing generic
        prev = ch
    return depth


def _strip_line_comment(line: str) -> str:
    # Remove trailing // comments that are not inside strings (good enough for .d.ts).
    idx = line.find("//")
    if idx != -1 and line[:idx].count('"') % 2 == 0 and "://" not in line[max(0, idx - 6) : idx + 2]:
        return line[:idx].rstrip()
    return line.rstrip()


def parse_dts(text: str) -> DtsFile:
    result = DtsFile()
    lines = text.splitlines()
    i = 0
    doc_buf: list[str] = []
    pending_doc: str | None = None
    container: ApiSymbol | None = None
    stmt: list[str] = []
    ordinals: dict[tuple[str | None, str], int] = {}

    def add(sym: ApiSymbol) -> None:
        key = (sym.parent, sym.name)
        sym.ordinal = ordinals.get(key, 0)
        ordinals[key] = sym.ordinal + 1
        sym.module = result.module
        sym.deprecated = bool(sym.doc and "@deprecated" in sym.doc)
        result.symbols.append(sym)

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1

        # JSDoc blocks
        if not stmt and line.startswith("/**"):
            doc_buf = [line]
            while "*/" not in line and i < len(lines):
                line = lines[i].strip()
                doc_buf.append(line)
                i += 1
            pending_doc = _clean_doc(doc_buf)
            continue
        if not stmt and (line.startswith("//") or not line):
            continue

        code = _strip_line_comment(line)
        if not code:
            continue

        m = _MODULE.match(code)
        if m and container is None:
            result.module = m["name"]
            if result.module_doc is None:
                result.module_doc = pending_doc
            pending_doc = None
            continue

        if container is None:
            if code in ("{", "}"):
                continue
            stmt.append(code)
            joined = " ".join(stmt)
            # Wait for a complete top-level statement.
            opens_body = joined.endswith("{") and _balance(joined) == 1
            if (
                not opens_body
                and (_balance(joined) > 0 or not re.search(r"[;}]$", joined))
                and not re.search(r"\{\s*\}$", joined)
            ):
                continue
            stmt = []
            dm = _DECL.match(joined)
            if not dm:
                pending_doc = None
                continue
            kind = "const" if dm["kind"] == "let" else dm["kind"]
            sig = joined.rstrip("{").strip().rstrip(";")
            sym = ApiSymbol(
                name=dm["name"],
                kind=kind,
                signature=re.sub(r"^(export\s+)?(declare\s+)?", "", sig),
                doc=pending_doc,
                extends=(dm["extends"] or "").strip() or None,
            )
            add(sym)
            pending_doc = None
            if opens_body and kind in ("class", "interface", "enum", "namespace"):
                container = sym
            continue

        # Inside class / interface / enum body
        if not stmt and code.startswith("}"):
            container = None
            pending_doc = None
            continue
        stmt.append(code)
        joined = " ".join(stmt)
        if _balance(joined) > 0:
            continue
        # Interface members may omit ';' on the last line before '}'.
        member_done = (
            re.search(r"[;,]$", joined)
            or container.kind == "enum"
            or i >= len(lines)
            or lines[i].strip().startswith("}")
        )
        if not member_done:
            continue
        stmt = []
        member_sig = joined.rstrip(";,").strip()
        mm = _MEMBER_NAME.match(member_sig)
        if not mm:
            pending_doc = None
            continue
        if container.kind == "enum":
            mkind = "enum_member"
        elif mm["next"] == "(":
            mkind = "method"
        else:
            mkind = "property"
        add(
            ApiSymbol(
                name=mm["name"],
                kind=mkind,
                signature=member_sig,
                parent=container.name,
                doc=pending_doc,
            )
        )
        pending_doc = None

    return result
