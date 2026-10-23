#!/usr/bin/env bash
# rule: CHC-LINT
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: `ruff check` with the project's own configuration ([tool.ruff] in pyproject.toml)
#   reports nothing for src/, tests/ and scripts/ - including the Python the CI scripts embed as
#   <<'PY' heredocs, which ruff would otherwise never see.
#
# usage: CHC-LINT-ruff.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-LINT "$@"
ci_need_file "$TREE/pyproject.toml"
ci_need_dir "$TREE/src" "$TREE/tests" "$TREE/scripts"
ruff="$(ci_venv_tool ruff)"
export CI_RUFF="$ruff"

failed=0
if (cd "$TREE" && "$ruff" check --no-cache --output-format concise src tests scripts); then rc=0; else rc=$?; fi
case "$rc" in
    0) ;;
    1) echo "FAIL CHC-LINT: ruff check reported the findings above"; failed=1 ;;
    *) ci_die 2 "ruff could not run (exit $rc)" ;;
esac

if ci_py - <<'PY'; then rc=0; else rc=$?; fi
import os
import re
import subprocess

from cilib import CannotEvaluate, read_text, run_check, tree, tree_files

HEREDOC = re.compile(r"<<'PY'\n(?P<body>.*?)\nPY\n", re.S)


def check():
    root = tree()
    problems, blocks = [], 0
    for rel in tree_files(root):
        if not (rel.startswith("scripts/") and rel.endswith(".sh")):
            continue
        text = read_text(root / rel)
        for match in HEREDOC.finditer(text):
            blocks += 1
            line = text.count("\n", 0, match.start("body")) + 1
            proc = subprocess.run(
                [os.environ["CI_RUFF"], "check", "--no-cache", "--output-format", "concise",
                 "--stdin-filename", str(root / f"{rel}.heredoc-L{line}.py"), "-"],
                input=match["body"], cwd=root, capture_output=True, text=True,
            )
            if proc.returncode == 1:
                problems += [f"{rel} (heredoc at line {line}): {ln.split('.py:', 1)[-1].strip()}"
                             for ln in proc.stdout.splitlines() if ".heredoc-L" in ln]
            elif proc.returncode != 0:
                raise CannotEvaluate(f"ruff could not lint the heredoc in {rel} at line {line}: {proc.stderr[-400:]}")
    print(f"  CHC-LINT: linted {blocks} embedded Python heredocs")
    return problems


run_check(check, ok="ruff check is clean for src, tests, scripts and the CI scripts' embedded Python")
PY
case "$rc" in
    0) ;;
    1) failed=1 ;;
    *) exit "$rc" ;;
esac
exit "$failed"
