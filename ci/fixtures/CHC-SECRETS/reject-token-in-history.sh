#!/usr/bin/env bash
# CHC-SECRETS reject: a token committed and then deleted: the tree is clean, the history is not.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
token="$(plant fake-token github-pat)"
printf '"""Planted by ci/fixtures/CHC-SECRETS."""\n\nGITHUB_TOKEN = "%s"\n' "$token" | plant write src/s2docs/_planted_settings.py
plant_commit "settings: read the GitHub token from the module"
git rm -q src/s2docs/_planted_settings.py
plant_commit "settings: drop the hard-coded token again"
