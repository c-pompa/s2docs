#!/usr/bin/env bash
# Refresh the s2docs index from Valve's SteamTracking GameTracking dumps (incremental).
#
# Why a script and not a server endpoint: heavy local ingest is deliberately CLI/cron-only — see
# docs/CLI_PLAN.md ("Process lifecycle: serve, mcp, sync, load are CLI-only ... heavy local ingest is a
# human/cron action"). The web UI therefore has no "sync" button; this is what runs instead.
#
# Run it by hand with
#   scripts/sync_index.sh                 # both games + source2.wiki
#   scripts/sync_index.sh --game cs2      # one game (extra args go straight to `s2docs sync`)
#
# Scheduled runs do NOT use this file: scripts/launchd/com.s2docs.sync.plist execs
# `.venv/bin/python -m s2docs.cli sync` directly, because a launchd-spawned /bin/bash cannot read files
# under /Users/you (it exits 126, "Operation not permitted"). This script is the human path, and the
# reference for what the agent runs.
#
# It always writes to <repo>/data: the data dir must be the one the running `s2docs serve` reads, and a
# source checkout resolves its default to <checkout>/data. S2DOCS_DATA_DIR is therefore pinned here so a
# cron/launchd environment can never write a shadow index somewhere else.
set -uo pipefail

REPO="${S2DOCS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BIN="$REPO/.venv/bin/s2docs"

if [ ! -d "$REPO/data" ]; then
  echo "s2docs sync: no data dir at $REPO/data — is the volume mounted?" >&2
  exit 1
fi
if [ ! -x "$BIN" ]; then
  echo "s2docs sync: no CLI at $BIN — run 'uv sync' in $REPO" >&2
  exit 1
fi

cd "$REPO" || exit 1
export S2DOCS_DATA_DIR="$REPO/data"
unset PYTHONPATH # a stray PYTHONPATH from a shell profile shadows the project's venv

echo "=== $(date -u +%FT%TZ) s2docs sync  repo=$REPO data=$S2DOCS_DATA_DIR args=$*"
"$BIN" sync "$@"
rc=$?
# A failed fetch (offline, GitHub hiccup) must be visible in the log but is not worth a retry loop here:
# the next scheduled run picks it up, and the index keeps serving the last complete build.
echo "=== $(date -u +%FT%TZ) s2docs sync exit=$rc"
exit "$rc"
