#!/usr/bin/env bash
# HPF-003 reject: deploy_push and scp_argv trust the stored target, so a row written without validation
#   reaches scp as '-o...:path'.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/deploys.py '    hosts.parse_ssh_target(host.ssh_target)
' ''
plant_replace src/s2docs/hosts.py '    target = parse_ssh_target(host.ssh_target)
    operand = f"{target.destination}:{remote}"
    if operand.startswith("-"):  # unreachable for a validated destination; kept as the invariant
        raise HostError(f"refusing an scp operand that starts with '"'"'-'"'"': {operand!r}")' '    target = SshTarget(destination=host.ssh_target)  # planted by ci/fixtures/HPF-003
    operand = f"{target.destination}:{remote}"'
