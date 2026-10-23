#!/usr/bin/env bash
# HPF-007 accept: the same instant written with a -05:00 offset is just as fixed.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
"$PLANT_PY" - <<'PY'
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

path = Path("plugin/mcp.json")
data = json.loads(path.read_text(encoding="utf-8"))
args = data["mcpServers"]["s2docs"]["args"]
at = args.index("--exclude-newer") + 1
moment = datetime.fromisoformat(args[at].replace("Z", "+00:00"))
args[at] = moment.astimezone(timezone(timedelta(hours=-5))).isoformat()
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
