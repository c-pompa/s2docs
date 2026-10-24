#!/usr/bin/env bash
# CHC-TESTS reject: a test module that does not import, so pytest stops at collection.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write tests/test_planted_collection.py <<'EOF'
"""Planted by ci/fixtures/CHC-TESTS."""

import s2docs.no_such_module  # noqa: F401
EOF
