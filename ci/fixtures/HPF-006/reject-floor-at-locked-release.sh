#!/usr/bin/env bash
# HPF-006 reject: fastapi is floored at the version uv.lock tests - the newest release, as reviewed - while
#   older releases pass.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
locked="$("$PLANT_PY" -c 'import tomllib; print(next(p["version"] for p in tomllib.load(open("uv.lock", "rb"))["package"] if p["name"] == "fastapi"))')"
plant_sub pyproject.toml '"fastapi>=[^,"]+,' "\"fastapi>=$locked,"
