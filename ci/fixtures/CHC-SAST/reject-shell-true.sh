#!/usr/bin/env bash
# CHC-SAST reject: subprocess with shell=True (S602).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import subprocess


def run(command):
    return subprocess.run(command, shell=True, check=False)
EOF
