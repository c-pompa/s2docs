#!/usr/bin/env bash
# rule: HPF-007
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/2c
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: the uvx launch in plugin/mcp.json passes --exclude-newer to uvx itself (before the
#   command) with one fixed instant - an RFC 3339 timestamp with an offset, not a bare date, not
#   a relative span, not in the future - and the runtime dependencies and the build backend
#   resolve at that instant, so a first start can neither drift nor fail on the date.
#
# usage: HPF-007-uvx-exclude-newer-fixed.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# A bare date is read in the installing machine's own time zone, so two users would resolve against
# different cutoffs; only a timestamp with an offset names one instant everywhere.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-007 "$@"
ci_need_file "$TREE/plugin/mcp.json" "$TREE/pyproject.toml"
ci_need_tool uv

ci_py - <<'PY'
import json
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from cilib import CannotEvaluate, fixed_timestamp, read_text, run_check, tree, uvx_option

PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


def resolves(requirements_file, stamp, what):
    proc = subprocess.run(
        ["uv", "pip", "compile", str(requirements_file), "--exclude-newer", stamp, "--python-version", PY_VERSION,
         "--no-header", "--no-annotate", "-q"],
        cwd=requirements_file.parent, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return None
    if "No solution found" in proc.stderr:
        lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
        return f"{what} cannot resolve at --exclude-newer {stamp}: {' '.join(lines[1:4])[:400]}"
    raise CannotEvaluate(f"uv pip compile of {what} failed (rc={proc.returncode}): {proc.stderr.strip()[-600:]}")


def check():
    root = tree()
    try:
        servers = json.loads(read_text(root / "plugin" / "mcp.json"))["mcpServers"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CannotEvaluate(f"plugin/mcp.json has no mcpServers mapping: {exc}") from exc
    launches = {name: s for name, s in servers.items() if isinstance(s, dict) and s.get("command") == "uvx"}
    if not launches:
        raise CannotEvaluate("plugin/mcp.json launches no server with uvx; this rule's premise no longer holds")

    violations, stamps = [], []
    for name, server in sorted(launches.items()):
        args = [str(a) for a in server.get("args", [])]
        value, command = uvx_option(args, "--exclude-newer")
        if value is None:
            late = any(a == "--exclude-newer" or a.startswith("--exclude-newer=") for a in args[command:])
            violations.append(f"server {name!r}: --exclude-newer " + (
                f"comes after the command {args[command]!r}, so it is passed to that tool, not to uvx" if late
                else "is missing from the uvx args, so the first start resolves whatever is newest that day"))
            continue
        try:
            moment = fixed_timestamp(value)
        except ValueError as exc:
            violations.append(f"server {name!r}: --exclude-newer {exc}")
            continue
        if moment > datetime.now(UTC):
            violations.append(f"server {name!r}: --exclude-newer {value} is in the future and constrains nothing yet")
            continue
        stamps.append(value)

    if violations:
        return violations
    pyproject = tomllib.loads(read_text(root / "pyproject.toml"))
    runtime = pyproject.get("project", {}).get("dependencies") or []
    backend = pyproject.get("build-system", {}).get("requires") or []
    with tempfile.TemporaryDirectory(prefix="hpf-007-") as tmp:
        for stamp in sorted(set(stamps)):
            for what, reqs in (("the runtime dependencies", runtime), ("the build backend", backend)):
                if not reqs:
                    continue
                path = Path(tmp) / "requirements.in"
                path.write_text("\n".join(reqs) + "\n", encoding="utf-8")
                problem = resolves(path, stamp, what)
                if problem:
                    violations.append(problem)
    return violations


run_check(check, ok="uvx resolves the first start at one fixed, resolvable --exclude-newer instant")
PY
