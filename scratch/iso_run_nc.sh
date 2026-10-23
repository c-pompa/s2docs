#!/bin/bash
# usage: iso_run_nc.sh "cmd1" "cmd2" ...
H='ssh -i ~/.ssh/homelab_ed25519 -o IdentitiesOnly=yes pompa@192.0.2.172'
CMDS=""
for c in "$@"; do
  esc=$(printf '%s' "$c" | sed 's/"/\\"/g')
  CMDS="$CMDS -Cmds \"$esc\""
done
$H "powershell -NoProfile -ExecutionPolicy Bypass -File C:/iso_build/iso_nc.ps1 $CMDS" 2>&1
