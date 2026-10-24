#!/usr/bin/env bash
# CHC-LINT reject: a shell script whose embedded Python heredoc has an unused import: ruff never sees it unless
#   the rule extracts it.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant write scripts/planted_heredoc.sh <<'EOF'
#!/usr/bin/env bash
# Planted by ci/fixtures/CHC-LINT.
python3 - <<'PY'
import os

print("planted")
PY
EOF
