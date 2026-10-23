#!/usr/bin/env bash
# HPF-005 reject: mcp pinned with == instead of a >=floor,<bound range.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"mcp\[cli\]>=([^,"]+),<[^"]+"' '"mcp[cli]==\g<1>"'
