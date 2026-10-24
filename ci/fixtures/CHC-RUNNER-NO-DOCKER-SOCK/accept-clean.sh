#!/usr/bin/env bash
# CHC-RUNNER-NO-DOCKER-SOCK accept: no socket at the inspected path and no DOCKER_HOST.
# (clean: fixture - the workstation running falsify may well have a Docker socket of its own.)
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
scratch="$(plant_scratch)"
mkdir -p "$scratch/var/run"
plant_env CHC_DOCKER_SOCKETS "$scratch/var/run/docker.sock"
