"""Shared plumbing for the CI checks in scripts/ci/checks/: results, exit codes, tree access.

Nothing in here knows about a specific rule — rule logic lives in the check scripts, so a rule is
added by adding a script, never by editing this module or another check.

Exit codes (see lib.sh): 0 pass, 1 violation, 2 cannot evaluate. ``run_check`` maps them: a check
function returns its violations (an empty list passes), raises ``CannotEvaluate`` for a missing or
unreadable input, and any other exception is a crash — exit 2, never 1, so a broken check can
never masquerade as a working one that found a violation.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path

EXIT_PASS, EXIT_VIOLATION, EXIT_ERROR = 0, 1, 2


class CannotEvaluate(Exception):
    """The rule could not be evaluated (missing tool, unreadable input, unreachable index...)."""


def rule_id() -> str:
    return os.environ.get("CI_RULE", "ci")


def tree() -> Path:
    try:
        return Path(os.environ["CI_TREE"]).resolve()
    except KeyError as exc:
        raise CannotEvaluate("CI_TREE is not set; run the check through its .sh entry point") from exc


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CannotEvaluate(f"unreadable input: {path} ({exc.strerror})") from exc


def import_from_tree(module: str):
    """Import ``module`` and prove it came from the tree under test, not from an installed copy."""
    root = tree() / "src"
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # an import failure in the tree is "cannot evaluate", not a violation
        raise CannotEvaluate(f"cannot import {module} from {root}: {type(exc).__name__}: {exc}") from exc
    origin = Path(getattr(mod, "__file__", "") or "").resolve()
    if root not in origin.parents:
        raise CannotEvaluate(f"{module} was imported from {origin}, not from the tree under test ({root})")
    return mod


def run_check(check: Callable[[], Iterable[str]], *, ok: str) -> None:
    """Run a check function and exit with the contract's code. Never returns."""
    rid = rule_id()
    try:
        violations = list(check())
    except CannotEvaluate as exc:
        print(f"ERROR {rid}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None
    except Exception:
        traceback.print_exc()
        print(f"ERROR {rid}: the check crashed (see traceback); this is not a verdict", file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from None
    if violations:
        for line in violations:
            print(f"FAIL {rid}: {line}")
        raise SystemExit(EXIT_VIOLATION)
    print(f"PASS {rid}: {ok}")
    raise SystemExit(EXIT_PASS)


# ----------------------------------------------------------------- shared parsing (no rule logic)
# uvx options that consume the next argument; enough to find where uvx's own options end.
UVX_VALUE_OPTIONS = frozenset({
    "--from", "--with", "--with-editable", "--with-requirements", "--python", "-p", "--exclude-newer",
    "--exclude-newer-package", "--index", "--default-index", "--index-url", "-i", "--extra-index-url",
    "--find-links", "-f", "--constraints", "-c", "--overrides", "--build-constraints", "-b", "--prerelease",
    "--resolution", "--index-strategy", "--keyring-provider", "--config-setting", "-C", "--link-mode",
    "--refresh-package", "--reinstall-package", "--upgrade-package", "-P", "--cache-dir", "--directory",
    "--project", "--config-file", "--color", "--python-preference", "--allow-insecure-host", "--env-file",
})


def uvx_option(args: list[str], name: str) -> tuple[str | None, int]:
    """Value of uvx option ``name`` in a uvx argv (``--x v`` or ``--x=v``) and the index of the command.

    Returns (value or None, command_index). Only options before the command belong to uvx; anything
    after it is passed to the tool. Raises CannotEvaluate when no command can be found.
    """
    value, i = None, 0
    while i < len(args):
        arg = args[i]
        if arg == "--" or not arg.startswith("-"):
            command = i + 1 if arg == "--" else i
            if command >= len(args):
                break
            return value, command
        key, eq, inline = arg.partition("=")
        if key == name:
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else None)
        i += 1 if eq or key not in UVX_VALUE_OPTIONS else 2
    raise CannotEvaluate(f"cannot find the command in the uvx arguments {args!r}")


def fixed_timestamp(text: str):
    """An RFC 3339 timestamp with an explicit offset, as an aware UTC datetime; ValueError otherwise.

    uv also accepts a bare date (read in the installing machine's local time zone) and relative
    spans ("3 days", "P7D"); neither names one instant for every machine, so neither is fixed.
    """
    import re
    from datetime import UTC, datetime

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", text or ""):
        kind = "a bare date (read in the local time zone)" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or "") else (
            "a relative span" if re.search(r"[a-zA-Z]", text or "") and not re.search(r"\d{4}-", text or "") else
            "not an RFC 3339 timestamp")
        raise ValueError(f"{text!r} is {kind}; use an RFC 3339 timestamp with an offset, e.g. 2026-10-10T00:00:00Z")
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def tree_files(root: Path) -> list[str]:
    """Files git would ship from ``root``'s working tree: tracked + untracked-not-ignored - deleted."""

    def git(*args: str) -> list[str]:
        proc = subprocess.run(["git", "-C", str(root), *args, "-z"], capture_output=True)
        if proc.returncode != 0:
            raise CannotEvaluate(f"git ls-files failed in {root}: {proc.stderr.decode(errors='replace').strip()}")
        return [p for p in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0") if p]

    listed = set(git("ls-files", "--cached", "--others", "--exclude-standard"))
    deleted = set(git("ls-files", "--deleted"))
    return sorted(p for p in listed - deleted if (root / p).is_file() or (root / p).is_symlink())


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "tree-files":
        try:
            files = tree_files(Path(argv[2]))
        except CannotEvaluate as exc:
            print(f"ERROR {rule_id()}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        sys.stdout.write("".join(f"{p}\0" for p in files))
        return EXIT_PASS
    print("usage: cilib.py tree-files <root>", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
