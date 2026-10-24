#!/usr/bin/env bash
# CHC-RUNNER-NO-DOCKER-SOCK reject: no socket file, but DOCKER_HOST points the job at a daemon.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
scratch="$(plant_scratch)"
plant_env CHC_DOCKER_SOCKETS "$scratch/var/run/docker.sock"
plant_env DOCKER_HOST "tcp://docker:2375"
