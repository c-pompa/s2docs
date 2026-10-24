#!/usr/bin/env bash
# HPF-003 reject: the code as reviewed at 531a24c7: scp gets '<target>:<path>' built from the stored target.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_as_reviewed
