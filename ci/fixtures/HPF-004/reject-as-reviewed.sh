#!/usr/bin/env bash
# HPF-004 reject: the regression test is present but the code is as reviewed at 531a24c7, so its cases fail.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_as_reviewed
