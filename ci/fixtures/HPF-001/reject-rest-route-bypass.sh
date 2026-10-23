#!/usr/bin/env bash
# HPF-001 reject: the REST route stores the target itself instead of calling hosts.register_host.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace src/s2docs/api/routes.py '    return hosts.register_host(db, host_id, **body.model_dump())' '    with db.writer() as conn, conn:  # planted by ci/fixtures/HPF-001: stores the target unvalidated
        conn.execute("INSERT OR REPLACE INTO hosts (id, ssh_target, shell) VALUES (?, ?, '"'"'powershell'"'"')",
                     (host_id, body.ssh_target))
    return hosts.get_host(db, host_id)'
