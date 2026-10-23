#!/usr/bin/env bash
# HPF-005 accept: a >=floor,!=broken,<bound range, the shape the catalog policy recommends for skipping one
#   bad release.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"fastapi>=([^,"]+),<([^"]+)"' '"fastapi>=\g<1>,!=0.120.0,<\g<2>"'
