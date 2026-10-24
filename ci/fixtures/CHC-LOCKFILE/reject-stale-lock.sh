#!/usr/bin/env bash
# CHC-LOCKFILE reject: pyproject.toml changes a requirement and uv.lock is not regenerated.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"pydantic>=([^,"]+),' '"pydantic>=\g<1>.post0,'
