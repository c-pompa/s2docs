#!/usr/bin/env bash
# CHC-TESTS reject: a skipped test: unverified behaviour fails the house rule even when nothing failed.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write tests/test_planted_skip.py <<'EOF'
"""Planted by ci/fixtures/CHC-TESTS."""

import pytest


@pytest.mark.skip(reason="planted by ci/fixtures/CHC-TESTS")
def test_planted_skip():
    pass
EOF
