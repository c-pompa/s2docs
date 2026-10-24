#!/usr/bin/env bash
# HPF-005 reject: a 0.x package bounded at <1, which admits every breaking 0.y release.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"uvicorn\[standard\]>=([^,"]+),<[^"]+"' '"uvicorn[standard]>=\g<1>,<1"'
