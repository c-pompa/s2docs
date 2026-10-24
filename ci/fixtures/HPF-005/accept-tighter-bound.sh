#!/usr/bin/env bash
# HPF-005 accept: an upper bound tighter than the next major that still admits the locked version.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"pydantic>=([^,"]+),<3"' '"pydantic>=\g<1>,<2.99"'
