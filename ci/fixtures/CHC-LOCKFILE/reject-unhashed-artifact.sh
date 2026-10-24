#!/usr/bin/env bash
# CHC-LOCKFILE reject: one locked wheel loses its sha256 (uv lock --check does not notice; the rule must).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
"$PLANT_PY" - <<'PY'
import re
from pathlib import Path

path = Path("uv.lock")
text = path.read_text(encoding="utf-8")
match = re.search(r'(wheels = \[\n    \{ url = "[^"]+"), hash = "sha256:[0-9a-f]{64}"', text)
assert match, "stale fixture: no hashed wheel entry in uv.lock"
path.write_text(text[:match.start()] + match.group(1) + text[match.end():], encoding="utf-8")
PY
