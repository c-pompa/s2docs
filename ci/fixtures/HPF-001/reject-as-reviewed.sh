#!/usr/bin/env bash
# HPF-001 reject: the code as reviewed at 531a24c7: register_host stores ssh_target verbatim, so
#   '-oProxyCommand=...' is accepted.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_as_reviewed
