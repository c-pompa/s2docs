"""Type-check cs_script JavaScript against Valve's point_script.d.ts with the real TypeScript compiler.

Diagnostics are classified so agents can act on them:
- unknown_api           (error)   member doesn't exist on the type or any subtype: invented API
- needs_type_narrowing  (warning) member exists on a subclass (e.g. FindEntityByName() returns Entity,
                                  the method is on CustomHudLayout): add a JSDoc cast or instanceof check
- deprecated_api        (warning) member marked @deprecated in point_script.d.ts
- implicit_any          (info)    strict-mode noise; Valve's own samples have these
- syntax / type_error   (error)   everything else tsc reports
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from s2docs import models as m
from s2docs.db.repository import Repository

_DIAG = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) TS(?P<code>\d+): (?P<msg>.*)$"
)
_TS2339 = re.compile(r"Property '(?P<prop>[^']+)' does not exist on type '(?P<type>[^']+)'")
_INSTANCE_CALL = re.compile(r"\bInstance\.(\w+)")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}\.(js|mjs)$")


class ToolchainMissing(RuntimeError):
    pass


@lru_cache
def find_tsc() -> list[str]:
    """Locate tsc: $S2DOCS_TSC, the pinned copy in tools/tsc, or one on PATH."""
    env = os.environ.get("S2DOCS_TSC")
    candidates = [env] if env else []
    candidates += [str(_PROJECT_ROOT / "tools/tsc/node_modules/.bin/tsc"), shutil.which("tsc") or ""]
    for c in candidates:
        if c and Path(c).exists():
            return [c]
    raise ToolchainMissing("TypeScript compiler not found: run `npm ci --prefix tools/tsc` or set S2DOCS_TSC")


@lru_cache
def tsc_version() -> str:
    out = subprocess.run([*find_tsc(), "--version"], capture_output=True, text=True, timeout=30)
    return out.stdout.strip()


def _subtypes_with_member(repo: Repository, game: str, type_name: str, member: str) -> list[str]:
    """API types that inherit from `type_name` (directly or not) and declare/inherit `member`."""
    types = {t.name: t.extends for t in repo.list_script_types(game)}

    def ancestors(name: str) -> list[str]:
        out, cur, seen = [], types.get(name), {name}
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = types.get(cur)
        return out

    owners = {s.parent for s in repo.find_script_symbols(game, member, limit=200) if s.name == member and s.parent}
    hits = []
    for name in types:
        if name == type_name or type_name not in ancestors(name):
            continue
        if owners & {name, *ancestors(name)}:
            hits.append(name)
    return sorted(hits)


def validate_cs_script(
    repo: Repository, code: str, *, game: str = "cs2", filename: str = "script.js"
) -> m.ScriptValidation:
    if not SCRIPT_NAME_RE.match(filename):
        raise ValueError("filename must look like name.js")
    dts = repo.raw_file(game, "cs_script/point_script.d.ts")
    tsconfig = repo.raw_file(game, "cs_script/tsconfig.json")
    # A name is deprecated only if every declaration of it is (e.g. DebugScreenText keeps a
    # current overload next to a deprecated one).
    by_name: dict[str, list[m.ScriptSymbol]] = {}
    for t in repo.list_script_types(game):
        if t.kind in ("class", "interface"):
            for mem in repo.get_script_type(game, t.name, inherited=False).members:
                by_name.setdefault(mem.name, []).append(mem)
    deprecated: dict[str, m.ScriptSymbol] = {
        name: syms[0] for name, syms in by_name.items() if all(s.deprecated for s in syms)
    }

    with tempfile.TemporaryDirectory(prefix="s2docs-tsc-") as tmp:
        d = Path(tmp)
        (d / "point_script.d.ts").write_text(dts, encoding="utf-8")
        (d / "tsconfig.json").write_text(tsconfig, encoding="utf-8")
        (d / filename).write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [*find_tsc(), "-p", "tsconfig.json", "--pretty", "false", "--allowJs", "--checkJs", "--noEmit"],
            cwd=d,
            capture_output=True,
            text=True,
            timeout=120,
        )
    raw = (proc.stdout + proc.stderr).splitlines()

    diags: list[m.Diagnostic] = []
    for line in raw:
        dm = _DIAG.match(line.strip())
        if not dm or Path(dm["file"]).name != filename:
            continue
        code_n, msg = int(dm["code"]), dm["msg"]
        category, severity, hint = "type_error", "error", None
        if 1000 <= code_n < 2000:
            category = "syntax"
        elif code_n in (7005, 7006, 7008, 7019, 7031, 7034, 7053):
            category, severity = "implicit_any", "info"
        elif code_n in (2339, 2551):
            pm = _TS2339.search(msg)
            if pm:
                subs = _subtypes_with_member(repo, game, pm["type"], pm["prop"])
                if subs:
                    category, severity = "needs_type_narrowing", "warning"
                    hint = (
                        f"'{pm['prop']}' exists on {', '.join(subs)}. Narrow the value first, e.g. "
                        f"`/** @type {{{subs[0]}}} */ (value)` or `if (value instanceof {subs[0]})`."
                    )
                else:
                    category = "unknown_api"
                    hint = f"'{pm['prop']}' is not part of the cs_script API for {pm['type']}. Check get_script_api."
        elif code_n == 2307:
            category, hint = "unknown_api", 'Import from "cs_script/point_script" (the only module provided).'
        elif code_n in (18047, 18048, 2532):
            category = "possibly_undefined"
            hint = "Find*/Get* lookups can return undefined at runtime; check the value before using it."
        diags.append(
            m.Diagnostic(
                line=int(dm["line"]),
                column=int(dm["col"]),
                code=f"TS{code_n}",
                severity=severity,
                category=category,
                message=msg,
                hint=hint,
            )
        )

    # Deprecated API usage (tsc's CLI doesn't report @deprecated).
    for i, text in enumerate(code.splitlines(), start=1):
        for mm in re.finditer(r"\.(\w+)\s*\(", text):
            sym = deprecated.get(mm.group(1))
            if sym:
                diags.append(
                    m.Diagnostic(
                        line=i,
                        column=mm.start(1) + 1,
                        code="S2DEPRECATED",
                        severity="warning",
                        category="deprecated_api",
                        message=f"{sym.qualified_name} is deprecated",
                        hint=(sym.doc or "").replace("@deprecated", "").strip() or None,
                    )
                )

    diags.sort(key=lambda x: (x.line, x.column))
    errors = sum(1 for x in diags if x.severity == "error")
    return m.ScriptValidation(
        ok=errors == 0,
        errors=errors,
        warnings=sum(1 for x in diags if x.severity == "warning"),
        diagnostics=diags,
        instance_methods_used=sorted(set(_INSTANCE_CALL.findall(code))),
        checker=f"tsc {tsc_version()} against point_script.d.ts ({game})",
    )
