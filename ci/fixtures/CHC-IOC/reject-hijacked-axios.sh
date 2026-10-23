#!/usr/bin/env bash
# CHC-IOC reject: a package.json that pins one of the two hijacked axios releases.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf '{\n  "name": "planted",\n  "private": true,\n  "dependencies": {"axios": "%s"}\n}\n' "$(plant ioc axios-hijacked)" |
    plant write tools/planted/package.json
