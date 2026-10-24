#!/usr/bin/env bash
# HPF-007 reject: --exclude-newer predates the floors, so a first start cannot resolve at all.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '("--exclude-newer",\s*")[^"]*(")' '\g<1>2023-01-01T00:00:00Z\g<2>'
