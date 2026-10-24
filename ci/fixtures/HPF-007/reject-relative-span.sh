#!/usr/bin/env bash
# HPF-007 reject: --exclude-newer is a relative span, which moves every day.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '("--exclude-newer",\s*")[^"]*(")' '\g<1>3 days\g<2>'
