#!/usr/bin/env bash
# CHC-PACKAGING reject: the build backend excludes a tracked subpackage from the wheel.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '^\[tool\.uv\]$' '[tool.uv.build-backend]
wheel-exclude = ["s2docs/learning/**"]

[tool.uv]'
