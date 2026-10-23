#!/usr/bin/env bash
# CHC-SHELL accept: a new, correctly quoted script is checked and accepted.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write scripts/planted_clean.sh <<'EOF'
#!/usr/bin/env bash
# Planted by ci/fixtures/CHC-SHELL.
set -euo pipefail
rm -rf -- "${1:?usage: planted_clean.sh DIR}/build"
EOF
