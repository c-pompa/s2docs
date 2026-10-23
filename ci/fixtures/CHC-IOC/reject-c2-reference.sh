#!/usr/bin/env bash
# CHC-IOC reject: a config file that names the C2 domain.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf 'ENDPOINT=https://%s/beacon\n' "$(plant ioc c2-domain)" | plant write plugin/planted.env
