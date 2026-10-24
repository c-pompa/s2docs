"""Anchored edits for the falsification fixtures (sourced through scripts/ci/plant.sh).

    plant.py replace FILE OLD NEW     the one occurrence of OLD in FILE becomes NEW
    plant.py sub FILE REGEX REPL      re.sub (re.MULTILINE) that must match exactly once
    plant.py write FILE               stdin becomes FILE (parent directories created)
    plant.py append FILE              stdin is appended to FILE
    plant.py fake-token github-pat    a well-formed, deterministic, fake GitHub token
    plant.py ioc NAME                 a compromised-npm indicator: axios-hijacked, axios-hijacked-legacy,
                                      payload, c2-domain, c2-address

Exit 0 on success; 3 when an anchor is missing or matches more than once (the fixture is stale and
must be updated, see docs/FEEDBACK_CI.md); 2 on usage errors.

Secrets and indicators are assembled at run time from fragments, so no file in this repository
contains one literally and the secret/indicator scans never flag their own fixtures.
"""

from __future__ import annotations

import random
import re
import string
import sys
from pathlib import Path

STALE, USAGE = 3, 2

IOC = {
    "axios-hijacked": "1." + "14.1",
    "axios-hijacked-legacy": "0." + "30.4",
    "payload": "plain-crypto" + "-js",
    "c2-domain": "sfrclak" + ".com",
    "c2-address": ".".join(("142", "11", "206", "73")),
}


def stale(message: str) -> int:
    print(f"plant: {message}; the fixture no longer matches the code - update it (docs/FEEDBACK_CI.md)",
          file=sys.stderr)
    return STALE


def read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"plant: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return None


def replace(path: str, old: str, new: str) -> int:
    text = read(path)
    if text is None:
        return STALE
    count = text.count(old)
    if count != 1:
        return stale(f"expected exactly one occurrence of {old!r} in {path}, found {count}")
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")
    return 0


def sub(path: str, pattern: str, repl: str) -> int:
    text = read(path)
    if text is None:
        return STALE
    count = len(re.findall(pattern, text, flags=re.MULTILINE))
    if count != 1:
        return stale(f"expected exactly one match of /{pattern}/ in {path}, found {count}")
    Path(path).write_text(re.sub(pattern, repl, text, count=1, flags=re.MULTILINE), encoding="utf-8")
    return 0


def write(path: str, *, append: bool = False) -> int:
    """Bytes from stdin, verbatim: fixtures also plant binary (non-UTF-8) files."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab" if append else "wb") as fh:
        fh.write(sys.stdin.buffer.read())
    return 0


def fake_token(kind: str) -> int:
    if kind != "github-pat":
        print(f"plant: unknown token kind {kind!r}", file=sys.stderr)
        return USAGE
    rng = random.Random(143393)  # deterministic: the same fake token on every run
    body = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))
    print("gh" + "p_" + body)
    return 0


def main(argv: list[str]) -> int:
    match argv[1:]:
        case ["replace", path, old, new]:
            return replace(path, old, new)
        case ["sub", path, pattern, repl]:
            return sub(path, pattern, repl)
        case ["write", path]:
            return write(path)
        case ["append", path]:
            return write(path, append=True)
        case ["fake-token", kind]:
            return fake_token(kind)
        case ["ioc", name] if name in IOC:
            print(IOC[name])
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return USAGE


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
