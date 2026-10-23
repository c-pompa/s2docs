#!/usr/bin/env bash
# CHC-SAST reject: os.system starts a shell (S605).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import os


def run(command):
    return os.system(command)
EOF
