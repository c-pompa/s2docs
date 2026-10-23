#!/usr/bin/env bash
# HPF-006 reject: pydantic is floored at 2.0.0, which the other floors (mcp needs a newer pydantic) never let
#   install: an untested floor.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"pydantic>=[^,"]+,' '"pydantic>=2.0.0,'
