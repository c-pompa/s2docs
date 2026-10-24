#!/usr/bin/env bash
# CHC-SECRETS reject: a GitHub token in a shipped file (uncommitted: the working-tree scan must catch it).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
token="$(plant fake-token github-pat)"
printf '"""Planted by ci/fixtures/CHC-SECRETS."""\n\nGITHUB_TOKEN = "%s"\n' "$token" | plant write src/s2docs/_planted_settings.py
