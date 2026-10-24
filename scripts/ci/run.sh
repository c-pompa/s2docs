#!/usr/bin/env bash
# The one runner both pipelines use. It executes only the rules ci/feedback-rules.yaml assigns to
# the pipeline it is asked for, so which rule runs where is decided by the registry alone.
#
#   scripts/ci/run.sh enforce <pipeline>            every rule of <pipeline> against this checkout
#   scripts/ci/run.sh falsify <pipeline> [RULE...]  every rule's planted violations must be rejected
#                                                   (exit exactly 1) and its clean case accepted (0)
#   scripts/ci/run.sh check <RULE> [TREE]           one rule against TREE (default: this checkout)
#   scripts/ci/run.sh list <pipeline>               the rule ids of <pipeline>
#
# enforce runs with the caller's environment (CI variables decide commit ranges). falsify builds each
# case in a throwaway git clone of this checkout and runs the check under `env -i` with a short
# allowlist, so a fixture verdict cannot depend on the machine or the CI job it runs in. A fixture
# that must control the check's environment (a rule about the job environment itself) writes
# KEY=VALUE lines to <clone>/.git/ci-case.env (plant_env); they are added to that one case's check
# run only. The file lives in .git, so it is never part of a tree and enforce never reads it.
# Reports: $CI_REPORT_DIR (default ./ci-reports)/<pipeline>-<mode>.xml (JUnit) + .tsv.
# Exit: 0 everything held; 1 a rule failed or a falsification did not hold; 2 usage/environment.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
CI_RULE="run.sh"

usage() {
    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

registry() {
    "$(ci_python)" "$CI_LIB_DIR/registry.py" "$@"
}

REPORT_DIR="${CI_REPORT_DIR:-$CI_REPO_ROOT/ci-reports}"
WORK=""

cleanup() {
    if [ -n "$WORK" ] && [ -d "$WORK" ]; then
        if [ "${CI_KEEP_FIXTURES:-0}" = "1" ]; then
            ci_log "run.sh: kept fixture trees in $WORK (CI_KEEP_FIXTURES=1)"
        else
            rm -rf "$WORK"
        fi
    fi
}
trap cleanup EXIT

new_work() {
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/s2docs-ci.XXXXXX")"
}

# hermetic CMD...: run CMD with only the variables a check legitimately needs.
hermetic() {
    local assignments var
    assignments=("PATH=$PATH" "HOME=$HOME" "TMPDIR=${TMPDIR:-/tmp}" "LANG=${LANG:-C.UTF-8}"
        "GIT_CONFIG_NOSYSTEM=1" "GIT_CONFIG_GLOBAL=/dev/null"
        "GIT_AUTHOR_NAME=s2docs fixture" "GIT_AUTHOR_EMAIL=fixture@example.invalid"
        "GIT_COMMITTER_NAME=s2docs fixture" "GIT_COMMITTER_EMAIL=fixture@example.invalid")
    for var in LC_ALL XDG_CACHE_HOME XDG_DATA_HOME UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_INDEX_URL \
        UV_DEFAULT_INDEX UV_EXTRA_INDEX_URL SSL_CERT_FILE SSL_CERT_DIR CI_PYTHON S2DOCS_TSC; do
        if [ -n "${!var:-}" ]; then
            assignments+=("$var=${!var}")
        fi
    done
    env -i "${assignments[@]}" "$@"
}

verdict_of() {
    case "$1" in
        0) printf 'PASS' ;;
        1) printf 'FAIL' ;;
        *) printf 'ERROR' ;;
    esac
}

write_reports() {
    local pipeline="$1" mode="$2" results="$3"
    mkdir -p "$REPORT_DIR"
    cp "$results" "$REPORT_DIR/$pipeline-$mode.tsv"
    registry junit "$pipeline" "$mode" "$results" "$REPORT_DIR/$pipeline-$mode.xml" ||
        ci_die 2 "could not write the JUnit report for $pipeline $mode"
}

summary() {
    local results="$1" title="$2"
    printf '\n== %s\n' "$title"
    awk -F '\t' '{ printf "  %-7s %-14s %-44s %5ss\n", $3, $1, ($2 == "-" ? "" : $2), $4 }' "$results"
}

rules_of() {
    local pipeline="$1"
    if ! registry rules "$pipeline" >"$WORK/rules.tsv"; then
        ci_die 2 "ci/feedback-rules.yaml is invalid or has no pipeline '$pipeline' (see above)"
    fi
    [ -s "$WORK/rules.tsv" ] || ci_die 2 "pipeline '$pipeline' has no rules"
}

cmd_enforce() {
    [ "$#" -eq 1 ] || usage
    local pipeline="$1" id script fixtures clean title rc start log failed=0
    new_work
    rules_of "$pipeline"
    : >"$WORK/results.tsv"
    while IFS=$'\t' read -r id script fixtures clean title <&3; do
        log="$WORK/$id.log"
        printf '\n== %s %s\n' "$id" "$title"
        start="$(date +%s)"
        if bash "$CI_REPO_ROOT/$script" "$CI_REPO_ROOT" </dev/null 2>&1 | tee "$log"; then rc=0; else rc=$?; fi
        printf '%s\t-\t%s\t%s\t%s\n' "$id" "$(verdict_of "$rc")" "$(($(date +%s) - start))" "$log" >>"$WORK/results.tsv"
        [ "$rc" -eq 0 ] || failed=1
    done 3<"$WORK/rules.tsv"
    write_reports "$pipeline" enforce "$WORK/results.tsv"
    summary "$WORK/results.tsv" "$pipeline enforce"
    return "$failed"
}

# A pristine git clone of this checkout's shippable files, committed once, cloned per case.
make_base() {
    local base="$WORK/base"
    ci_export_tree "$CI_REPO_ROOT" "$base"
    hermetic git -C "$base" init -q -b fixture
    hermetic git -C "$base" add -A
    hermetic git -C "$base" commit -q --no-verify -m "fixture: base tree"
}

run_case() {
    # run_case RULE SCRIPT CASE_PATH|accept-unmodified-tree -> appends one row to results.tsv
    local id="$1" script="$2" case_path="$3" name expect tree log rc start verdict note line
    local case_env=()
    name="$(basename "$case_path")"
    case "$name" in
        accept-*) expect=0 ;;
        reject-*) expect=1 ;;
        *) ci_die 2 "fixture $case_path is neither accept-* nor reject-*" ;;
    esac
    tree="$WORK/cases/$id/${name%.*}"
    log="$WORK/cases/$id/${name%.*}.log"
    mkdir -p "$(dirname "$tree")"
    start="$(date +%s)"
    hermetic git clone -q "$WORK/base" "$tree"
    {
        printf '== %s %s (expect exit %s)\n' "$id" "$name" "$expect"
        case "$name" in
            *.patch)
                if ! hermetic git -C "$tree" apply --whitespace=nowarn "$case_path"; then
                    printf 'fixture %s no longer applies to this checkout; regenerate it (docs/FEEDBACK_CI.md)\n' "$name"
                    rc=3
                fi
                ;;
            *.sh)
                if ! (cd "$tree" && hermetic bash "$case_path" "$tree") </dev/null; then
                    printf 'fixture script %s failed while planting the case\n' "$name"
                    rc=3
                fi
                ;;
        esac
        if [ "${rc:-}" != "3" ] && [ -f "$tree/.git/ci-case.env" ]; then
            while IFS= read -r line || [ -n "$line" ]; do
                if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
                    case_env+=("$line")
                else
                    printf 'fixture %s wrote an invalid .git/ci-case.env line: %s\n' "$name" "$line"
                    rc=3
                fi
            done <"$tree/.git/ci-case.env"
            printf 'case environment: %s\n' "${case_env[*]-}"
        fi
    } >"$log" 2>&1
    if [ "${rc:-}" = "3" ]; then
        verdict="ERROR"
        note="fixture could not be planted"
    else
        if (cd "$tree" && hermetic env ${case_env[@]+"${case_env[@]}"} bash "$CI_REPO_ROOT/$script" "$tree") \
            </dev/null >>"$log" 2>&1; then rc=0; else rc=$?; fi
        if [ "$rc" -eq "$expect" ]; then
            verdict="HOLDS"
            note="exit $rc as expected"
        elif [ "$expect" -eq 1 ] && [ "$rc" -eq 0 ]; then
            verdict="BROKEN"
            note="the planted violation was ACCEPTED (exit 0): this check cannot fail on it"
        elif [ "$expect" -eq 1 ]; then
            verdict="BROKEN"
            note="the planted violation made the check error (exit $rc), not reject (exit 1)"
        else
            verdict="BROKEN"
            note="the clean case was not accepted (exit $rc)"
        fi
    fi
    printf '%-7s %s %s: %s\n' "$verdict" "$id" "$name" "$note"
    if [ "$verdict" != "HOLDS" ]; then
        sed 's/^/    | /' "$log"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$name" "$verdict" "$(($(date +%s) - start))" "$log" >>"$WORK/results.tsv"
    if [ "${CI_KEEP_FIXTURES:-0}" != "1" ]; then
        rm -rf "$tree"
    fi
    [ "$verdict" = "HOLDS" ]
}

# selected ID [WANTED...]: true when no rule was named on the command line, or ID was.
selected() {
    local id="$1" want
    shift
    [ "$#" -eq 0 ] && return 0
    for want in "$@"; do
        [ "$want" = "$id" ] && return 0
    done
    return 1
}

cmd_falsify() {
    [ "$#" -ge 1 ] || usage
    local pipeline="$1" id script fixtures clean title case_path failed=0 want ran=0 found
    shift
    new_work
    rules_of "$pipeline"
    for want in "$@"; do
        found="$(awk -F '\t' -v id="$want" '$1 == id { print $1 }' "$WORK/rules.tsv")"
        [ -n "$found" ] || ci_die 2 "rule $want is not a rule of $pipeline"
    done
    make_base
    : >"$WORK/results.tsv"
    shopt -s nullglob
    while IFS=$'\t' read -r id script fixtures clean title <&3; do
        selected "$id" "$@" || continue # only the rules named on the command line
        ran=$((ran + 1))
        printf '\n== %s %s\n' "$id" "$title"
        ci_need_dir "$CI_REPO_ROOT/$fixtures"
        if [ "$clean" = "tree" ]; then
            run_case "$id" "$script" "accept-unmodified-tree" || failed=1
        fi
        # the registry guarantees at least one reject-* (and an accept-* when clean: fixture)
        for case_path in "$CI_REPO_ROOT/$fixtures"/accept-* "$CI_REPO_ROOT/$fixtures"/reject-*; do
            run_case "$id" "$script" "$case_path" || failed=1
        done
    done 3<"$WORK/rules.tsv"
    shopt -u nullglob
    [ "$ran" -gt 0 ] || ci_die 2 "no rules selected"
    write_reports "$pipeline" falsify "$WORK/results.tsv"
    summary "$WORK/results.tsv" "$pipeline falsify"
    return "$failed"
}

cmd_check() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
    local id="$1" tree="${2:-$CI_REPO_ROOT}" script
    if ! script="$(registry get "$id" script)"; then
        ci_die 2 "unknown rule $id"
    fi
    bash "$CI_REPO_ROOT/$script" "$tree" </dev/null
}

cmd_list() {
    [ "$#" -eq 1 ] || usage
    new_work
    rules_of "$1"
    cut -f1 "$WORK/rules.tsv"
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift
case "$mode" in
    enforce) cmd_enforce "$@" ;;
    falsify) cmd_falsify "$@" ;;
    check) cmd_check "$@" ;;
    list) cmd_list "$@" ;;
    *) usage ;;
esac
