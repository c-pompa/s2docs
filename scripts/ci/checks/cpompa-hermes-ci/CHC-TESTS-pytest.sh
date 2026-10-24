#!/usr/bin/env bash
# rule: CHC-TESTS
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: the whole pytest suite passes against the tree's own source with nothing skipped -
#   a skipped test (no node, no pinned tsc) is a gap in what was verified, so it fails the rule.
#
# usage: CHC-TESTS-pytest.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The project environment (uv sync --all-groups) runs the suite; the tree's src/ comes first on the
# path, so a fixture copy or a pinned commit is tested as itself.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-TESTS "$@"
ci_need_dir "$TREE/src" "$TREE/tests"

ci_py - <<'PY'
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from cilib import CannotEvaluate, run_check, tree


def check():
    root = tree()
    with tempfile.TemporaryDirectory(prefix="chc-tests-") as tmp:
        report = Path(tmp) / "junit.xml"
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEs", f"--junitxml={report}"],
            cwd=root, env=env, capture_output=True, text=True,
        )
        print(proc.stdout[-6000:], end="")
        # 0 passed, 1 failures, 2 interrupted by collection errors, 5 nothing collected: all verdicts.
        # 3 (internal error) and 4 (usage error) mean pytest itself did not run the suite.
        if proc.returncode not in (0, 1, 2, 5) or not report.is_file():
            raise CannotEvaluate(f"pytest did not complete a run (exit {proc.returncode}): {proc.stderr[-1500:]}")
        suite = ET.parse(report).getroot()
        suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
        counts = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    problems = []
    if counts["tests"] == 0:
        problems.append("the suite collected no tests")
    if counts["failures"] or counts["errors"]:
        problems.append(f"{counts['failures']} failed, {counts['errors']} errors (summary above)")
    if counts["skipped"]:
        problems.append(f"{counts['skipped']} tests skipped (reasons above): install what they need, e.g. node and "
                        "`npm ci --prefix tools/tsc --ignore-scripts`")
    if proc.returncode != 0 and not problems:
        problems.append(f"pytest exited {proc.returncode} without a recorded failure")
    return problems


run_check(check, ok="the full suite passed with nothing skipped")
PY
