#!/usr/bin/env bash
# CHC-COMMITS reject: a work-in-progress commit.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
plant_commit "WIP: half of the deploy change"
