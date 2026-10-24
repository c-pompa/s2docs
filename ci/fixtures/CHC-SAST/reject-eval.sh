#!/usr/bin/env bash
# CHC-SAST reject: eval of a string (S307).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

def calculate(expression):
    return eval(expression)
EOF
