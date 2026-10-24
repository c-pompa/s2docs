#!/usr/bin/env bash
# rule: CHC-RUNNER-NO-DOCKER-SOCK
# pipeline: cpompa-hermes-ci
# source: coordinator security finding 2026-10-23: a runner mounted /var/run/docker.sock into job containers
# enforces: the job this check runs in has no Docker daemon socket (/var/run/docker.sock, as a
#   file, socket or symlink, dangling or not) and no DOCKER_HOST in its environment, so code under
#   test cannot drive the host's Docker daemon (root on the runner host).
#
# usage: CHC-RUNNER-NO-DOCKER-SOCK-job-env.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The check inspects the environment it runs in, not the tree. CHC_DOCKER_SOCKETS (':'-separated
# paths, default /var/run/docker.sock) redirects it for the falsification fixtures, which plant a
# fake socket in a scratch directory; inside a GitLab job (GITLAB_CI=true) a redirect is itself a
# violation, because it would hide the real socket from the check. Pure bash: no python, no tools.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-RUNNER-NO-DOCKER-SOCK "$@"
DEFAULT_SOCKETS="/var/run/docker.sock"
sockets="${CHC_DOCKER_SOCKETS-$DEFAULT_SOCKETS}"
[ -n "$sockets" ] || ci_die 2 "CHC_DOCKER_SOCKETS is empty; no socket path to inspect"

failed=0
if [ "${GITLAB_CI:-}" = "true" ] && [ "$sockets" != "$DEFAULT_SOCKETS" ]; then
    echo "FAIL CHC-RUNNER-NO-DOCKER-SOCK: CHC_DOCKER_SOCKETS redirects the check away from $DEFAULT_SOCKETS inside a CI job ($sockets)"
    failed=1
fi
IFS=':' read -r -a paths <<<"$sockets"
for path in "${paths[@]}"; do
    [ -n "$path" ] || ci_die 2 "CHC_DOCKER_SOCKETS has an empty entry: '$sockets'"
    if [ -e "$path" ] || [ -L "$path" ]; then
        kind="file"
        if [ -L "$path" ]; then
            kind="symlink to $(readlink "$path")"
        elif [ -S "$path" ]; then
            kind="socket"
        fi
        echo "FAIL CHC-RUNNER-NO-DOCKER-SOCK: $path exists in the job environment ($kind); the runner mounts the Docker daemon socket into job containers"
        failed=1
    fi
done
if [ -n "${DOCKER_HOST+set}" ]; then
    echo "FAIL CHC-RUNNER-NO-DOCKER-SOCK: DOCKER_HOST is set in the job environment ('${DOCKER_HOST}'); jobs must not reach a Docker daemon"
    failed=1
fi
[ "$failed" -eq 0 ] || exit 1
echo "PASS CHC-RUNNER-NO-DOCKER-SOCK: no Docker socket at $sockets and no DOCKER_HOST"
