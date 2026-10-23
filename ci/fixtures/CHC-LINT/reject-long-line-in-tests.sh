#!/usr/bin/env bash
# CHC-LINT reject: a test line longer than the project's 120-column limit (E501).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write tests/test_planted_lint.py <<'EOF'
"""Planted by ci/fixtures/CHC-LINT."""


def test_planted():
    assert "this planted line is deliberately longer than the one hundred and twenty columns the project allows, by design" != ""
EOF
