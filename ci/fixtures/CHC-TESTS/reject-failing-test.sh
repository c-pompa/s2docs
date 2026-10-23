#!/usr/bin/env bash
# CHC-TESTS reject: a test that fails.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write tests/test_planted_failure.py <<'EOF'
"""Planted by ci/fixtures/CHC-TESTS."""


def test_planted_failure():
    assert 1 + 1 == 3
EOF
