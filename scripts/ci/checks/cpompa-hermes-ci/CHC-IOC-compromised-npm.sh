#!/usr/bin/env bash
# rule: CHC-IOC
# pipeline: cpompa-hermes-ci
# source: house rule
# enforces: none of the compromised-npm indicators named in the workspace AGENTS.md appears in the
#   tree: the two hijacked axios releases (as a pin, a lockfile entry or an installed package), an
#   axios dependency that is not an exact known-safe version, their payload package, or the C2
#   domain and address - in shipped files or in any node_modules on disk.
#
# usage: CHC-IOC-compromised-npm.sh <tree>    exit 0 pass, 1 violation, 2 cannot evaluate
# The indicators are assembled from fragments below so this file, its fixtures and the docs never
# contain them literally (and never trip the scan themselves).
set -euo pipefail
# shellcheck source=scripts/ci/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib.sh"
ci_begin CHC-IOC "$@"

ci_py - <<'PY'
import json
import os
from pathlib import Path

from cilib import CannotEvaluate, run_check, tree, tree_files

BAD_AXIOS = {"1." + "14.1", "0." + "30.4"}
SAFE_CEILING = (1, 14, 0)
PAYLOAD = "plain-crypto" + "-js"
C2 = ("sfrclak" + ".com", ".".join(("142", "11", "206", "73")))
NEEDLES = (PAYLOAD, *C2, *(f"axios@{v}" for v in BAD_AXIOS))


def version_tuple(text):
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return None


def scan_bytes(rel, data, found):
    """Every file, text or binary: an indicator inside a bundle or an archive member counts too."""
    for needle in NEEDLES:
        at = data.find(needle.encode())
        if at >= 0:
            line = data.count(b"\n", 0, at) + 1
            found.append(f"{rel}:{line} names a compromised-npm indicator ({needle[:4]}...)")


def scan_manifest(rel, data, found):
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "overrides"):
        spec = (data.get(field) or {}).get("axios") if isinstance(data.get(field), dict) else None
        if spec is None:
            continue
        exact = version_tuple(str(spec))
        if exact is None:
            found.append(f"{rel}: axios {spec!r} in {field} is a range; pin an exact known-safe version")
        elif str(spec) in BAD_AXIOS or exact > SAFE_CEILING:
            found.append(f"{rel}: axios {spec} in {field} is not a known-safe release")
    for key, entry in (data.get("packages") or {}).items():  # package-lock.json v2/v3
        if not isinstance(entry, dict):
            continue
        if key.endswith("node_modules/" + PAYLOAD):
            found.append(f"{rel}: lockfile installs {PAYLOAD[:5]}... ({key})")
        if key.endswith("node_modules/axios") and entry.get("version") in BAD_AXIOS:
            found.append(f"{rel}: lockfile pins a compromised axios release ({entry.get('version')})")


def check():
    root = tree()
    found = []
    files = tree_files(root)
    for rel in files:
        path = root / rel
        if path.is_symlink():  # scan the link itself; never follow it out of the tree
            scan_bytes(rel, os.readlink(path).encode(), found)
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CannotEvaluate(f"unreadable input: {rel} ({exc.strerror})") from exc
        scan_bytes(rel, data, found)
        if path.name in ("package.json", "package-lock.json", "npm-shrinkwrap.json"):
            try:
                scan_manifest(rel, json.loads(data), found)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise CannotEvaluate(f"{rel} is not valid JSON: {exc}") from exc

    installed = 0
    for dirpath, dirnames, _files in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        if Path(dirpath).name != "node_modules":
            continue
        base = Path(dirpath)
        if (base / PAYLOAD).exists():
            found.append(f"{base.relative_to(root)}/{PAYLOAD[:5]}... is installed")
        manifest = base / "axios" / "package.json"
        if manifest.is_file():
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
            if version in BAD_AXIOS:
                found.append(f"{manifest.relative_to(root)}: a compromised axios release ({version}) is installed")
        installed += 1
    print(f"  CHC-IOC: scanned {len(files)} shipped files and {installed} node_modules directories")
    return found


run_check(check, ok="no compromised-npm indicator in shipped files or installed node_modules")
PY
