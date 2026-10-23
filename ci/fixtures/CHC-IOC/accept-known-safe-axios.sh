#!/usr/bin/env bash
# CHC-IOC accept: axios pinned exactly at a known-safe release.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write tools/planted/package.json <<'EOF'
{
  "name": "planted",
  "private": true,
  "dependencies": {"axios": "1.14.0"}
}
EOF
