#!/usr/bin/env bash
# CHC-RUNNER-NO-DOCKER-SOCK reject: inside a GitLab job the inspected path is redirected away from
# /var/run/docker.sock, which would hide a real socket from the check.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
scratch="$(plant_scratch)"
plant_env GITLAB_CI true
plant_env CHC_DOCKER_SOCKETS "$scratch/var/run/docker.sock"
