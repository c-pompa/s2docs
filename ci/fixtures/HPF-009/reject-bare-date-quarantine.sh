#!/usr/bin/env bash
# HPF-009 reject: [tool.uv] exclude-newer is a bare date (local time zone).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '^exclude-newer = "[^"]*"$' 'exclude-newer = "2026-10-10"'
