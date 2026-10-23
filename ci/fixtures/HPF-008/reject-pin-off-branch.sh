#!/usr/bin/env bash
# HPF-008 reject: the uvx ref pins a commit on another branch, not an ancestor of HEAD.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
branch="$(git rev-parse --abbrev-ref HEAD)"
git checkout -q -b planted-side
echo "side" >side.txt
plant_commit "side branch commit"
side="$(git rev-parse HEAD)"
git checkout -q "$branch"
plant_pin "$side"
plant_commit "plugin: pin the uvx server at a side-branch commit"
