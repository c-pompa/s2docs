#!/usr/bin/env bash
# HPF-004 reject: the test survives but no case starts with '-oProxyCommand=' any more.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace tests/test_ssh_hardening.py 'REVIEWER_TARGET = "-oProxyCommand=touch /tmp/s2docs-pwned"' 'REVIEWER_TARGET = "-oLocalCommand=touch /tmp/s2docs-pwned"'
plant_replace tests/test_ssh_hardening.py '    "-oProxyCommand=sh -c id",
' ''
