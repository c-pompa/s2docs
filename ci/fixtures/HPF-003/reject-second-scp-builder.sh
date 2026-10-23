#!/usr/bin/env bash
# HPF-003 reject: a second scp argv is built outside hosts.scp_argv.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant append src/s2docs/deploys.py <<'EOF'


def _planted_raw_scp(host, local, remote):
    """Planted by ci/fixtures/HPF-003: an scp argv built outside hosts.scp_argv."""
    return subprocess.run(["scp", str(local), f"{host.ssh_target}:{remote}"], capture_output=True, text=True)
EOF
