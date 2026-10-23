#!/usr/bin/env bash
# Provision a CI job with exactly the tools the checks need. Written for the Debian-based uv image the
# GitLab jobs run in (.gitlab-ci.yml), and runnable the same way on a workstation inside that image:
#
#   docker run --rm -v "$PWD:/src" -w /src ghcr.io/astral-sh/uv:0.13.5-python3.11-trixie \
#       bash -c 'scripts/ci/bootstrap.sh python node gitleaks && scripts/ci/run.sh enforce cpompa-hermes-ci'
#
# Components (all of them when none is named):
#   python    uv sync --all-groups --frozen -> .venv: the project, pytest, ruff, pyyaml (every check)
#   node      node + npm (Debian packages when missing), then the pinned tsc with
#             `npm ci --ignore-scripts` (the cs_script tests; CHC-TESTS and HPF-006 refuse skips)
#   gitleaks  gitleaks 8.30.1 from its GitHub release, sha256-verified (CHC-SECRETS)
#
# A component already present at the expected version is left alone. Any failure exits non-zero
# and names the step; nothing is skipped.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
CI_RULE="bootstrap"

GITLEAKS_VERSION="8.30.1"
GITLEAKS_SHA256_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_SHA256_ARM64="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"

python_env() {
    ci_need_tool uv
    (cd "$CI_REPO_ROOT" && uv sync --all-groups --frozen) || ci_die 2 "uv sync --all-groups --frozen failed"
    ci_python >/dev/null
    ci_log "bootstrap: python environment ready ($("$(ci_python)" --version))"
}

node_tsc() {
    if ! command -v npm >/dev/null 2>&1; then
        ci_need_tool apt-get
        ci_log "bootstrap: installing node and npm from the distribution"
        apt-get update -qq || ci_die 2 "apt-get update failed"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends nodejs npm >/dev/null ||
            ci_die 2 "apt-get install nodejs npm failed"
    fi
    ci_need_tool node npm
    ci_need_file "$CI_REPO_ROOT/tools/tsc/package-lock.json"
    npm ci --prefix "$CI_REPO_ROOT/tools/tsc" --ignore-scripts --no-audit --no-fund --loglevel=error ||
        ci_die 2 "npm ci for tools/tsc failed"
    [ -x "$CI_REPO_ROOT/tools/tsc/node_modules/.bin/tsc" ] ||
        ci_die 2 "npm ci did not install tools/tsc/node_modules/.bin/tsc"
    ci_log "bootstrap: node $(node --version), tsc $("$CI_REPO_ROOT/tools/tsc/node_modules/.bin/tsc" --version)"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        ci_die 2 "missing tool: sha256sum or shasum"
    fi
}

gitleaks_bin() {
    local arch expected tarball work got
    if command -v gitleaks >/dev/null 2>&1 && [ "$(gitleaks version)" = "$GITLEAKS_VERSION" ]; then
        ci_log "bootstrap: gitleaks $GITLEAKS_VERSION already installed"
        return 0
    fi
    ci_need_tool curl tar uname install
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64 | Linux-amd64) arch="x64" expected="$GITLEAKS_SHA256_X64" ;;
        Linux-aarch64 | Linux-arm64) arch="arm64" expected="$GITLEAKS_SHA256_ARM64" ;;
        *) ci_die 2 "no pinned gitleaks build for $(uname -s)-$(uname -m); install gitleaks $GITLEAKS_VERSION yourself" ;;
    esac
    tarball="gitleaks_${GITLEAKS_VERSION}_linux_${arch}.tar.gz"
    work="$(mktemp -d "${TMPDIR:-/tmp}/gitleaks.XXXXXX")"
    curl -fsSL --retry 3 -o "$work/$tarball" \
        "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/$tarball" ||
        ci_die 2 "cannot download $tarball"
    got="$(sha256_of "$work/$tarball")"
    [ "$got" = "$expected" ] || ci_die 2 "$tarball has sha256 $got, expected $expected"
    tar -xzf "$work/$tarball" -C "$work" gitleaks || ci_die 2 "cannot unpack $tarball"
    install -m 0755 "$work/gitleaks" /usr/local/bin/gitleaks || ci_die 2 "cannot install gitleaks into /usr/local/bin"
    rm -rf "$work"
    [ "$(gitleaks version)" = "$GITLEAKS_VERSION" ] || ci_die 2 "installed gitleaks reports $(gitleaks version)"
    ci_log "bootstrap: gitleaks $GITLEAKS_VERSION installed (sha256 verified)"
}

[ "$#" -gt 0 ] || set -- python node gitleaks
for component in "$@"; do
    case "$component" in
        python) python_env ;;
        node) node_tsc ;;
        gitleaks) gitleaks_bin ;;
        *) ci_die 2 "unknown component '$component' (python, node, gitleaks)" ;;
    esac
done
