#!/usr/bin/env bash
# CHC-SAST accept: an argv-list subprocess call without a shell is fine.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import subprocess


def status():
    return subprocess.run(["git", "status", "--short"], capture_output=True, check=False)
EOF
