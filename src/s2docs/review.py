"""Review-by-area: scope a branch diff into per-area reviewer briefs.

``s2docs review <branch>`` is a pure git client (no DB, no server): it resolves
the merge-base of ``--base`` and ``<branch>``, lists the files the branch
changed since that merge-base, partitions out files whose content already
matches the ``--base`` tip (the stale-merge-base guard — such files will merge
as no-ops), and groups the rest into named **areas** for a per-area reviewer
swarm. Output is markdown (default) or JSON (``--json``).

This mirrors the partitioning in the Hermes kanban review recipe
(``hermes_cli/kanban_review.py``) but is a standalone re-implementation — no
import of that module — so s2docs stays a self-contained package.
"""

from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


class ReviewError(Exception):
    """A user-facing failure resolving the repo, refs, or diff."""


def _git(repo: str | Path, *args: str) -> str:
    """Run ``git -C repo <args>``, returning stdout; raise ReviewError on failure.

    ``check=False`` + explicit return-code handling matches ``ingest/sources.py``.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise ReviewError(f"git {' '.join(args)} in {repo}: {detail}")
    return proc.stdout


def _rev_parse(repo: str | Path, spec: str) -> str | None:
    """Return the commit hash for ``spec`` or None when it does not resolve."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _show(repo: str | Path, ref: str, path: str) -> bytes | None:
    """Return the raw bytes of ``ref:path`` or None when the path is absent there.

    Bytes (not text) so identical content is a byte-for-byte comparison; a path
    deleted on one side yields None and is therefore never "already integrated".
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"], check=False, capture_output=True
    )
    return proc.stdout if proc.returncode == 0 else None


def resolve_repo(path: str | Path) -> Path:
    """Resolve ``path`` to a git repo's top level, raising ReviewError otherwise."""
    root = Path(path).expanduser()
    if not root.exists():
        raise ReviewError(f"repo path does not exist: {root}")
    try:
        top = _git(root, "rev-parse", "--show-toplevel").strip()
    except ReviewError as exc:
        raise ReviewError(f"not a git repository: {root}") from exc
    return Path(top or root)


def merge_base(repo: str | Path, base: str, branch: str) -> str:
    """Merge-base of ``base`` and ``branch``; a clear error if either ref is missing."""
    for ref in (base, branch):
        if _rev_parse(repo, ref) is None:
            raise ReviewError(f"unknown ref {ref!r} in {repo}")
    mb = _git(repo, "merge-base", base, branch).strip()
    if not mb:
        raise ReviewError(f"{base} and {branch} have no common ancestor")
    return mb


def diff_files(repo: str | Path, merge_base_hash: str, branch: str) -> list[str]:
    """Files the branch changed since the merge-base (``mb..branch``)."""
    out = _git(repo, "diff", "--name-only", f"{merge_base_hash}..{branch}")
    return [line.strip() for line in out.splitlines() if line.strip()]


def partition_integrated(
    repo: str | Path, base: str, branch: str, files: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split ``files`` into (needs-review, already-integrated).

    A file whose content is byte-identical at both tips will merge as a no-op —
    typically the same change that already landed on base via another branch — so
    it is out of review scope. A file deleted on either side is never identical.
    """
    review: list[str] = []
    integrated: list[str] = []
    for path in files:
        branch_bytes = _show(repo, branch, path)
        base_bytes = _show(repo, base, path)
        if branch_bytes is not None and base_bytes is not None and branch_bytes == base_bytes:
            integrated.append(path)
        else:
            review.append(path)
    return review, integrated


def parse_area_specs(pairs: Sequence[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Parse ``NAME:GLOB[,GLOB...]`` strings into explicit area specs."""
    specs: list[tuple[str, tuple[str, ...]]] = []
    for pair in pairs:
        name, sep, globs_raw = pair.partition(":")
        name = name.strip()
        globs = tuple(g.strip() for g in globs_raw.split(",") if g.strip())
        if not sep or not name or not globs:
            raise ReviewError(f"area must be NAME:GLOB[,GLOB...] — got {pair!r}")
        specs.append((name, globs))
    return specs


def auto_area(path: str) -> str:
    """Deterministic default area: first two path components, else the first."""
    parts = path.split("/")
    if len(parts) >= 3:
        return "/".join(parts[:2])
    if len(parts) == 2:
        return parts[0]
    return "root"


def plan_areas(
    files: Sequence[str], specs: Sequence[tuple[str, tuple[str, ...]]] | None = None
) -> dict[str, list[str]]:
    """Group diff files into named areas.

    Explicit ``--area`` specs are applied before the default grouping and the
    last matching override wins (later flags override earlier ones for the same
    file). Files matching no override fall back to :func:`auto_area`.
    """
    areas: dict[str, list[str]] = {}
    for path in files:
        name: str | None = None
        for spec_name, globs in specs or ():
            if any(fnmatch.fnmatch(path, g) for g in globs):
                name = spec_name  # last match wins
        areas.setdefault(name or auto_area(path), []).append(path)
    return areas


def _numstat(repo: str | Path, merge_base_hash: str, branch: str, path: str) -> tuple[int, int]:
    """(insertions, deletions) for one file; a deleted file counts 0/0."""
    if _show(repo, branch, path) is None:  # deleted on the branch
        return 0, 0
    out = _git(repo, "diff", "--numstat", f"{merge_base_hash}..{branch}", "--", path)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            ins, dels = parts[0], parts[1]
            return (int(ins) if ins.isdigit() else 0, int(dels) if dels.isdigit() else 0)
    return 0, 0


@dataclass(frozen=True)
class FileChange:
    path: str
    insertions: int
    deletions: int


@dataclass(frozen=True)
class ReviewScope:
    """What the review will cover, and what it deliberately skips."""

    repo: str
    base: str
    branch: str
    merge_base: str
    areas: dict[str, list[FileChange]] = field(default_factory=dict)
    already_integrated: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        """Total files carried by the branch diff (reviewed + integrated)."""
        return sum(len(v) for v in self.areas.values()) + len(self.already_integrated)

    def as_dict(self) -> dict:
        return {
            "branch": self.branch,
            "base": self.base,
            "merge_base": self.merge_base,
            "already_integrated": list(self.already_integrated),
            "areas": [
                {
                    "name": name,
                    "files": [
                        {"path": f.path, "insertions": f.insertions, "deletions": f.deletions}
                        for f in files
                    ],
                }
                for name, files in self.areas.items()
            ],
        }


def build_scope(
    *,
    repo: str | Path,
    base: str,
    branch: str,
    area_specs: Sequence[tuple[str, tuple[str, ...]]] | None = None,
) -> ReviewScope:
    """Resolve the repo/refs, partition the diff, and group it into areas."""
    root = resolve_repo(repo)
    mb = merge_base(root, base, branch)
    files = diff_files(root, mb, branch)
    review_files, integrated = partition_integrated(root, base, branch, files)
    grouped = plan_areas(review_files, area_specs)
    areas = {
        name: [FileChange(p, *_numstat(root, mb, branch, p)) for p in paths]
        for name, paths in grouped.items()
    }
    return ReviewScope(str(root), base, branch, mb, areas, integrated)


def render_markdown(scope: ReviewScope) -> str:
    """Human-readable per-area brief (title line, then one section per area)."""
    lines = [
        f"# Review {scope.branch} vs {scope.base} (merge-base {scope.merge_base})",
        f"{scope.changed} changed file(s), {len(scope.already_integrated)} already-integrated excluded",
    ]
    for name, files in scope.areas.items():
        lines.append("")
        lines.append(f"## {name} ({len(files)} files)")
        for f in files:
            lines.append(f"- {f.path} (+{f.insertions}/-{f.deletions})")
    return "\n".join(lines)
