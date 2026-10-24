#!/usr/bin/env bash
# rule: HPF-008
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/3
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: the uvx git ref in plugin/mcp.json pins a full 40-hex commit of this repository's
#   history (an ancestor of HEAD), and the server code at that commit passes every reviewer rule
#   marked pinned_server in ci/feedback-rules.yaml - users run the pinned server, not HEAD.
#
# usage: HPF-008-uvx-pin-carries-fixes.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The pinned commit is exported with `git archive` and each pinned_server rule runs against it
# through run.sh, so a new server-side rule is covered here by its registry row alone.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-008 "$@"
ci_need_file "$TREE/plugin/mcp.json"
ci_need_tool git tar

# 1. the ref: one uvx launch, --from git+<url>@<40-hex>
ref_file="$(mktemp "${TMPDIR:-/tmp}/hpf-008-ref.XXXXXX")"
work="$(mktemp -d "${TMPDIR:-/tmp}/hpf-008.XXXXXX")"
trap 'rm -rf "$work" "$ref_file"' EXIT
if ci_py - >"$ref_file" <<'PY'; then rc=0; else rc=$?; fi
import json
import re

from cilib import CannotEvaluate, read_text, run_check, tree, uvx_option


def check():
    servers = json.loads(read_text(tree() / "plugin" / "mcp.json")).get("mcpServers") or {}
    launches = {n: s for n, s in servers.items() if isinstance(s, dict) and s.get("command") == "uvx"}
    if len(launches) != 1:
        raise CannotEvaluate(f"expected exactly one uvx launch in plugin/mcp.json, found {sorted(launches)}")
    (name, server), = launches.items()
    spec, _ = uvx_option([str(a) for a in server.get("args", [])], "--from")
    if spec is None:
        return [f"server {name!r} has no --from: uvx would install whatever 's2docs' PyPI serves"]
    match = re.fullmatch(r"git\+(?P<url>\S+?)@(?P<ref>[^@/]+)", spec)
    if match is None:
        return [f"server {name!r}: --from {spec!r} is not git+<url>@<ref>"]
    if not re.fullmatch(r"[0-9a-f]{40}", match["ref"]):
        return [f"server {name!r}: the uvx ref {match['ref']!r} is not a full 40-hex commit sha; a branch, tag or "
                "short sha can move to code nobody reviewed"]
    print(match["ref"])
    return []


run_check(check, ok="the uvx ref is a full commit sha")
PY
sha="$(head -n 1 "$ref_file")"
if [ "$rc" -ne 0 ]; then
    cat "$ref_file"
    exit "$rc"
fi
ci_log "HPF-008: plugin/mcp.json pins the server at $sha"

# 2. the commit: present, in this branch's history
git -C "$TREE" rev-parse --git-dir >/dev/null 2>&1 || ci_die 2 "unreadable input: $TREE is not a git work tree"
[ "$(git -C "$TREE" rev-parse --is-shallow-repository)" = "false" ] ||
    ci_die 2 "$TREE is a shallow clone; the pinned commit may be cut off (CI needs GIT_DEPTH: 0)"
if ! git -C "$TREE" cat-file -e "${sha}^{commit}" 2>/dev/null; then
    echo "FAIL HPF-008: the pinned commit $sha is not in this repository's history"
    exit 1
fi
if ! git -C "$TREE" merge-base --is-ancestor "$sha" HEAD; then
    echo "FAIL HPF-008: the pinned commit $sha is not an ancestor of HEAD; the plugin would launch code this branch does not contain"
    exit 1
fi

# 3. the code at that commit must pass every pinned_server rule
mkdir -p "$work/pinned"
git -C "$TREE" archive "$sha" | tar -x -C "$work/pinned"
py="$(ci_python)"
"$py" "$CI_LIB_DIR/registry.py" pinned-server >"$work/rules" || ci_die 2 "cannot read the pinned_server rules from the registry"
[ -s "$work/rules" ] || ci_die 2 "no rule in ci/feedback-rules.yaml is marked pinned_server; nothing to verify the pin against"
failed=""
errored=""
while read -r rule <&3; do
    [ "$rule" != "HPF-008" ] || ci_die 2 "HPF-008 is marked pinned_server; it cannot verify itself"
    printf -- '-- %s at %s\n' "$rule" "${sha:0:12}"
    if bash "$CI_LIB_DIR/run.sh" check "$rule" "$work/pinned" </dev/null 2>&1; then rule_rc=0; else rule_rc=$?; fi
    case "$rule_rc" in
        0) ;;
        1) failed="$failed $rule" ;;
        *) errored="$errored $rule" ;;
    esac
done 3<"$work/rules"

if [ -n "$failed" ]; then
    echo "FAIL HPF-008: the pinned server ${sha:0:12} fails${failed}; re-pin plugin/mcp.json at a commit that carries the fixes"
    exit 1
fi
if [ -n "$errored" ]; then
    echo "ERROR HPF-008: could not evaluate${errored} at the pinned server ${sha:0:12} (see above): either that commit predates what the rule reads, or the environment lacks a tool"
    exit 2
fi
echo "PASS HPF-008: the pinned server ${sha:0:12} is in HEAD's history and passes every pinned_server rule"
