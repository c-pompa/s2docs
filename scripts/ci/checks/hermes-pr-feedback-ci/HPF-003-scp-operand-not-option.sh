#!/usr/bin/env bash
# rule: HPF-003
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/1c
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: no scp process s2docs starts gets a host:path operand that starts with '-' - not for a
#   registered host, and not for an ssh_target stored before validation existed - and no scp argv
#   is built anywhere but hosts.scp_argv.
#
# usage: HPF-003-scp-operand-not-option.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# deploy_push is driven with subprocess faked, against a throwaway database. A host row written
# straight into the table stands in for a registry filled before validation existed.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-003 "$@"
ci_need_file "$TREE/src/s2docs/hosts.py" "$TREE/src/s2docs/deploys.py"

ci_py - <<'PY'
import ast
import contextlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from cilib import import_from_tree, read_text, run_check, tree

TOOL = "scp"
BUILDER = ("s2docs/hosts.py", "scp_argv")
VALID = ["pompa@192.0.2.172", "rtx-pc", "pompa@build-pc:2222"]
UNVALIDATED = ["-oProxyCommand=touch /tmp/hpf-003", "-F/dev/null"]


def destination(target):
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
    found = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        module = ast.parse(read_text(path), filename=rel)
        parents = {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(module):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and (
                    first.value == TOOL or first.value.endswith("/" + TOOL)
                ) and (rel, function_of(node, parents)) != BUILDER:
                    found.append(f"{rel}:{node.lineno} builds an {TOOL} argv in {function_of(node, parents)}(); "
                                 f"only {BUILDER[0]}::{BUILDER[1]} may")
            if isinstance(node, ast.Call) and node.args and spawns_process(node):
                arg = node.args[0]
                text = arg.value if isinstance(arg, ast.Constant) else (
                    arg.values[0].value if isinstance(arg, ast.JoinedStr) and arg.values
                    and isinstance(arg.values[0], ast.Constant) else None)
                if isinstance(text, str) and text.startswith(TOOL + " "):
                    found.append(f"{rel}:{node.lineno} runs {TOOL} through a shell string; use {BUILDER[1]}()")
    return found


class Result:
    def __init__(self, stdout=""):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


CALLS = []
REPLY = [""]


def fake_run(argv, **_kw):
    CALLS.append(list(argv))
    return Result() if argv and argv[0] == TOOL else Result(stdout=REPLY[0])




def check():
    logging.disable(logging.INFO)
    root = tree()
    violations = static_scan(root / "src")
    work = Path(tempfile.mkdtemp(prefix="hpf-003-"))
    os.environ["S2DOCS_DATA_DIR"] = str(work / "data")
    hosts = import_from_tree("s2docs.hosts")
    deploys = import_from_tree("s2docs.deploys")
    Database = import_from_tree("s2docs.db.connection").Database
    md5_file = import_from_tree("s2docs.builds").md5_file
    local = work / "payload.txt"
    local.write_bytes(b"hpf-003")
    REPLY[0] = f"{md5_file(local)}|C:/dst/payload.txt\n"

    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        for n, target in enumerate(VALID):
            db = Database(work / f"valid{n}.sqlite")
            hosts.register_host(db, "probe-host", target, shell="powershell")
            CALLS.clear()
            deploys.push(db, "probe-host", "C:/dst", [{"path": "payload.txt", "local": str(local)}])
            scp_calls = [c for c in CALLS if c and c[0] == TOOL]
            if not scp_calls:
                violations.append(f"deploy_push started no {TOOL} process for {target!r}")
            for argv in scp_calls:
                remote = argv[-1]  # the copy's destination operand, host:path
                if remote.startswith("-"):
                    violations.append(f"deploy_push to {target!r}: the scp host:path operand starts with '-': {argv!r}")
                elif remote != f"{destination(target)}:C:/dst/payload.txt":
                    violations.append(f"deploy_push to {target!r}: the scp host:path operand is {remote!r}, expected "
                                      f"'{destination(target)}:C:/dst/payload.txt' in {argv!r}")

        for n, target in enumerate(UNVALIDATED):
            db = Database(work / f"legacy{n}.sqlite")
            with db.writer() as conn, conn:
                conn.execute("INSERT INTO hosts (id, ssh_target, shell) VALUES ('legacy-pc', ?, 'powershell')",
                             (target,))
            CALLS.clear()
            # refusing the stored target is one acceptable outcome; starting scp with it is not
            with contextlib.suppress(hosts.HostError, deploys.DeployError):
                deploys.push(db, "legacy-pc", "C:/dst", [{"path": "payload.txt", "local": str(local)}])
            for argv in (c for c in CALLS if c and c[0] == TOOL):
                if argv[-1].startswith("-"):
                    violations.append(f"deploy_push for a target stored without validation ({target!r}) started scp "
                                      f"with a host:path operand that starts with '-': {argv!r}")
    finally:
        subprocess.run = real_run
    return violations


run_check(check, ok="scp's host:path operand never starts with '-', for registered and for unvalidated rows")
PY
