#!/usr/bin/env bash
# CHC-COMMITS reject: a 130-character subject.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
plant_commit "deploys: $(head -c 121 /dev/zero | tr '\0' x)"
