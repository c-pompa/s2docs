#!/usr/bin/env bash
# HPF-008 reject: the uvx ref is an abbreviated sha.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_pin "$(git rev-parse --short=12 HEAD)"
