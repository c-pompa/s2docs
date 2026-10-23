#!/usr/bin/env bash
# HPF-009 reject: [tool.uv] exclude-newer is a relative span, not the users' fixed instant.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '^exclude-newer = "[^"]*"$' 'exclude-newer = "3 days"'
