#!/usr/bin/env bash
# HPF-005 reject: fastapi goes back to a bare floor, the shape the review flagged.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"fastapi>=([^,"]+),<[^"]+"' '"fastapi>=\g<1>"'
