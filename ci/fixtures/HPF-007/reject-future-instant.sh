#!/usr/bin/env bash
# HPF-007 reject: --exclude-newer is in the future, so it constrains nothing yet.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '("--exclude-newer",\s*")[^"]*(")' '\g<1>2999-01-01T00:00:00Z\g<2>'
