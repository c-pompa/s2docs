#!/usr/bin/env bash
# rule: CHC-SAST
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: application and CI code (src/, scripts/) makes none of the dangerous calls in the
#   ruff flake8-bandit subset below: exec/eval, pickle/marshal loads, unsafe yaml.load, a shell
#   through subprocess(shell=True)/os.system/os.popen, unverified TLS, unescaped templates.
#
# usage: CHC-SAST-dangerous-calls.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# --isolated: the subset is this rule's, independent of the style rules in [tool.ruff]. S604 is
# left out on purpose: s2docs' own `shell=` keyword names a remote shell flavour ("powershell"),
# not a local shell, and S602/S605 already cover every way to start one.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-SAST "$@"
ci_need_dir "$TREE/src" "$TREE/scripts"
ruff="$(ci_venv_tool ruff)"
RULES="S102,S301,S302,S307,S323,S501,S506,S602,S605,S701,S702"

if (cd "$TREE" && "$ruff" check --isolated --no-cache --target-version py311 --select "$RULES" \
    --output-format concise src scripts); then rc=0; else rc=$?; fi
case "$rc" in
    0) echo "PASS CHC-SAST: no dangerous calls ($RULES) in src or scripts" ;;
    1) echo "FAIL CHC-SAST: dangerous calls listed above" ;;
    *) ci_die 2 "ruff could not run (exit $rc)" ;;
esac
exit "$rc"
