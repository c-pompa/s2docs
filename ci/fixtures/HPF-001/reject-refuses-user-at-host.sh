#!/usr/bin/env bash
# HPF-001 reject: over-tightened grammar that refuses every user@host target: the rule also requires the valid
#   forms to keep working.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py 'r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]{0,63})@)?"' 'r"(?:(?P<user>(?!))@)?"'
