#!/usr/bin/env bash
# rule: HPF-002
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/1b
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: every ssh process s2docs starts (probe_host, build_start, deploy_push/verify) has "--"
#   immediately before the destination, the destination is the registered [user@]host, and no
#   ssh argv is built anywhere but hosts.ssh_argv, so a new call site cannot skip the "--".
#
# usage: HPF-002-ssh-end-of-options.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# Static half: an AST scan of <tree>/src for ssh argv literals and ssh shell strings. Behavioural
# half: each call site runs against a throwaway database with subprocess faked, and the argv it
# would have executed is inspected. Without "--", OpenSSH reads an option-like destination (and
# re-parses the words after the host) as options: -oProxyCommand=... runs a local command.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-002 "$@"
ci_need_file "$TREE/src/s2docs/hosts.py" "$TREE/src/s2docs/builds.py" "$TREE/src/s2docs/deploys.py"

ci_py - <<'PY'
import ast
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from cilib import CannotEvaluate, import_from_tree, read_text, run_check, tree

TOOL = "ssh"
BUILDER = ("s2docs/hosts.py", "ssh_argv")
TARGETS = ["pompa@192.0.2.172", "rtx-pc", "pompa@build-pc:2222"]


def destination(target):
    """[user@]host of a [user@]host[:port] target, computed here rather than by the code under test."""
    return target.rsplit(":", 1)[0] if ":" in target else target


SPAWNERS = {("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_call"),
            ("subprocess", "check_output"), ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
            ("os", "system"), ("os", "popen")}


def spawns_process(call):
    func = call.func
    if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
        return False
    return (func.value.id, func.attr) in SPAWNERS


def function_of(node, parents):
    """Name of the function a node sits in ('<module>' at top level)."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def static_scan(src):
    """ssh argv literals outside the builder, and ssh shell strings anywhere."""
    found = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        module = ast.parse(read_text(path), filename=rel)
        parents = {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(module):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                names_tool = isinstance(first, ast.Constant) and isinstance(first.value, str) and (
                    first.value == TOOL or first.value.endswith("/" + TOOL))
                if names_tool and (rel, function_of(node, parents)) != BUILDER:
                    found.append(f"{rel}:{node.lineno} builds an {TOOL} argv in {function_of(node, parents)}(); "
                                 f"only {BUILDER[0]}::{BUILDER[1]} may (it owns the '--')")
            if isinstance(node, ast.Call) and node.args and spawns_process(node):
                arg = node.args[0]
                text = arg.value if isinstance(arg, ast.Constant) else (
                    arg.values[0].value if isinstance(arg, ast.JoinedStr) and arg.values
                    and isinstance(arg.values[0], ast.Constant) else None)
                if isinstance(text, str) and text.startswith(TOOL + " "):
                    found.append(f"{rel}:{node.lineno} runs {TOOL} through a shell string; use {BUILDER[1]}()")
    return found


class FakeProc:
    pid = 4242

    def __init__(self, argv, **_kw):
        CALLS.append(list(argv))

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    kill = terminate


class Result:
    def __init__(self, stdout=""):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


CALLS = []


def fake_run(argv, **_kw):
    CALLS.append(list(argv))
    if argv[0] == "scp":
        return Result()
    return Result(stdout=REPLY[0])


REPLY = [""]


def show(argv):
    """argv for a message, with long words (encoded PowerShell) abbreviated."""
    return "[" + ", ".join(repr(a if len(a) <= 48 else a[:40] + "...") for a in argv) + "]"


def shape(argv, target, what):
    """None when argv ends option parsing right before the destination, else the problem."""
    if not argv or argv[0] != TOOL:
        return f"{what}: expected an {TOOL} argv, got {show(argv)}"
    if "--" not in argv:
        return f"{what}: no '--' before the destination in {show(argv)}"
    at = argv.index("--")
    if at + 1 >= len(argv) or argv[at + 1] != destination(target):
        return f"{what}: the argument after '--' is not the destination {destination(target)!r}: {show(argv)}"
    if ":" in target and argv[:at][-2:] != ["-p", target.rsplit(":", 1)[1]]:
        return f"{what}: the port of {target!r} is not passed as -p before '--': {show(argv)}"
    return None


def check():
    logging.disable(logging.INFO)
    root = tree()
    violations = static_scan(root / "src")
    work = Path(tempfile.mkdtemp(prefix="hpf-002-"))
    os.environ["S2DOCS_DATA_DIR"] = str(work / "data")
    hosts = import_from_tree("s2docs.hosts")
    builds = import_from_tree("s2docs.builds")
    deploys = import_from_tree("s2docs.deploys")
    Database = import_from_tree("s2docs.db.connection").Database
    md5_file = builds.md5_file

    probe_script = work / "probe.ps1"
    probe_script.write_text("Write-Output '{}'")
    hosts.PROBE_SCRIPT = probe_script
    real_run, real_popen = subprocess.run, subprocess.Popen
    subprocess.run, subprocess.Popen = fake_run, FakeProc
    try:
        for n, target in enumerate(TARGETS):
            db = Database(work / f"db{n}.sqlite")
            # One host per call site, all with the same target: a lease the build still holds (it is
            # released after the run is marked finished) can never make the deploy scenario refuse.
            for host_id in ("probe-host", "build-host", "deploy-host"):
                hosts.register_host(db, host_id, target, shell="powershell")

            # 1. the builder itself
            argv = hosts.ssh_argv(hosts.get_host(db, "probe-host"))
            problem = shape(argv, target, "hosts.ssh_argv")
            if problem is None and argv[argv.index("--") + 1:] != [destination(target)]:
                problem = f"hosts.ssh_argv: unexpected words after the destination: {show(argv)}"
            if problem:
                violations.append(problem)

            # 2. probe_host
            CALLS.clear()
            REPLY[0] = '{"host": "PROBE"}\n'
            hosts.probe_host(db, "probe-host")
            ssh_calls = [c for c in CALLS if c and c[0] == TOOL]
            if not ssh_calls:
                violations.append(f"probe_host started no {TOOL} process for {target!r}")
            violations += [p for p in (shape(c, target, "probe_host") for c in ssh_calls) if p]

            # 3. build_start: the remote command must come after the destination
            CALLS.clear()
            run = builds.start(db, "build-host", "compile-addon")
            ssh_calls = [c for c in CALLS if c and c[0] == TOOL]
            if not ssh_calls:
                violations.append(f"build_start started no {TOOL} process for {target!r}")
            for c in ssh_calls:
                problem = shape(c, target, "build_start")
                if problem is None and c[-1] != "compile-addon":
                    problem = f"build_start: the remote command is not the last argument: {show(c)}"
                if problem:
                    violations.append(problem)
            deadline = time.monotonic() + 30
            while builds.get_run(db, run.id).status == "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            if builds.get_run(db, run.id).status == "running":
                raise CannotEvaluate(f"build run {run.id} is still running 30s after its faked ssh exited; later "
                                     "ssh calls could not be attributed to the right call site")

            # 4. deploy_push + verify: the Get-FileHash pass over ssh
            local = work / f"payload{n}.txt"
            local.write_bytes(b"hpf-002")
            REPLY[0] = f"{md5_file(local)}|C:/dst/payload.txt\n"
            CALLS.clear()
            deploys.push(db, "deploy-host", "C:/dst", [{"path": "payload.txt", "local": str(local)}])
            ssh_calls = [c for c in CALLS if c and c[0] == TOOL]
            if not ssh_calls:
                violations.append(f"deploy_push started no {TOOL} process (Get-FileHash) for {target!r}")
            violations += [p for p in (shape(c, target, "deploy_push verify") for c in ssh_calls) if p]
    finally:
        subprocess.run, subprocess.Popen = real_run, real_popen
    return violations


run_check(check, ok="every ssh call site ends option parsing with '--' right before the destination")
PY
