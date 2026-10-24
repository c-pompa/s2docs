#!/usr/bin/env bash
# CHC-PACKAGING reject: the console script points at a function that does not exist.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_replace pyproject.toml 's2docs = "s2docs.cli:main"' 's2docs = "s2docs.cli:no_such_main"'
