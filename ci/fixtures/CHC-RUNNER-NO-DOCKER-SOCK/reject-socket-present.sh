#!/usr/bin/env bash
# CHC-RUNNER-NO-DOCKER-SOCK reject: the runner mounted the daemon socket into the job (a fake one).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
scratch="$(plant_scratch)"
mkdir -p "$scratch/var/run"
: >"$scratch/var/run/docker.sock"
plant_env CHC_DOCKER_SOCKETS "$scratch/var/run/docker.sock"
