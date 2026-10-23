#!/usr/bin/env bash
# CHC-LINT reject: a new module with an unused import (F401).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write src/s2docs/_planted_lint.py <<'EOF'
"""Planted by ci/fixtures/CHC-LINT."""

import os
EOF
