#!/usr/bin/env bash
# HPF-008 accept: the uvx ref pins a full sha of an ancestor that carries the fixes (this fixture's base
#   commit).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_pin "$(git rev-parse HEAD)"
plant_commit "plugin: pin the uvx server at the fixed commit"
