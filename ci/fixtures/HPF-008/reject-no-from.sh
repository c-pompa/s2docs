#!/usr/bin/env bash
# HPF-008 reject: the uvx launch has no --from, so it would install whatever PyPI serves as s2docs.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub plugin/mcp.json '\s*"--from",\s*"git\+[^"]*",' ''
