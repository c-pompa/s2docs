#!/usr/bin/env bash
# rule: CHC-PACKAGING
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: the tree builds an sdist and a wheel with uv; the wheel ships every file tracked under
#   src/s2docs/ (code and package data) and declares exactly the [project.scripts] entry points;
#   installed with the locked runtime dependencies into a fresh venv, outside the source tree,
#   every console script answers --help and the server modules import from the wheel.
#
# usage: CHC-PACKAGING-wheel.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-PACKAGING "$@"
ci_need_file "$TREE/pyproject.toml" "$TREE/uv.lock"
ci_need_tool uv

ci_py - <<'PY'
import configparser
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

from cilib import CannotEvaluate, read_text, run_check, tree, tree_files

IMPORTS = ("s2docs", "s2docs.cli", "s2docs.api.app", "s2docs.mcp_server")


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def check():
    root = tree()
    pyproject = tomllib.loads(read_text(root / "pyproject.toml"))
    scripts = pyproject.get("project", {}).get("scripts") or {}
    problems = []
    with tempfile.TemporaryDirectory(prefix="chc-packaging-") as tmp:
        tmp = Path(tmp)
        built = run(["uv", "build", "--quiet", "--sdist", "--wheel", "--out-dir", str(tmp / "dist"), str(root)])
        if built.returncode != 0:
            return [f"uv build failed (exit {built.returncode}): {built.stderr.strip()[-1200:]}"]
        wheels = sorted((tmp / "dist").glob("*.whl"))
        sdists = sorted((tmp / "dist").glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            return [f"expected one wheel and one sdist, got {[p.name for p in wheels + sdists]}"]

        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            shipped = [rel for rel in tree_files(root) if rel.startswith("src/s2docs/")
                       and "__pycache__" not in rel and not rel.endswith((".pyc", ".pyo"))]
            if not shipped:
                raise CannotEvaluate("no tracked files under src/s2docs/")
            missing = [rel for rel in shipped if rel.removeprefix("src/") not in names]
            problems += [f"the wheel does not ship {rel}" for rel in missing[:25]]
            if len(missing) > 25:
                problems.append(f"... and {len(missing) - 25} more tracked files missing from the wheel")
            entry = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
            declared = {}
            if entry:
                parser = configparser.ConfigParser()
                parser.read_string(wheel.read(entry).decode())
                declared = dict(parser["console_scripts"]) if parser.has_section("console_scripts") else {}
            if declared != scripts:
                problems.append(f"the wheel's console_scripts {declared} differ from [project.scripts] {scripts}")
        with tarfile.open(sdists[0]) as sdist:
            members = {m.name.split("/", 1)[1] for m in sdist.getmembers() if "/" in m.name}
        for needed in ("pyproject.toml", "src/s2docs/__init__.py"):
            if needed not in members:
                problems.append(f"the sdist does not contain {needed}")
        if problems:
            return problems

        exported = run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--quiet",
                        "--output-file", str(tmp / "runtime.txt")], cwd=root)
        if exported.returncode != 0:
            raise CannotEvaluate(f"uv export from uv.lock failed: {exported.stderr.strip()[-800:]}")
        venv, py = tmp / "venv", tmp / "venv" / "bin" / "python"
        install = ["uv", "pip", "install", "--quiet", "--python", str(py), "--no-deps"]
        for argv in (["uv", "venv", "--quiet", "--python", sys.executable, str(venv)],
                     [*install, "-r", str(tmp / "runtime.txt")],
                     [*install, str(wheels[0])]):
            proc = run(argv)
            if proc.returncode != 0:
                raise CannotEvaluate(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()[-800:]}")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for name in sorted(scripts):
            proc = run([str(venv / "bin" / name), "--help"], cwd=tmp, env=env)
            if proc.returncode != 0 or "usage" not in proc.stdout.lower():
                problems.append(f"the installed `{name} --help` failed (exit {proc.returncode}): "
                                f"{(proc.stderr or proc.stdout).strip()[-600:]}")
        probe = "import importlib, sys\nfor m in sys.argv[1:]:\n    print(importlib.import_module(m).__file__)"
        proc = run([str(py), "-c", probe, *IMPORTS], cwd=tmp, env=env)
        if proc.returncode != 0:
            problems.append(f"the installed wheel cannot import {IMPORTS}: {proc.stderr.strip()[-600:]}")
        else:
            outside = [f for f in proc.stdout.split() if not f.startswith(str(venv))]
            if outside:
                problems.append(f"imports resolved outside the installed wheel: {outside}")
    return problems


run_check(check, ok="sdist + wheel build, the wheel ships src/s2docs and runs from a clean venv")
PY
