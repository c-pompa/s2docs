#!/usr/bin/env bash
# Helpers for the falsification fixtures, ci/fixtures/<RULE>/(accept|reject)-<slug>.sh. Sourced by
# a fixture, never executed. A fixture runs inside a throwaway git clone of this checkout (its working
# directory, also "$1") and plants exactly one change: a violation its rule must reject (exit 1), or
# a variation the rule must accept (exit 0).
#
# Every edit names the exact text it changes and exits 3 when that text is missing or ambiguous, so
# a fixture that no longer matches the code is reported by run.sh as "could not be planted" (ERROR,
# the falsification job fails) - it can never quietly turn into a case that passes.
PLANT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLANT_PY="${CI_PYTHON:-$PLANT_ROOT/.venv/bin/python}"
if [ ! -x "$PLANT_PY" ]; then
    echo "plant: no python at $PLANT_PY (run 'uv sync --all-groups', or set CI_PYTHON)" >&2
    exit 3
fi

# plant SUBCOMMAND ARGS...: see scripts/ci/plant.py (replace, sub, write, append, fake-token, ioc).
plant() { "$PLANT_PY" "$PLANT_ROOT/scripts/ci/plant.py" "$@"; }

# plant_replace FILE OLD NEW: the one occurrence of OLD in FILE becomes NEW (NEW may be empty).
plant_replace() { plant replace "$@"; }

# plant_sub FILE REGEX REPLACEMENT: re.sub (multiline) that must match exactly once.
plant_sub() { plant sub "$@"; }

# plant_pin REF: point the uvx `--from git+<url>@<ref>` in plugin/mcp.json at REF.
plant_pin() { plant sub plugin/mcp.json '("git\+[^"@]+@)[^"]+(")' "\\g<1>$1\\g<2>"; }

# plant_as_reviewed: put hosts.py, deploys.py and builds.py back to the code reviewed at 531a24c7
# (review 5631127162) - the reviewer's own counterexample, ssh_target stored and passed verbatim.
plant_as_reviewed() {
    git apply --whitespace=nowarn "$PLANT_ROOT/ci/fixtures/as-reviewed-531a24c7.patch" ||
        { echo "plant: ci/fixtures/as-reviewed-531a24c7.patch no longer applies (docs/FEEDBACK_CI.md)" >&2; exit 3; }
}

# plant_env KEY VALUE: run this case's check with KEY=VALUE in its environment (run.sh reads
# .git/ci-case.env; for rules about the job environment, which the hermetic harness otherwise strips).
plant_env() {
    case "$2" in
        *$'\n'*) echo "plant: plant_env $1: a value cannot contain a newline" >&2; exit 2 ;;
    esac
    printf '%s=%s\n' "$1" "$2" >>"$(git rev-parse --absolute-git-dir)/ci-case.env"
}

# plant_scratch: print a scratch directory inside the clone's .git (outside the tree, removed with the
# clone), for files a case plants outside the repository (a fake socket, a fake config).
plant_scratch() {
    local dir
    dir="$(git rev-parse --absolute-git-dir)/ci-case-scratch"
    mkdir -p "$dir"
    printf '%s\n' "$dir"
}

# plant_commit MESSAGE: commit everything in the fixture clone.
plant_commit() { git add -A && git commit -q --no-verify -m "$1"; }
