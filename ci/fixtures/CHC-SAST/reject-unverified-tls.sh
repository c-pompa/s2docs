#!/usr/bin/env bash
# CHC-SAST reject: an httpx request with certificate verification off (S501).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_sast.py <<'EOF'
"""Planted by ci/fixtures/CHC-SAST."""

import httpx


def fetch(url):
    return httpx.get(url, verify=False)
EOF
