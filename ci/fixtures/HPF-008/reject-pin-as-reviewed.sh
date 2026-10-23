#!/usr/bin/env bash
# HPF-008 reject: HEAD carries the fixes, but the uvx ref pins an ancestor whose server is as reviewed at
#   531a24c7.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_as_reviewed
plant_commit "server as reviewed at 531a24c7"
reviewed="$(git rev-parse HEAD)"
git revert --no-edit HEAD >/dev/null
plant_pin "$reviewed"
plant_commit "plugin: pin the uvx server at the reviewed code"
