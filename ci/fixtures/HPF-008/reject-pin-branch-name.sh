#!/usr/bin/env bash
# HPF-008 reject: the uvx ref is a branch name, which can move to code nobody reviewed.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_pin main
