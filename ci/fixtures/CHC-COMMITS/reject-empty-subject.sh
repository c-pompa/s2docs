#!/usr/bin/env bash
# CHC-COMMITS reject: a commit with an empty message.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
git add -A
git commit -q --no-verify --allow-empty-message -m ""
