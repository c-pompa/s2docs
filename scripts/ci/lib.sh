#!/usr/bin/env bash
# Shared helpers for scripts/ci/*. Sourced, never executed.
#
# Exit-code contract for every check script (scripts/ci/checks/<pipeline>/<RULE>-*.sh <tree>):
#   0  the tree satisfies the rule
#   1  the tree violates the rule (the check found something and names it)
#   2  the rule could not be evaluated: bad usage, an unreadable input, a missing tool, a crash
# The falsification harness requires a planted violation to exit exactly 1 — a check that errors
# (2) on a planted violation has not proven it can detect it.
#
# Portable to macOS /bin/bash 3.2: no associative arrays, no mapfile, no ${var,,}.

CI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_REPO_ROOT="$(cd "$CI_LIB_DIR/../.." && pwd)"
export CI_LIB_DIR CI_REPO_ROOT

ci_log() { printf '%s\n' "$*" >&2; }

# ci_die CODE MESSAGE...: report as the current rule (or the runner) and exit with CODE.
ci_die() {
    local code="$1"
    shift
    ci_log "${CI_RULE:-ci}: $*"
    exit "$code"
}

ci_need_tool() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || ci_die 2 "missing tool: $tool (not on PATH)"
    done
}

ci_need_file() {
    local path
    for path in "$@"; do
        [ -f "$path" ] && [ -r "$path" ] || ci_die 2 "unreadable input: $path"
    done
}

ci_need_dir() {
    local path
    for path in "$@"; do
        [ -d "$path" ] && [ -r "$path" ] || ci_die 2 "unreadable input: directory $path"
    done
}

# The interpreter of THIS checkout's environment (uv sync), which runs the checks. Trees under test
# (fixture copies, a pinned commit) have no environment of their own; their code is imported from
# <tree>/src through PYTHONPATH.
ci_python() {
    local py="${CI_PYTHON:-$CI_REPO_ROOT/.venv/bin/python}"
    [ -x "$py" ] || ci_die 2 "missing tool: python at $py (run 'uv sync --all-groups', or set CI_PYTHON)"
    printf '%s\n' "$py"
}

ci_venv_tool() {
    local tool="$CI_REPO_ROOT/.venv/bin/$1"
    [ -x "$tool" ] || ci_die 2 "missing tool: $1 at $tool (run 'uv sync --all-groups')"
    printf '%s\n' "$tool"
}

# ci_begin RULE "$@": start a check script. Sets CI_RULE and TREE (absolute) from the one argument.
ci_begin() {
    CI_RULE="$1"
    shift
    export CI_RULE
    [ "$#" -eq 1 ] || ci_die 2 "usage: $(basename "$0") <tree>"
    [ -d "$1" ] || ci_die 2 "unreadable input: tree $1 is not a directory"
    TREE="$(cd "$1" && pwd)"
    export TREE
    # cs_script tests need the pinned tsc; point every tree at this checkout's copy when present.
    if [ -z "${S2DOCS_TSC:-}" ] && [ -x "$CI_REPO_ROOT/tools/tsc/node_modules/.bin/tsc" ]; then
        S2DOCS_TSC="$CI_REPO_ROOT/tools/tsc/node_modules/.bin/tsc"
        export S2DOCS_TSC
    fi
}

# ci_py [ARGS...]: run python (script on stdin or args) with the tree's src first on the path and
# scripts/ci (cilib) importable. PYTHONPATH from the caller is replaced, never extended.
ci_py() {
    local py
    py="$(ci_python)"
    PYTHONPATH="$TREE/src:$CI_LIB_DIR" PYTHONDONTWRITEBYTECODE=1 CI_TREE="$TREE" "$py" "$@"
}

# ci_tree_files TREE: NUL-separated paths (relative to TREE) of every file git would ship from the
# working tree: tracked + untracked-but-not-ignored, minus tracked files deleted on disk.
ci_tree_files() {
    local tree="$1" py
    py="$(ci_python)"
    "$py" "$CI_LIB_DIR/cilib.py" tree-files "$tree"
}

# ci_export_tree TREE DEST: copy exactly the ci_tree_files of TREE into DEST (created).
ci_export_tree() {
    local tree="$1" dest="$2" list
    mkdir -p "$dest"
    list="$(mktemp "${TMPDIR:-/tmp}/ci-files.XXXXXX")"
    ci_tree_files "$tree" >"$list"
    (cd "$tree" && tar --null -T "$list" -cf -) | (cd "$dest" && tar -xf -)
    rm -f "$list"
}

# ci_commit_base TREE: print the base commit of the range a history check inspects (base..HEAD),
# and say on stderr where it came from. Exit 2 when no base can be determined.
#   1. CHC_COMMITS_BASE (explicit override, local use)
#   2. CI_MERGE_REQUEST_DIFF_BASE_SHA (merge-request pipelines)
#   3. the merge-base with the branch's upstream (local runs; falsification fixtures)
#   4. CI_COMMIT_BEFORE_SHA (branch pipelines: the tip before this push), when that commit exists
#      here (after a force-push the old tip may be unreachable and is not fetched: fall through)
#   5. the merge-base with origin/$CI_DEFAULT_BRANCH (first push of a new branch)
ci_commit_base() {
    local tree="$1" base before="${CI_COMMIT_BEFORE_SHA:-}"
    git -C "$tree" rev-parse --git-dir >/dev/null 2>&1 || ci_die 2 "unreadable input: $tree is not a git work tree"
    if [ "$before" = "0000000000000000000000000000000000000000" ]; then
        before=""
    elif [ -n "$before" ] && ! git -C "$tree" cat-file -e "${before}^{commit}" 2>/dev/null; then
        ci_log "${CI_RULE}: CI_COMMIT_BEFORE_SHA $before is not in this clone (force-push?); using the default branch"
        before=""
    fi
    if [ -n "${CHC_COMMITS_BASE:-}" ]; then
        base="$CHC_COMMITS_BASE"
        ci_log "${CI_RULE}: range base $base (from CHC_COMMITS_BASE)"
    elif [ -n "${CI_MERGE_REQUEST_DIFF_BASE_SHA:-}" ]; then
        base="$CI_MERGE_REQUEST_DIFF_BASE_SHA"
        ci_log "${CI_RULE}: range base $base (from CI_MERGE_REQUEST_DIFF_BASE_SHA)"
    elif git -C "$tree" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
        base="$(git -C "$tree" merge-base HEAD '@{upstream}')" || ci_die 2 "no merge-base between HEAD and its upstream"
        ci_log "${CI_RULE}: range base $base (merge-base with @{upstream})"
    elif [ -n "$before" ]; then
        base="$before"
        ci_log "${CI_RULE}: range base $base (from CI_COMMIT_BEFORE_SHA)"
    elif [ -n "${CI_DEFAULT_BRANCH:-}" ]; then
        git -C "$tree" fetch -q origin "$CI_DEFAULT_BRANCH" || ci_die 2 "cannot fetch origin/$CI_DEFAULT_BRANCH for the range base"
        base="$(git -C "$tree" merge-base HEAD FETCH_HEAD)" || ci_die 2 "no merge-base with origin/$CI_DEFAULT_BRANCH"
        ci_log "${CI_RULE}: range base $base (merge-base with origin/$CI_DEFAULT_BRANCH)"
    else
        ci_die 2 "cannot determine the commit range: set CHC_COMMITS_BASE=<commit>, or give the branch an upstream"
    fi
    git -C "$tree" cat-file -e "${base}^{commit}" 2>/dev/null || ci_die 2 "range base $base is not a commit in $tree"
    printf '%s\n' "$base"
}
