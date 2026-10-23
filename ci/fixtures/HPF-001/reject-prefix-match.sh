#!/usr/bin/env bash
# HPF-001 reject: re.match instead of fullmatch: 'host -oProxyCommand=id' validates on its 'host' prefix.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py 'match = _SSH_TARGET_RE.fullmatch(target)' 'match = _SSH_TARGET_RE.match(target)'
