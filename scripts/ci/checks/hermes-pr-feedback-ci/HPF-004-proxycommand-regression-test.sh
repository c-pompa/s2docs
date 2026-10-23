#!/usr/bin/env bash
# rule: HPF-004
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/1d
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: the suite keeps the regression test the reviewer asked for -
#   test_register_host_rejects_unsafe_ssh_target, parametrised with a '-oProxyCommand=' target -
#   and every one of its cases passes (none skipped).
#
# usage: HPF-004-proxycommand-regression-test.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The test is found by collecting <tree>/tests, so it may move between files; it may not be renamed
# or lose the reviewer's case without this rule failing.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-004 "$@"
ci_need_dir "$TREE/tests"

ci_py - <<'PY'
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from cilib import CannotEvaluate, run_check, tree

TEST = "test_register_host_rejects_unsafe_ssh_target"
CASE = "-oProxyCommand="


def pytest(args, root):
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
                          cwd=root, env=env, capture_output=True, text=True)


def check():
    root = tree()
    collected = pytest(["--collect-only", "-q", "-k", TEST, "tests"], root)
    if collected.returncode == 5:
        return [f"the regression test {TEST} is missing from {root / 'tests'} (nothing collected)"]
    if collected.returncode != 0:
        raise CannotEvaluate(f"pytest could not collect {root / 'tests'} (rc={collected.returncode}):\n"
                             + (collected.stdout + collected.stderr)[-1500:])
    nodes = [ln.strip() for ln in collected.stdout.splitlines() if re.search(rf"::{TEST}\[", ln)]
    if not nodes:
        return [f"{TEST} is not collected as a parametrised test"]
    if not any(f"[{CASE}" in node for node in nodes):
        return [f"{TEST} has no '{CASE}...' case (the reviewer's example); cases: {len(nodes)}"]

    with tempfile.TemporaryDirectory(prefix="hpf-004-") as tmp:
        report = Path(tmp) / "junit.xml"
        ran = pytest(["-q", "-rs", f"--junitxml={report}", "-k", TEST, "tests"], root)
        if ran.returncode not in (0, 1) or not report.is_file():
            output = (ran.stdout + ran.stderr)[-1500:]
            raise CannotEvaluate(f"pytest did not run {TEST} (rc={ran.returncode}):\n{output}")
        cases = ET.parse(report).getroot().iter("testcase")
        outcomes = {}
        for case in cases:
            state = "passed"
            for tag in ("failure", "error", "skipped"):
                if case.find(tag) is not None:
                    state = tag
            outcomes[case.get("name")] = state
    problems = [f"{name}: {state}" for name, state in sorted(outcomes.items()) if state != "passed"]
    if not any(CASE in name for name, state in outcomes.items() if state == "passed"):
        problems.append(f"the '{CASE}' case did not pass")
    if not outcomes:
        problems.append(f"{TEST} ran no cases")
    return problems


run_check(check, ok=f"{TEST} exists, covers '{CASE}' and passes")
PY
