#!/usr/bin/env bash
# HPF-009 reject: both snapshots agree but lie in the future, so neither quarantines anything.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub pyproject.toml '^exclude-newer = "[^"]*"$' 'exclude-newer = "2999-01-01T00:00:00Z"'
plant_sub plugin/mcp.json '("--exclude-newer",\s*")[^"]*(")' '\g<1>2999-01-01T00:00:00Z\g<2>'
