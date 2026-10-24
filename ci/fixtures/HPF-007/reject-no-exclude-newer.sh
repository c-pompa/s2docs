#!/usr/bin/env bash
# HPF-007 reject: the uvx args lose --exclude-newer, as reviewed: first start resolves whatever is newest that
#   day.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '\s*"--exclude-newer",\s*"[^"]*",' ''
