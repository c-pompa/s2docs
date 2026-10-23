#!/usr/bin/env bash
# CHC-SAST reject: yaml.load without a safe loader (S506).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import yaml


def load(text):
    return yaml.load(text)
EOF
