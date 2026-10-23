#!/usr/bin/env bash
# CHC-PACKAGING reject: the package imports pytest, which only the dev group installs: it works in the checkout and
#   breaks installed.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant append src/s2docs/__init__.py <<'EOF'

import pytest  # noqa: E402,F401  planted by ci/fixtures/CHC-PACKAGING
EOF
