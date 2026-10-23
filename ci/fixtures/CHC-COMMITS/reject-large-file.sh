#!/usr/bin/env bash
# CHC-COMMITS reject: a commit that adds a 6 MiB file.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
head -c 6291456 /dev/zero >planted.bin
plant_commit "assets: add a planted blob"
