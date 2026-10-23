#!/usr/bin/env bash
# HPF-007 reject: --exclude-newer moved after the command, where uvx passes it to s2docs instead of using it.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
"$PLANT_PY" - <<'PY'
import json
from pathlib import Path

path = Path("plugin/mcp.json")
data = json.loads(path.read_text(encoding="utf-8"))
args = data["mcpServers"]["s2docs"]["args"]
at = args.index("--exclude-newer")
option = args[at:at + 2]
del args[at:at + 2]
args.extend(option)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
