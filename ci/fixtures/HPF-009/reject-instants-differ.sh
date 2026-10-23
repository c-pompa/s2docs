#!/usr/bin/env bash
# HPF-009 reject: CI resolves one day earlier than the plugin's first start.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
"$PLANT_PY" - <<'PY'
import re
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

path = Path("pyproject.toml")
text = path.read_text(encoding="utf-8")
value = tomllib.loads(text)["tool"]["uv"]["exclude-newer"]
earlier = datetime.fromisoformat(value.replace("Z", "+00:00")) - timedelta(days=1)
new, count = re.subn(r'^exclude-newer = "[^"]*"$', f'exclude-newer = "{earlier.strftime("%Y-%m-%dT%H:%M:%SZ")}"',
                     text, flags=re.M)
assert count == 1, "stale fixture: no [tool.uv] exclude-newer line"
path.write_text(new, encoding="utf-8")
PY
