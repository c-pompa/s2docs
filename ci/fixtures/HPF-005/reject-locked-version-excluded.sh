#!/usr/bin/env bash
# HPF-005 reject: pydantic's upper bound excludes the version uv.lock tests.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
locked="$("$PLANT_PY" -c 'import tomllib; print(next(p["version"] for p in tomllib.load(open("uv.lock", "rb"))["package"] if p["name"] == "pydantic"))')"
plant_sub pyproject.toml '"pydantic>=([^,"]+),<[^"]+"' "\"pydantic>=\\g<1>,<$locked\""
