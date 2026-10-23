#!/usr/bin/env bash
# CHC-COMMITS reject: a commit that adds a leftover merge-conflict marker.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf 'a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> topic\n' >planted.txt
plant_commit "docs: add a planted note"
