#!/usr/bin/env bash
# CHC-COMMITS reject: a body that starts on the second line, with no blank line after the subject.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
echo "planted" >planted.txt
git add -A
printf 'deploys: copy files\nthe body starts right under the subject\n' | git commit -q --no-verify -F -
