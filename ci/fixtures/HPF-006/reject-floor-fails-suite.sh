#!/usr/bin/env bash
# HPF-006 reject: fastapi is floored at 0.110.0, which installs but fails the suite at the snapshot.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '"fastapi>=[^,"]+,' '"fastapi>=0.110.0,'
