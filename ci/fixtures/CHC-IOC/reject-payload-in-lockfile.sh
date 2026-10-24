#!/usr/bin/env bash
# CHC-IOC reject: a package-lock.json that installs the payload package.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf '{\n  "lockfileVersion": 3,\n  "packages": {\n    "node_modules/%s": {"version": "4.2.1"}\n  }\n}\n' "$(plant ioc payload)" |
    plant write tools/planted/package-lock.json
