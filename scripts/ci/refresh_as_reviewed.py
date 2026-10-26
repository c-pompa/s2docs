"""Rebuild ci/fixtures/as-reviewed-531a24c7.patch against today's tree.

The patch plants the pre-review (unhardened) hosts/deploys/builds code so the HPF checks have a
counterexample to reject. Its hunks were written against an older tree, so two of them no longer
matched after durable-builds rewrote builds.start() and the live work added scp_argv(pull=).

This script:
  1. resets the three source files,
  2. applies the old patch with fuzz (builds.py, deploys.py and 4 of 6 hosts.py hunks land),
  3. rewrites the remaining hosts.py block by hand to the same unhardened shape, keeping pull=,
  4. writes the result back out as a fresh patch with current context.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PATCH = ROOT / "ci/fixtures/as-reviewed-531a24c7.patch"
FILES = ["src/s2docs/hosts.py", "src/s2docs/deploys.py", "src/s2docs/builds.py"]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=check)


def main() -> int:
    run("git", "checkout", "--", *FILES)
    old = PATCH.read_text(encoding="utf-8")
    applied = subprocess.run(["patch", "-p1", "--fuzz=3"], cwd=ROOT, input=old,
                             text=True, capture_output=True)
    print("  fuzz apply:", applied.stdout.strip().splitlines()[-1] if applied.stdout else "")
    # `patch` leaves .orig/.rej beside a hunk it could not place; the hunk is redone by hand below,
    # so clear them or they end up in the tree (and once, in a commit).
    for junk in list((ROOT / "src/s2docs").glob("*.orig")) + list((ROOT / "src/s2docs").glob("*.rej*")):
        junk.unlink()

    hosts = ROOT / "src/s2docs/hosts.py"
    text = hosts.read_text(encoding="utf-8")

    # Drop the shared option builder (the old patch removed it; its hunk is one we must redo).
    start = text.find("def _connection_options(")
    if start != -1:
        end = text.find("def ssh_argv(", start)
        text = text[:start] + text[end:]

    hardened_ssh = text[text.find("def ssh_argv("):text.find("POWERSHELL = ")]
    plain_ssh = '''def ssh_argv(host: m.Host) -> list[str]:
    """Base ssh argv for a host (connection timeout + identity file when set).

    Start with this and append the remote command; the build and deploy services use it so a
    single place owns the ssh flags. The remote command is passed as one argv element — ssh
    joins the remaining arguments and hands them to the remote shell.
    """
    argv = ["ssh", "-o", "ConnectTimeout=15"]
    if host.ssh_key:
        argv += ["-i", host.ssh_key, "-o", "IdentitiesOnly=yes"]
    return argv + [host.ssh_target]


'''
    if hardened_ssh not in text:
        print("  ssh_argv block not found — aborting", file=sys.stderr)
        return 1
    text = text.replace(hardened_ssh, plain_ssh, 1)

    # scp_argv keeps the pull= the live work added: the point of the counterexample is the missing
    # validation and the missing --, not the direction flag.
    start = text.find("def scp_argv(")
    end = text.find("def cs2_install_path(", start)
    plain_scp = '''def scp_argv(host: m.Host, local: Path, remote: str, *, pull: bool = False) -> list[str]:
    """scp argv copying ``local`` to ``remote`` on the host (``pull=True``: the reverse).

    The ONLY place an scp argv is built (CI rule HPF-003).
    """
    argv = ["scp", "-o", "ConnectTimeout=15"]
    if host.ssh_key:
        argv += ["-i", host.ssh_key, "-o", "IdentitiesOnly=yes"]
    operand = f"{host.ssh_target}:{remote}"
    pair = [operand, str(local)] if pull else [str(local), operand]
    return argv + pair


'''
    if start == -1 or end == -1:
        print("  scp_argv block not found — aborting", file=sys.stderr)
        return 1
    text = text[:start] + plain_scp + text[end:]
    hosts.write_text(text, encoding="utf-8")

    diff = run("git", "diff", "--no-color", "--", *FILES).stdout
    if not diff.strip():
        print("  no diff produced — nothing to plant", file=sys.stderr)
        return 1
    PATCH.write_text(diff, encoding="utf-8")
    run("git", "checkout", "--", *FILES)
    print(f"  wrote {PATCH.relative_to(ROOT)}: {len(diff.splitlines())} lines")

    check = subprocess.run(["git", "apply", "--check", str(PATCH)], cwd=ROOT, text=True, capture_output=True)
    print("  git apply --check:", "applies cleanly" if check.returncode == 0 else check.stderr.strip())
    return 0 if check.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
