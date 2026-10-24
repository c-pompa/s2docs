#!/usr/bin/env bash
# CHC-LOCKFILE reject: one locked package comes from a registry other than PyPI (uv lock --check does not notice;
#   the rule must).
set -euo pipefail
# shellcheck source=scripts/ci/plant.sh
. "$(dirname "$0")/../../../scripts/ci/plant.sh"
plant_sub uv.lock '(name = "anyio"\nversion = "[^"]+"\nsource = \{ registry = ")https://pypi\.org/simple(" \})' '\g<1>https://mirror.example.invalid/simple\g<2>'
