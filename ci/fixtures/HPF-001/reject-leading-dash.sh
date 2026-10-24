#!/usr/bin/env bash
# HPF-001 reject: the host's first character may be '-', so '-x' and '-host' are accepted.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/hosts.py 'r"(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]{0,252})"' 'r"(?P<host>[A-Za-z0-9_-][A-Za-z0-9._-]{0,252})"'
