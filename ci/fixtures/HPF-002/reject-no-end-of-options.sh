#!/usr/bin/env bash
# HPF-002 reject: ssh_argv drops the '--' before the destination.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py 'return ["ssh", *_connection_options(host, target, port_flag="-p"), "--", target.destination]' 'return ["ssh", *_connection_options(host, target, port_flag="-p"), target.destination]'
