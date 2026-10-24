#!/usr/bin/env bash
# rule: HPF-006
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/2b
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: the runtime floors are the oldest compatible releases, proven at the project's fixed
#   [tool.uv] exclude-newer snapshot: under --resolution lowest-direct every floor resolves to
#   itself and the full suite passes with nothing skipped, and for every dependency the release
#   just below its floor either cannot resolve with the other floors or fails the suite.
#
# usage: HPF-006-runtime-deps-oldest-floor.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# Needs uv and the PyPI JSON API (release list + upload times). Each environment is a throwaway
# venv; the tree's own uv.lock is never touched. "Oldest" is a local minimum: lowering any single
# floor by one release must break resolution or the suite.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-006 "$@"
ci_need_file "$TREE/pyproject.toml"
ci_need_dir "$TREE/tests" "$TREE/src"
ci_need_tool uv

ci_py - <<'PY'
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from cilib import CannotEvaluate, fixed_timestamp, read_text, run_check, tree
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


def say(msg):
    print(f"  HPF-006: {msg}", flush=True)


def snapshot(pyproject):
    raw = (pyproject.get("tool", {}).get("uv", {}) or {}).get("exclude-newer")
    if not raw:
        raise CannotEvaluate("pyproject.toml has no [tool.uv] exclude-newer: floors cannot be proven against a fixed "
                             "release snapshot (setting it is HPF-009's rule)")
    try:
        return str(raw), fixed_timestamp(str(raw))
    except ValueError as exc:
        raise CannotEvaluate(f"[tool.uv] exclude-newer {exc} (HPF-009's rule)") from exc


def releases_before(name, floor, cutoff):
    """Final releases of `name` older than `floor` that had a non-yanked file on the index by `cutoff`."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CannotEvaluate(f"cannot read the release list from {url}: {exc}") from exc
    out = []
    for ver, files in data.get("releases", {}).items():
        try:
            v = Version(ver)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease or v >= floor:
            continue
        for f in files:
            uploaded = datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00"))
            if not f.get("yanked") and uploaded <= cutoff:
                out.append(v)
                break
    return sorted(out)


def compile_(pyproject_text, stamp, work):
    """('ok', pins, text) or ('unresolvable', reason, '')."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    proc = subprocess.run(
        ["uv", "pip", "compile", str(work / "pyproject.toml"), "--group", "dev", "--resolution", "lowest-direct",
         "--exclude-newer", stamp, "--python-version", PY_VERSION, "--no-header", "--no-annotate", "-q"],
        cwd=work, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        if "No solution found" in proc.stderr:
            lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
            return "unresolvable", " ".join(lines[1:4])[:400], ""
        raise CannotEvaluate(f"uv pip compile failed (rc={proc.returncode}): {proc.stderr.strip()[-800:]}")
    pins = {canonicalize_name(n): v for n, v in (ln.split("==", 1) for ln in proc.stdout.splitlines() if "==" in ln)}
    return "ok", pins, proc.stdout


def suite(requirements, work):
    """(passed, summary). Installs exactly `requirements` into a fresh venv and runs the tree's suite.

    A failure is re-run once (pytest --lf): the suite is the oracle for "these versions work", so a
    test that fails and then passes is reported as flaky instead of deciding the verdict.
    """
    venv = work / "venv"
    (work / "requirements.txt").write_text(requirements, encoding="utf-8")
    for argv in (["uv", "venv", "-q", "--python", sys.executable, str(venv)],
                 ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), "--no-deps",
                  "-r", str(work / "requirements.txt")]):
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CannotEvaluate(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()[-800:]}")
    env = {**os.environ, "PYTHONPATH": str(tree() / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    base = [str(venv / "bin" / "python"), "-m", "pytest", "-q", "-rs", "-o", f"cache_dir={work / 'pytest-cache'}"]

    def run(extra, report):
        proc = subprocess.run(base + [f"--junitxml={report}", *extra], cwd=tree(), env=env, capture_output=True,
                              text=True)
        if proc.returncode not in (0, 1, 2) or not report.is_file():
            raise CannotEvaluate(f"pytest did not run (rc={proc.returncode}): {(proc.stdout + proc.stderr)[-800:]}")
        root = ET.parse(report).getroot()
        root = root if root.tag == "testsuite" else root.find("testsuite")
        counts = {k: int(root.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
        failed = [f"{c.get('classname')}::{c.get('name')}" for c in root.iter("testcase")
                  if c.find("failure") is not None or c.find("error") is not None]
        tail = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
        return proc, counts, failed, tail

    proc, counts, failed, tail = run([], work / "junit.xml")
    if proc.returncode == 0:
        if counts["skipped"]:
            skips = [ln for ln in proc.stdout.splitlines() if ln.startswith("SKIPPED")][:5]
            raise CannotEvaluate(f"{counts['skipped']} tests were skipped, so the suite did not fully run: {skips}")
        return True, tail
    again, _, still, _ = run(["--lf"], work / "junit-rerun.xml")
    if again.returncode == 0:
        say(f"flaky, passed on a re-run: {', '.join(failed[:5])}")
        return True, f"{tail} -> re-run of the failures passed (flaky)"
    return False, f"{tail} | reproducible failures: {', '.join((still or failed)[:3])}"


def check():
    root = tree()
    text = read_text(root / "pyproject.toml")
    pyproject = tomllib.loads(text)
    stamp, cutoff = snapshot(pyproject)
    say(f"release snapshot {stamp} (python {PY_VERSION})")
    reqs = []
    for raw in pyproject.get("project", {}).get("dependencies") or []:
        req = Requirement(raw)
        floors = [Version(s.version) for s in req.specifier if s.operator == ">="]
        if not floors:
            raise CannotEvaluate(f"{raw!r} has no >= floor to verify (the requirement shape is HPF-005's rule)")
        reqs.append((raw, req, max(floors)))
    if not reqs:
        raise CannotEvaluate("[project].dependencies is empty")
    if any(text.count(f'"{raw}"') != 1 for raw, _, _ in reqs):
        raise CannotEvaluate("each runtime requirement must appear exactly once, double-quoted, in pyproject.toml")

    violations = []
    with tempfile.TemporaryDirectory(prefix="hpf-006-") as tmp:
        tmp = Path(tmp)
        state, pins, requirements = compile_(text, stamp, tmp / "floors")
        if state == "unresolvable":
            return [f"the declared floors do not resolve together at {stamp}: {pins}"]
        for raw, req, floor in reqs:
            got = pins.get(canonicalize_name(req.name))
            if got is None or Version(got) != floor:
                violations.append(f"{raw!r}: under lowest-direct {req.name} resolves to {got}, not the declared floor "
                                  f"{floor} - that floor cannot be installed alongside the others, so it is untested")
        if violations:
            return violations
        say("floors resolve to themselves: " + ", ".join(f"{req.name}=={floor}" for _, req, floor in reqs))
        passed, summary = suite(requirements, tmp / "floors")
        if not passed:
            return [f"the suite fails at the declared floors: {summary}"]
        say(f"suite at the floors: {summary}")

        for n, (raw, req, floor) in enumerate(reqs):
            older = releases_before(req.name, floor, cutoff)
            if not older:
                say(f"{req.name}: {floor} is the oldest release on the index at {stamp}")
                continue
            prior = older[-1]
            extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
            marker = f"; {req.marker}" if req.marker else ""
            trial = text.replace(f'"{raw}"', f'"{req.name}{extras}=={prior}{marker}"', 1)
            state, detail, requirements = compile_(trial, stamp, tmp / f"prior-{n}")
            if state == "unresolvable":
                say(f"{req.name}: {prior} (the release before {floor}) cannot resolve with the other floors: {detail}")
                continue
            passed, summary = suite(requirements, tmp / f"prior-{n}")
            if passed:
                violations.append(f"{raw!r}: {req.name} {prior}, the release before the floor {floor}, resolves "
                                  f"with the other floors and passes the suite ({summary}) - lower the floor to the "
                                  "oldest compatible release")
            else:
                say(f"{req.name}: {prior} (the release before {floor}) fails the suite: {summary}")
    return violations


run_check(check, ok="every runtime floor is the oldest release that resolves and passes the suite at the snapshot")
PY
