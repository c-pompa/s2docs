#!/usr/bin/env bash
# rule: CHC-COMMITS
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: every commit in the range under review has a non-empty subject of at most 120
#   characters with no trailing whitespace, a blank line before any body, and is not a
#   fixup!/squash!/amend! or WIP commit; the range adds no whitespace errors or conflict markers
#   (git diff --check) and no file larger than 5 MiB.
#
# usage: CHC-COMMITS-hygiene.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The range is base..HEAD with the base from ci_commit_base (lib.sh): CHC_COMMITS_BASE, the merge
# request's diff base, the branch's upstream, or the tip before this push - never a guess.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-COMMITS "$@"
ci_need_tool git
MAX_SUBJECT=120
MAX_BYTES=$((5 * 1024 * 1024))
WIP_RE='^(wip|tmp|temp)([^a-z]|$)|(^|[^a-z])wip([^a-z]|$)'
work="$(mktemp -d "${TMPDIR:-/tmp}/chc-commits.XXXXXX")"
trap 'rm -rf "$work"' EXIT

base="$(ci_commit_base "$TREE")"
git -C "$TREE" rev-list --reverse "$base..HEAD" >"$work/commits" || ci_die 2 "cannot list the commits in $base..HEAD"
failed=0
fail() {
    echo "FAIL CHC-COMMITS: $*"
    failed=1
}

shopt -s nocasematch
count=0
while IFS= read -r commit <&3; do
    count=$((count + 1))
    short="$(git -C "$TREE" rev-parse --short "$commit")"
    git -C "$TREE" log -1 --format=%B "$commit" >"$work/message"
    subject="$(sed -n '1p' "$work/message")"
    second="$(sed -n '2p' "$work/message")"
    case "$subject" in
        "") fail "$short has an empty subject" ;;
        "fixup! "* | "squash! "* | "amend! "*) fail "$short '$subject' is an autosquash commit; squash it before merging" ;;
    esac
    if [[ "$subject" =~ $WIP_RE ]]; then
        fail "$short '$subject' is a work-in-progress commit"
    fi
    if [ "${#subject}" -gt "$MAX_SUBJECT" ]; then
        fail "$short subject is ${#subject} characters (max $MAX_SUBJECT): '$subject'"
    fi
    case "$subject" in
        *" " | *$'\t') fail "$short subject ends in whitespace" ;;
    esac
    if [ -n "$second" ]; then
        fail "$short has no blank line between the subject and the body"
    fi
done 3<"$work/commits"
shopt -u nocasematch

if [ "$count" -gt 0 ]; then
    if ! git -C "$TREE" diff --check "$base" HEAD; then
        fail "the range adds whitespace errors or conflict markers (above)"
    fi
    git -C "$TREE" diff -z --name-only --diff-filter=AM "$base" HEAD >"$work/paths" ||
        ci_die 2 "cannot list the files the range adds"
    while IFS= read -r -d '' path <&3; do
        size="$(git -C "$TREE" cat-file -s "HEAD:$path")"
        if [ "$size" -gt "$MAX_BYTES" ]; then
            fail "the range adds $path at $size bytes (max $MAX_BYTES)"
        fi
    done 3<"$work/paths"
fi

[ "$failed" -eq 0 ] || exit 1
echo "PASS CHC-COMMITS: $count commit(s) in ${base:0:12}..HEAD are clean"
