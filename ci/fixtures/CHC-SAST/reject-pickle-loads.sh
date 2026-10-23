#!/usr/bin/env bash
# CHC-SAST reject: pickle.loads of untrusted bytes (S301).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import pickle


def load(blob):
    return pickle.loads(blob)
EOF
