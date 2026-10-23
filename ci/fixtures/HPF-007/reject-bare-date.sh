#!/usr/bin/env bash
# HPF-007 reject: --exclude-newer is a bare date, which each installer reads in its own time zone.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '("--exclude-newer",\s*")[^"]*(")' '\g<1>2026-10-10\g<2>'
