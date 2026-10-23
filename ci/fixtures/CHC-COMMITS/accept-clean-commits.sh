#!/usr/bin/env bash
# CHC-COMMITS accept: two well-formed commits are checked and accepted.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
plant_commit "docs: add a planted note"
echo "more" >>planted.txt
git add -A
printf 'docs: extend the planted note\n\nA body after a blank line is fine.\n' | git commit -q --no-verify -F -
