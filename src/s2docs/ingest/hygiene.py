"""Read-only index staleness report: does the index still describe the sources it came from?

`s2docs sync` / `s2docs load` rewrite the index offline and print a row count, and then nothing in
the index records *how old* its answers are. A checkout that moved on (or a hand-edited steam.inf)
leaves every convar / schema answer quietly describing a build that no longer exists, and the reader
has no way to tell. This module answers that question from what ingestion actually wrote down:

- ``builds.source_commit`` — the git HEAD the build was loaded from (``ingest.sources.source_commit``);
- ``builds.ingested_at`` — when the rows were written;
- ``builds.is_current`` — which build the readers resolve to.

against the checkout under ``<data_dir>/sources/<game>``, the path ``sync`` uses. Two independent
signals are compared, because each misses what the other catches: a moved HEAD (a normal re-fetch)
and a source file newer than the ingest (a tree edited in place, where HEAD never moves).

Everything here is a SELECT plus ``stat`` / ``git rev-parse`` on the checkout: the caller opens the
database with ``Database.reader()`` (``file:...?mode=ro``) and this module never writes.

A build indexed with ``load`` from an arbitrary path cannot be located again — nothing records the
root — so that case reports ``sources missing`` and names the path it looked at, rather than
pretending to have verified a tree it cannot see.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from s2docs.config import GAMES, Game, get_settings
from s2docs.ingest.sources import source_commit

# The verdict vocabulary. `current` is the only value that lets `sync --check --fail-if-stale` pass,
# so every uncertain case below resolves to one of the others.
CURRENT = "current"
STALE = "stale"
NEVER_SYNCED = "never synced"
SOURCES_MISSING = "sources missing"

# What the loader keys a build off: `steam.inf` carries the build number that stamps every row, and the
# DumpSource2 files carry the bulk of the content. These are the in-place-edit signal; the commit is the
# re-fetch signal. Directories are deliberately not walked — a checkout is ~25 MB per game and a
# directory mtime says nothing about whether the files inside changed.
_SOURCE_FILES = (
    "game/{mod_dir}/steam.inf",
    "DumpSource2/convars.txt",
    "DumpSource2/commands.txt",
    "DumpSource2/entities/server.fgd",
    "DumpSource2/entities/client.fgd",
)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # builds.ingested_at: strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), UTC
_SHORT_SHA = 8


@dataclass(frozen=True, slots=True)
class GameFreshness:
    """One game's answer: what the index records, what the checkout says, and the verdict between them."""

    game: str
    verdict: str
    detail: str
    build_number: int | None = None
    patch_version: str | None = None
    ingested_at: str | None = None
    indexed_commit: str | None = None
    checkout_commit: str | None = None
    sources: Path | None = None
    newer_files: tuple[str, ...] = ()

    @property
    def is_current(self) -> bool:
        return self.verdict == CURRENT

    def as_dict(self) -> dict[str, object]:
        return {
            "game": self.game,
            "build": self.build_number,
            "patch_version": self.patch_version,
            "ingested_at": self.ingested_at,
            "indexed_commit": self.indexed_commit,
            "checkout_commit": self.checkout_commit,
            "sources": str(self.sources) if self.sources is not None else None,
            "newer_files": list(self.newer_files),
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IndexReport:
    """Every reported game's freshness, plus the aggregate a CI step branches on."""

    games: list[GameFreshness] = field(default_factory=list)

    @property
    def all_current(self) -> bool:
        return all(g.is_current for g in self.games)

    @property
    def stale_games(self) -> list[GameFreshness]:
        return [g for g in self.games if not g.is_current]

    def as_dict(self) -> dict[str, object]:
        return {
            "games": [g.as_dict() for g in self.games],
            "stale": [g.game for g in self.stale_games],
            "all_current": self.all_current,
        }


def _recorded_builds(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The current build row of every game the index knows, keyed by game id.

    A database file that exists but was never migrated has no ``games`` table at all; that is the
    "never synced" state, not an error, so it reports as no recorded builds. Any other
    OperationalError (a corrupt file, a locked page) is a real failure and propagates.
    """
    try:
        rows = conn.execute(
            "SELECT g.id AS game_id, b.build_number, b.patch_version, b.source_commit, b.ingested_at "
            "FROM games g LEFT JOIN builds b ON b.game_id = g.id AND b.is_current = 1"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {}
    return {r["game_id"]: r for r in rows}


def _epoch(stamp: str | None) -> int | None:
    """``ingested_at`` as whole epoch seconds, or None when it is missing/unparsable.

    Whole seconds on purpose: the column has one-second resolution, so a file written in the same
    second as the ingest must not read as "newer" — only a file written after that second does.
    """
    if not stamp:
        return None
    try:
        return int(datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC).timestamp())
    except ValueError:
        return None


def _source_files(game: Game, root: Path) -> list[Path]:
    """The checkout files this game's build was keyed off, in declaration order (missing ones dropped)."""
    return [p for p in (root / rel.format(mod_dir=game.mod_dir) for rel in _SOURCE_FILES) if p.is_file()]


def _newer_than(paths: list[Path], root: Path, stamp_epoch: int) -> tuple[str, ...]:
    """Relative paths of source files modified after the index was written."""
    newer = []
    for path in paths:
        try:
            modified = int(path.stat().st_mtime)
        except OSError:  # vanished between the listing and the stat: nothing left to compare
            continue
        if modified > stamp_epoch:
            newer.append(path.relative_to(root).as_posix())
    return tuple(newer)


def _judge(game: Game, row: sqlite3.Row | None, sources: Path) -> GameFreshness:
    """Decide one game's verdict from its recorded build row and the checkout on disk."""
    if row is None or row["build_number"] is None:
        return GameFreshness(
            game=game.id,
            verdict=NEVER_SYNCED,
            detail=f"no build recorded — run `s2docs sync --game {game.id}`",
            sources=sources,
        )

    recorded = GameFreshness(
        game=game.id,
        verdict=CURRENT,
        detail="",
        build_number=row["build_number"],
        patch_version=row["patch_version"],
        ingested_at=row["ingested_at"],
        indexed_commit=row["source_commit"],
        sources=sources,
    )

    key_file = sources / "game" / game.mod_dir / "steam.inf"
    if not sources.is_dir() or not key_file.is_file():
        return replace(
            recorded,
            verdict=SOURCES_MISSING,
            detail=f"indexed build {recorded.build_number} but {key_file} is gone — the answers cannot be "
            "verified against a checkout that is not there",
        )

    checkout = source_commit(sources)
    stale_reasons: list[str] = []
    newer: tuple[str, ...] = ()

    if recorded.indexed_commit and checkout and recorded.indexed_commit != checkout:
        stale_reasons.append(
            f"checkout moved on: indexed {recorded.indexed_commit[:_SHORT_SHA]}, HEAD now {checkout[:_SHORT_SHA]}"
        )

    stamp = _epoch(recorded.ingested_at)
    if stamp is not None:
        newer = _newer_than(_source_files(game, sources), sources, stamp)
        if newer:
            shown = ", ".join(newer[:3]) + (f" (+{len(newer) - 3} more)" if len(newer) > 3 else "")
            stale_reasons.append(f"{len(newer)} source file(s) newer than the index: {shown}")

    if stale_reasons:
        return replace(
            recorded, verdict=STALE, checkout_commit=checkout, newer_files=newer, detail="; ".join(stale_reasons)
        )

    if checkout is None and recorded.indexed_commit is None and stamp is None:
        # Nothing comparable at all: an index that cannot be dated must not pass for current.
        return replace(
            recorded,
            verdict=STALE,
            detail="cannot date the index (no commit, unparsable ingested_at) — re-run `s2docs sync --no-fetch`",
        )

    matched = f"checkout at {checkout[:_SHORT_SHA]}" if checkout else "source files unchanged"
    return replace(recorded, checkout_commit=checkout, detail=f"{matched} since {recorded.ingested_at}")


def check_index(
    conn: sqlite3.Connection,
    *,
    sources_dir: Path | None = None,
    games: list[Game] | None = None,
) -> IndexReport:
    """Report, per game, whether the index still matches the sources it was built from. Never writes.

    Reads only (SELECT + ``stat`` + ``git rev-parse``), so it is safe on a read-only connection and
    safe to run against the live store while a server is serving it. A game the index has never heard
    of is reported as ``never synced`` rather than skipped: "you never synced deadlock" is exactly the
    state a caller needs to see before trusting an answer about it.
    """
    root = sources_dir if sources_dir is not None else get_settings().sources_dir
    recorded = _recorded_builds(conn)
    known = {g.id: g for g in games} if games is not None else dict(GAMES)
    # A row for a game this build no longer configures still gets reported: its answers are in the
    # index, and dropping it would hide a stale store behind a config change.
    for game_id in recorded:
        if game_id not in known:
            known[game_id] = Game(id=game_id, name=game_id, app_id=0, mod_dir="", repo="")
    return IndexReport(games=[_judge(known[gid], recorded.get(gid), root / gid) for gid in sorted(known)])
