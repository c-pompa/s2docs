#!/usr/bin/env bash
# rule: CHC-SECRETS
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: gitleaks (default rules) finds no secret in the files the tree ships, nor in any
#   commit of the range under review - a secret added and then deleted still counts.
#
# usage: CHC-SECRETS-gitleaks.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# Only shippable files are scanned (tracked + untracked-not-ignored), never .venv or caches. The
# range is the one CHC-COMMITS uses (see ci_commit_base in lib.sh). Findings are redacted.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-SECRETS "$@"
ci_need_tool gitleaks git tar
work="$(mktemp -d "${TMPDIR:-/tmp}/chc-secrets.XXXXXX")"
trap 'rm -rf "$work"' EXIT
LEAKS=3 # gitleaks' own fatal errors exit 1; give findings a code of their own

ci_export_tree "$TREE" "$work/files"
if gitleaks dir "$work/files" --no-banner --redact --log-level warn --exit-code "$LEAKS"; then rc=0; else rc=$?; fi
case "$rc" in
    0) ;;
    "$LEAKS") echo "FAIL CHC-SECRETS: gitleaks found secrets in the working tree (above, redacted)"; exit 1 ;;
    *) ci_die 2 "gitleaks dir could not scan the tree (exit $rc)" ;;
esac

base="$(ci_commit_base "$TREE")"
count="$(git -C "$TREE" rev-list --count "$base..HEAD")"
if [ "$count" -gt 0 ]; then
    if gitleaks git "$TREE" --no-banner --redact --log-level warn --exit-code "$LEAKS" \
        --log-opts="$base..HEAD"; then rc=0; else rc=$?; fi
    case "$rc" in
        0) ;;
        "$LEAKS") echo "FAIL CHC-SECRETS: gitleaks found secrets in commits $base..HEAD (above, redacted)"; exit 1 ;;
        *) ci_die 2 "gitleaks git could not scan $base..HEAD (exit $rc)" ;;
    esac
fi
echo "PASS CHC-SECRETS: no secrets in the shipped files or in the $count commit(s) of $base..HEAD"
