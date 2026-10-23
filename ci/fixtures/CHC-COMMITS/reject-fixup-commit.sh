#!/usr/bin/env bash
# CHC-COMMITS reject: an unsquashed fixup! commit.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
plant_commit "fixup! hosts: validate ssh_target"
