#!/usr/bin/env bash
# CHC-SHELL reject: rm -rf on an unquoted expansion (ShellCheck SC2086).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write scripts/planted_unquoted.sh <<'EOF'
#!/usr/bin/env bash
# Planted by ci/fixtures/CHC-SHELL.
set -eu
rm -rf $1/build
EOF
