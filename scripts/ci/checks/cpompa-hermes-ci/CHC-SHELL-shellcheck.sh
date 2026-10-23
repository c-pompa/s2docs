#!/usr/bin/env bash
# rule: CHC-SHELL
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: every shell script the repository ships (*.sh) parses with `bash -n` and passes
#   ShellCheck 0.11.0 at its default severity, following sourced files.
#
# usage: CHC-SHELL-shellcheck.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# ShellCheck comes from the pinned shellcheck-py wheel through uvx, so local runs and CI use the
# same binary; a machine without uv (or without network on first use) gets exit 2, not a skip.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-SHELL "$@"
ci_need_tool uvx bash
SHELLCHECK_PKG="shellcheck-py==0.11.0.1"

list="$(mktemp "${TMPDIR:-/tmp}/chc-shell.XXXXXX")"
trap 'rm -f "$list" "$list.sh"' EXIT
ci_tree_files "$TREE" >"$list"
tr '\0' '\n' <"$list" | awk '/\.sh$/' >"$list.sh"
[ -s "$list.sh" ] || ci_die 2 "no *.sh files found in $TREE; nothing to check"

failed=0
while IFS= read -r rel <&3; do
    if ! bash -n "$TREE/$rel"; then
        echo "FAIL CHC-SHELL: $rel does not parse (bash -n)"
        failed=1
    fi
done 3<"$list.sh"

version="$(uvx --from "$SHELLCHECK_PKG" shellcheck --version | sed -n 's/^version: //p')" ||
    ci_die 2 "cannot run ShellCheck from $SHELLCHECK_PKG via uvx"
[ "$version" = "0.11.0" ] || ci_die 2 "expected ShellCheck 0.11.0 from $SHELLCHECK_PKG, got '${version:-nothing}'"
files=()
while IFS= read -r rel <&3; do
    files+=("$rel")
done 3<"$list.sh"
if (cd "$TREE" && uvx --from "$SHELLCHECK_PKG" shellcheck -x "${files[@]}"); then rc=0; else rc=$?; fi
case "$rc" in
    0) ;;
    1) echo "FAIL CHC-SHELL: ShellCheck reported the findings above"; failed=1 ;;
    *) ci_die 2 "ShellCheck could not run (exit $rc)" ;;
esac
[ "$failed" -eq 0 ] || exit 1
echo "PASS CHC-SHELL: ${#files[@]} shell scripts parse and pass ShellCheck $version"
