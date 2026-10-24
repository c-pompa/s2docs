#!/usr/bin/env bash
# HPF-002 reject: ssh_argv passes the stored 'host:port' as the destination instead of '-p port' before '--'.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py 'return ["ssh", *_connection_options(host, target, port_flag="-p"), "--", target.destination]' 'return ["ssh", *_connection_options(host, target, port_flag="-p"), "--", host.ssh_target]'
