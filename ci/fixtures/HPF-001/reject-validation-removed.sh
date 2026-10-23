#!/usr/bin/env bash
# HPF-001 reject: register_host no longer validates; ssh_argv still would at use time, but the target is
#   stored.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py '    parse_ssh_target(ssh_target)  # refuse option-like / malformed targets before anything is stored
' ''
