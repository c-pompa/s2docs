#!/usr/bin/env bash
# CHC-COMMITS reject: a commit that adds trailing whitespace (git diff --check).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf 'line with trailing spaces   \n' >planted.txt
plant_commit "docs: add a planted note"
