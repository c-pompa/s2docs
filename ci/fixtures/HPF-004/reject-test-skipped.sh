#!/usr/bin/env bash
# HPF-004 reject: the regression test is collected but skipped.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace tests/test_ssh_hardening.py 'def test_register_host_rejects_unsafe_ssh_target(hdb, target):' '@pytest.mark.skip(reason="planted by ci/fixtures/HPF-004")
def test_register_host_rejects_unsafe_ssh_target(hdb, target):'
