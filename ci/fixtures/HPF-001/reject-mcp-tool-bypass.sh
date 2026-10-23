#!/usr/bin/env bash
# HPF-001 reject: the register_host MCP tool stores the target itself instead of calling hosts.register_host.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/mcp_server.py '            lambda: hosts.register_host(
                db, host_id, ssh_target, hostname=hostname, ssh_key=ssh_key, shell=shell, os=os, notes=notes
            )' '            lambda: _planted_store(db, host_id, ssh_target)'
plant append src/s2docs/mcp_server.py <<'EOF'


def _planted_store(db, host_id, ssh_target):
    """Planted by ci/fixtures/HPF-001: the MCP tool writes the row without hosts.register_host."""
    with db.writer() as conn, conn:
        conn.execute("INSERT OR REPLACE INTO hosts (id, ssh_target, shell) VALUES (?, ?, 'powershell')",
                     (host_id, ssh_target))
    return hosts.get_host(db, host_id)
EOF
