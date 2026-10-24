#!/usr/bin/env bash
# CHC-IOC reject: the payload package installed under an ignored node_modules directory.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
payload="$(plant ioc payload)"
printf '{"name": "%s", "version": "4.2.1"}\n' "$payload" | plant write "node_modules/$payload/package.json"
