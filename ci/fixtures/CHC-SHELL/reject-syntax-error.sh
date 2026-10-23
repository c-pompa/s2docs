#!/usr/bin/env bash
# CHC-SHELL reject: a shell script that does not parse (an if without fi).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write scripts/planted_syntax.sh <<'EOF'
#!/usr/bin/env bash
# Planted by ci/fixtures/CHC-SHELL.
if [ -n "${1:-}" ]; then
    echo "never closed"
EOF
