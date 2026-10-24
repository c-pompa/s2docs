#!/usr/bin/env bash
# HPF-008 reject: the uvx ref is a full sha that is not in this repository's history.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_pin deadbeefdeadbeefdeadbeefdeadbeefdeadbeef
