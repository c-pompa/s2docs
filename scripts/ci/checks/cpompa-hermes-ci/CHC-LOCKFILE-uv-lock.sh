#!/usr/bin/env bash
# rule: CHC-LOCKFILE
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: uv.lock is current for pyproject.toml (`uv lock --check`), every locked package other
#   than the project itself comes from the PyPI registry, and every locked artifact carries a
#   sha256 hash - the lock CI installs is complete, current and verifiable.
#
# usage: CHC-LOCKFILE-uv-lock.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-LOCKFILE "$@"
ci_need_file "$TREE/pyproject.toml" "$TREE/uv.lock"
ci_need_tool uv

ci_py - <<'PY'
import subprocess
import tomllib

from cilib import CannotEvaluate, read_text, run_check, tree

REGISTRY = "https://pypi.org/simple"


def check():
    root = tree()
    problems = []
    proc = subprocess.run(["uv", "lock", "--check"], cwd=root, capture_output=True, text=True)
    print(proc.stderr.strip())
    if proc.returncode == 1 and "needs to be updated" in proc.stderr:
        problems.append("uv.lock is stale for pyproject.toml; run 'uv lock' and commit the result")
    elif proc.returncode != 0:
        raise CannotEvaluate(f"uv lock --check could not decide (exit {proc.returncode})")

    try:
        lock = tomllib.loads(read_text(root / "uv.lock"))
        project = tomllib.loads(read_text(root / "pyproject.toml"))["project"]["name"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise CannotEvaluate(f"cannot parse uv.lock / pyproject.toml: {exc}") from exc
    packages = lock.get("package") or []
    if not packages:
        return [*problems, "uv.lock locks no packages"]
    for pkg in packages:
        name, source = pkg.get("name"), pkg.get("source") or {}
        if name == project and ("editable" in source or "virtual" in source):
            continue  # the project itself, installed from the checkout
        if source.get("registry") != REGISTRY:
            problems.append(f"{name} {pkg.get('version')} is locked from {source!r}, not {REGISTRY}")
            continue
        artifacts = ([pkg["sdist"]] if "sdist" in pkg else []) + list(pkg.get("wheels") or [])
        if not artifacts:
            problems.append(f"{name} {pkg.get('version')} has no locked artifact")
        for art in artifacts:
            if not str(art.get("hash", "")).startswith("sha256:"):
                problems.append(f"{name} {pkg.get('version')}: {art.get('url', '?')} has no sha256 hash")
    return problems


run_check(check, ok="uv.lock is current, PyPI-only and has a sha256 for every artifact")
PY
