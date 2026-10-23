#!/usr/bin/env bash
# CHC-SECRETS accept: a commit range with ordinary changes is scanned and accepted.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_settings.py <<'EOF'
"""Planted by ci/fixtures/CHC-SECRETS."""

import os

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
EOF
plant_commit "settings: read the GitHub token from the environment"
