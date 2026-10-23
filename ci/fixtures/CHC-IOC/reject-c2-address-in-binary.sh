#!/usr/bin/env bash
# CHC-IOC reject: the C2 address inside a non-UTF-8 file: binary files are scanned too.
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
printf '\377\376\000planted %s\000\n' "$(plant ioc c2-address)" | plant write src/s2docs/planted.bin
