"""Print the markdown delta an upstream PR comment pastes verbatim.

    pr_delta.py --from-sha A --to-sha B [--root DIR]

Every value is read, never typed: the runtime dependency ranges from pyproject.toml
[project].dependencies (exactly as written there), the uvx `--from git+...@<sha>` ref and the
`--exclude-newer` instant from plugin/mcp.json, `version` from plugin/plugin.json, and the commits of
A..B from `git log --oneline`. The three files must be the ones at B in the working tree (no local
edits against B), so the delta describes the commit it names. See docs/FEEDBACK_CI.md.

Exit codes: 0 delta printed; 2 an input is missing, unreadable or not what the script expects (the
message names it). Nothing is printed to stdout unless every value was read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FILES = ("pyproject.toml", "plugin/mcp.json", "plugin/plugin.json")
SHA_RE = re.compile(r"[0-9a-f]{40}")


class DeltaError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeltaError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError as exc:
        raise DeltaError(f"unreadable input: {rel}: {exc}") from exc


def dependencies(root: Path) -> list[str]:
    """[project].dependencies, each entry exactly as it is written in pyproject.toml."""
    raw = _read(root, "pyproject.toml")
    try:
        deps = tomllib.loads(raw)["project"]["dependencies"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise DeltaError(f"pyproject.toml has no readable [project].dependencies: {exc}") from exc
    if not isinstance(deps, list) or not deps or not all(isinstance(d, str) for d in deps):
        raise DeltaError("pyproject.toml [project].dependencies is not a non-empty list of strings")
    for dep in deps:
        # the parsed value must also be the literal text, so an escape can never alter what we echo
        if f'"{dep}"' not in raw and f"'{dep}'" not in raw:
            raise DeltaError(f"dependency {dep!r} is not written literally in pyproject.toml")
    return deps


def launch(root: Path) -> dict[str, str]:
    """The uvx --from ref (and its sha) and the --exclude-newer value from plugin/mcp.json."""
    try:
        servers = json.loads(_read(root, "plugin/mcp.json"))["mcpServers"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DeltaError(f"plugin/mcp.json has no readable mcpServers: {exc}") from exc
    found = []
    for name, server in servers.items():
        args = server.get("args", []) if isinstance(server, dict) else []
        if "--from" in args:
            found.append((name, args))
    if len(found) != 1:
        raise DeltaError(f"plugin/mcp.json: expected exactly one server launched with --from, found {len(found)}")
    name, args = found[0]

    def value_of(flag: str) -> str:
        if args.count(flag) != 1 or args.index(flag) + 1 >= len(args):
            raise DeltaError(f"plugin/mcp.json server {name!r}: {flag} must appear once, followed by a value")
        return str(args[args.index(flag) + 1])

    ref = value_of("--from")
    if not ref.startswith("git+") or "@" not in ref:
        raise DeltaError(f"plugin/mcp.json: --from {ref!r} is not a git+<url>@<ref>")
    sha = ref.rsplit("@", 1)[1]
    if not SHA_RE.fullmatch(sha):
        raise DeltaError(f"plugin/mcp.json: --from pins {sha!r}, not a full 40-hex commit sha")
    return {"server": name, "from": ref, "sha": sha, "exclude_newer": value_of("--exclude-newer")}


def version(root: Path) -> str:
    try:
        value = json.loads(_read(root, "plugin/plugin.json"))["version"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DeltaError(f"plugin/plugin.json has no readable version: {exc}") from exc
    if not isinstance(value, str) or not value:
        raise DeltaError("plugin/plugin.json version is not a non-empty string")
    return value


def commit_range(root: Path, from_sha: str, to_sha: str) -> tuple[str, str, str]:
    full = []
    for ref in (from_sha, to_sha):
        try:
            full.append(_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip())
        except DeltaError as exc:
            raise DeltaError(f"{ref} is not a commit in {root}") from exc
    log = _git(root, "log", "--oneline", f"{full[0]}..{full[1]}")
    if not log.strip():
        raise DeltaError(f"{from_sha}..{to_sha} contains no commits")
    dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", full[1], "--", *FILES])
    if dirty.returncode != 0:
        raise DeltaError(f"{', '.join(FILES)} in the working tree differ from {to_sha}; check out {to_sha} first")
    return full[0], full[1], log


def render(root: Path, from_sha: str, to_sha: str) -> str:
    a, b, log = commit_range(root, from_sha, to_sha)
    deps, run, ver = dependencies(root), launch(root), version(root)
    lines = [
        f"### Changes since `{a[:12]}`",
        "",
        f"Commits `{a}..{b}` (`git log --oneline`):",
        "",
        "```",
        log.rstrip("\n"),
        "```",
        "",
        "Runtime dependencies (`pyproject.toml` `[project].dependencies`, verbatim):",
        "",
        *(f"- `{dep}`" for dep in deps),
        "",
        f"Plugin launch (`plugin/mcp.json`, server `{run['server']}`):",
        "",
        f"- `--from {run['from']}`",
        f"- pinned commit: `{run['sha']}`",
        f"- `--exclude-newer {run['exclude_newer']}`",
        "",
        f"Plugin version (`plugin/plugin.json`): `{ver}`",
        "",
        f"<sub>Generated by `scripts/ci/pr_delta.py --from-sha {from_sha} --to-sha {to_sha}` at `{b}`.</sub>",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the markdown delta an upstream PR comment pastes verbatim.")
    parser.add_argument("--from-sha", required=True, help="the commit the reviewer read")
    parser.add_argument("--to-sha", required=True, help="the commit the delta describes")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    args = parser.parse_args(argv)
    try:
        out = render(args.root.resolve(), args.from_sha, args.to_sha)
    except DeltaError as exc:
        print(f"pr_delta: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
