#!/usr/bin/env bash
# HPF-002 reject: a second ssh argv is built outside hosts.ssh_argv, so the '--' has more than one owner.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant append src/s2docs/builds.py <<'EOF'


def _planted_raw_ssh(host, command):
    """Planted by ci/fixtures/HPF-002: an ssh argv built outside hosts.ssh_argv."""
    return subprocess.run(["ssh", host.ssh_target, command], capture_output=True, text=True)
EOF
