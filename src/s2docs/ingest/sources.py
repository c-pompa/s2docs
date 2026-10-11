"""Fetch GameTracking repositories with a shallow, blob-filtered sparse checkout.

Only the files the ingesters read are materialised (~25 MB per game instead of
the full multi-GB repo).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from s2docs.config import Game

log = logging.getLogger(__name__)


class SourceSyncError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = ["git", *args]
    log.debug("running %s", " ".join(cmd))
    proc = subprocess.run(cmd, check=False, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SourceSyncError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def sync_source(game: Game, sources_dir: Path) -> Path:
    """Clone or update the GameTracking checkout for `game`. Returns its path."""
    dest = sources_dir / game.id
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s -> %s", game.repo, dest)
        _git(
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-checkout",
            game.repo,
            str(dest),
        )
        _git("sparse-checkout", "set", "--no-cone", *game.sparse_patterns, cwd=dest)
        _git("checkout", cwd=dest)
    else:
        log.info("updating %s", dest)
        _git("sparse-checkout", "set", "--no-cone", *game.sparse_patterns, cwd=dest)
        _git("fetch", "--depth", "1", "origin", "HEAD", cwd=dest)
        _git("reset", "--hard", "FETCH_HEAD", cwd=dest)
    return dest


def source_commit(path: Path) -> str | None:
    try:
        return _git("rev-parse", "HEAD", cwd=path)
    except (SourceSyncError, FileNotFoundError):
        return None
