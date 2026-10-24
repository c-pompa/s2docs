#!/usr/bin/env bash
# rule: HPF-009
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/2d
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: the project resolves under the same release quarantine its users get - pyproject.toml
#   sets [tool.uv] exclude-newer to a fixed RFC 3339 instant, and it is the instant the plugin's
#   uvx launch passes as --exclude-newer, so the lock CI tests is the set a first start installs.
#
# usage: HPF-009-ci-release-quarantine.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# Catalog rule 10, which the review cites, recommends a release quarantine in the author's own CI
# ("uv --exclude-newer"); its developer guide adds "so the lock you test is the one users get".
# Whether uv.lock is current for pyproject.toml is a house rule (cpompa-hermes-ci), not this one.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-009 "$@"
ci_need_file "$TREE/pyproject.toml" "$TREE/plugin/mcp.json"

ci_py - <<'PY'
import json
import tomllib
from datetime import UTC, datetime

from cilib import CannotEvaluate, fixed_timestamp, read_text, run_check, tree, uvx_option


def check():
    root = tree()
    project_raw = ((tomllib.loads(read_text(root / "pyproject.toml")).get("tool") or {}).get("uv") or {}).get(
        "exclude-newer")
    if project_raw is None:
        return ["pyproject.toml has no [tool.uv] exclude-newer: CI resolves against whatever is newest today, "
                "not the snapshot users install"]
    try:
        project = fixed_timestamp(str(project_raw))
    except ValueError as exc:
        return [f"[tool.uv] exclude-newer {exc}"]
    if project > datetime.now(UTC):
        return [f"[tool.uv] exclude-newer {project_raw} is in the future, so it quarantines nothing"]

    try:
        servers = json.loads(read_text(root / "plugin" / "mcp.json"))["mcpServers"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CannotEvaluate(f"plugin/mcp.json has no mcpServers mapping: {exc}") from exc
    launches = {n: s for n, s in servers.items() if isinstance(s, dict) and s.get("command") == "uvx"}
    if not launches:
        raise CannotEvaluate("plugin/mcp.json launches no server with uvx; there is no first-start snapshot to match")
    violations = []
    for name, server in sorted(launches.items()):
        value, _ = uvx_option([str(a) for a in server.get("args", [])], "--exclude-newer")
        try:
            plugin = fixed_timestamp(value) if value is not None else None
        except ValueError:
            plugin = None
        if plugin != project:
            violations.append(f"server {name!r}: users resolve at --exclude-newer {value!r} but CI resolves at "
                              f"[tool.uv] exclude-newer {project_raw!r}; they must be the same instant")
    return violations


run_check(check, ok="[tool.uv] exclude-newer is a fixed instant and equals the plugin's first-start snapshot")
PY
