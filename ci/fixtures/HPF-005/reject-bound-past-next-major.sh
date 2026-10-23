#!/usr/bin/env bash
# HPF-005 reject: pydantic bounded at <4, one major past the next breaking line.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"pydantic>=([^,"]+),<[^"]+"' '"pydantic>=\g<1>,<4"'
