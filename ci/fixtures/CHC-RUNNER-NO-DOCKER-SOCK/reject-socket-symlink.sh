#!/usr/bin/env bash
# CHC-RUNNER-NO-DOCKER-SOCK reject: the socket path is a symlink to the daemon (Docker Desktop's shape),
# dangling here, so only the link itself exists.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
scratch="$(plant_scratch)"
mkdir -p "$scratch/var/run"
ln -s "$scratch/elsewhere/docker.sock" "$scratch/var/run/docker.sock"
plant_env CHC_DOCKER_SOCKETS "$scratch/var/run/docker.sock"
