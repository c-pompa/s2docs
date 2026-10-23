#!/usr/bin/env bash
# HPF-004 reject: the reviewer's regression test no longer exists under its name.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace tests/test_ssh_hardening.py 'def test_register_host_rejects_unsafe_ssh_target(hdb, target):' 'def test_register_host_rejects_bad_target(hdb, target):'
