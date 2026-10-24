#!/usr/bin/env bash
# HPF-009 reject: pyproject.toml loses [tool.uv] exclude-newer, so CI resolves today's releases.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '^exclude-newer = "[^"]*"$\n' ''
