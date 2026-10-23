#!/usr/bin/env bash
# HPF-003 reject: scp_argv builds the host:path operand from the stored 'host:port', not the destination.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py '    operand = f"{target.destination}:{remote}"' '    operand = f"{host.ssh_target}:{remote}"'
