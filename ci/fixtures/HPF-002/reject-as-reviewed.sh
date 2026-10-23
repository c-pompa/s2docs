#!/usr/bin/env bash
# HPF-002 reject: the code as reviewed at 531a24c7: ssh gets the bare target before the remote command, with
#   no '--'.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_as_reviewed
