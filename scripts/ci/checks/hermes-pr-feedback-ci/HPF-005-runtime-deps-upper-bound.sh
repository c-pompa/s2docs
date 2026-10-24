#!/usr/bin/env bash
# rule: HPF-005
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/2a
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: every [project].dependencies entry is an explicit >=floor,<bound range whose upper
#   bound admits no release line past the next breaking one after the version uv.lock tests:
#   <next major for >=1.0 packages, <0.(next minor) for pre-1.0 ones (SemVer 0.y).
#
# usage: HPF-005-runtime-deps-upper-bound.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The developer guide behind catalog rule 10 spells the two shapes out: ">=floor,<next_major for
# stable packages, >=0.29,<0.32 for pre-1.0 ones". A bound of <1 on a 0.x package would let every
# future breaking 0.y release in unreviewed, so it is rejected like a bare floor.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-005 "$@"
ci_need_file "$TREE/pyproject.toml" "$TREE/uv.lock"

ci_py - <<'PY'
import tomllib

from cilib import CannotEvaluate, read_text, run_check, tree
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def next_breaking(v: Version) -> Version:
    return Version(f"{v.major + 1}") if v.major >= 1 else Version(f"0.{v.minor + 1}")


def check():
    root = tree()
    try:
        project = tomllib.loads(read_text(root / "pyproject.toml"))["project"]
        lock = tomllib.loads(read_text(root / "uv.lock"))
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise CannotEvaluate(f"cannot parse pyproject.toml / uv.lock: {exc}") from exc
    deps = project.get("dependencies") or []
    if not deps:
        raise CannotEvaluate("[project].dependencies is empty; nothing to bound")
    locked = {canonicalize_name(p["name"]): Version(p["version"]) for p in lock.get("package", []) if "version" in p}

    violations = []
    for raw in deps:
        try:
            req = Requirement(raw)
        except InvalidRequirement as exc:
            violations.append(f"{raw!r} is not a valid requirement: {exc}")
            continue
        ops = [(s.operator, Version(s.version)) for s in req.specifier]
        odd = sorted({op for op, _ in ops} - {">=", "<", "!="})
        floors = [v for op, v in ops if op == ">="]
        uppers = [v for op, v in ops if op == "<"]
        if odd:
            violations.append(f"{raw!r}: use an explicit >=floor,<bound range (found {', '.join(odd)})")
            continue
        if not floors:
            violations.append(f"{raw!r} has no >= floor")
            continue
        if not uppers:
            violations.append(f"{raw!r} has a floor but no upper bound (a bare floor adopts every future release)")
            continue
        floor, upper = max(floors), min(uppers)
        name = canonicalize_name(req.name)
        if name not in locked:
            raise CannotEvaluate(f"uv.lock has no entry for {req.name}; the lock is stale (cpompa-hermes-ci owns that)")
        tested = locked[name]
        ceiling = next_breaking(tested)
        if not floor <= tested < upper:
            violations.append(f"{raw!r}: the locked {req.name} {tested} is outside the declared range")
        elif upper > ceiling:
            shape = f"<{ceiling}" if tested.major >= 1 else f"<{ceiling} (pre-1.0: each 0.y is a breaking line)"
            violations.append(f"{raw!r}: the upper bound <{upper} admits release lines past the next breaking one "
                              f"after the tested {tested}; bound it at {shape}")
    return violations


run_check(check, ok="every runtime dependency is a >=floor,<next-breaking range around the locked version")
PY
