"""Index hygiene: does the index still describe the build it claims, and is a fresh schema clean?

Two silent failure modes are pinned here.

`sync` / `load` rewrite the index offline and report a row count, so nothing afterwards says whether
the rows a reader is querying predate the checkout on disk — a convar or schema answer can be quietly
stale. `sync --check` reports that from what ingestion recorded (`builds.source_commit`,
`builds.ingested_at`) and `--fail-if-stale` turns it into an exit a cron step can branch on. The check
must never write: it runs against the store a live server is serving, so "no writes" is asserted on the
database file's bytes and mtime, not just promised in a docstring.

SQLite cannot alter a column in place, so several migrations rebuild a table through a scratch name
(`_new`, `_stash`). A migration that forgets its final DROP leaves a stale copy of the data sitting
beside the real table, and nothing checked the one path every deployment walks: a brand-new database.
No migration needed changing when this test was written — the fresh path is clean — but the old-shaped
path is covered too, because the safe way to "consolidate" a scratch step later is to keep the v12 → cur
rebuild of `jobs` provably lossless.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from s2docs import cli
from s2docs.config import GAMES, get_settings
from s2docs.db.connection import Database, _migrations, connect, migrate, schema_version
from s2docs.ingest.loader import load_build
from tests.conftest import write_tree

# Any table a migration builds as an intermediate and must not leave behind.
_SCRATCH_TABLE = re.compile(r"_(new|stash)$")


# ------------------------------------------------------------------- fixtures


def _store() -> Database:
    """The database `get_settings()` resolves for this test (conftest pins S2DOCS_DATA_DIR at tmp)."""
    return Database(get_settings().db_path)


def _checkout(game_id: str) -> Path:
    """The checkout path `sync` uses for `game_id` — the only root the check can look at."""
    return get_settings().sources_dir / game_id


def _load(game_id: str, root: Path) -> Database:
    """Index one game from `root` into this test's store."""
    db = _store()
    with db.writer() as conn:
        load_build(conn, GAMES[game_id], root)
    return db


def _snapshot(path: Path) -> tuple[int, int, str, list[str]]:
    """(mtime_ns, size, sha256, dir listing) — everything a write would disturb."""
    stat = path.stat()
    return (
        stat.st_mtime_ns,
        stat.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        sorted(p.name for p in path.parent.iterdir()),
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


# ------------------------------------------------------------------- sync --check


def test_check_on_a_never_synced_database_reports_never_synced(capsys):
    """A store that exists (migrations ran) but was never loaded reports every game as never synced."""
    with _store().writer():  # migrate only: the literal "never synced" state
        pass

    assert cli.main(["sync", "--check"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    for game_id in GAMES:
        assert game_id in printed
    assert printed.count("never synced") == len(GAMES)

    assert cli.main(["sync", "--check", "--fail-if-stale"]) == cli.EXIT_PARTIAL


def test_check_on_a_missing_database_fails_loudly():
    """No file at all is a misconfiguration, not a stale index: name the path instead of guessing."""
    assert not get_settings().db_path.exists()
    assert cli.main(["sync", "--check", "--fail-if-stale"]) == cli.EXIT_ERROR


def test_check_performs_no_writes(tmp_path, capsys):
    """Read-only means the database file is byte-identical afterwards — no row, no page, no checkpoint."""
    db = _load("cs2", write_tree(_checkout("cs2")))
    before = _snapshot(db.path)

    assert cli.main(["sync", "--check", "--game", "cs2"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "current" in printed, "the check reported current, so it did read the index it then left alone"

    after = _snapshot(db.path)
    assert after[:3] == before[:3], "the database file itself must be byte-identical (mtime, size, sha256)"
    # With no writer holding the log, SQLite hands a reader -wal/-shm sidecars: that is its read
    # machinery, not a write to the store. Anything else appearing (a journal, a temp table) is a write.
    assert set(after[3]) - set(before[3]) <= {f"{db.path.name}-wal", f"{db.path.name}-shm"}


def test_check_reports_current_for_a_recorded_build(tmp_path, capsys):
    """A build loaded from the checkout the index points at is current, and gates clean."""
    _load("cs2", write_tree(_checkout("cs2")))

    code = cli.main(["--json", "sync", "--check", "--game", "cs2", "--fail-if-stale"])
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    (cs2,) = payload["games"]
    assert cs2["verdict"] == "current"
    assert cs2["build"] == 1000
    assert cs2["patch_version"] == "1.0.1000"
    assert payload["all_current"] is True
    assert payload["stale"] == []


def test_check_reports_stale_when_a_source_file_is_newer(tmp_path, capsys):
    """An in-place edit (a hand-unpacked dump, an edited steam.inf) never moves HEAD: the mtime signal catches it."""
    _load("cs2", write_tree(_checkout("cs2")))
    steam_inf = _checkout("cs2") / "game" / "csgo" / "steam.inf"
    touched = steam_inf.stat().st_mtime + 120
    os.utime(steam_inf, (touched, touched))

    assert cli.main(["sync", "--check", "--game", "cs2", "--fail-if-stale"]) == cli.EXIT_PARTIAL
    printed = capsys.readouterr().out
    assert "stale" in printed
    assert "game/csgo/steam.inf" in printed


@pytest.mark.skipif(shutil.which("git") is None, reason="the commit signal needs git")
def test_check_reports_stale_when_the_checkout_moved_on(tmp_path, capsys):
    """A normal re-fetch moves HEAD without touching the indexed file contents: the commit signal catches it."""
    root = write_tree(_checkout("cs2"))
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "indexed build")
    _load("cs2", root)

    assert cli.main(["sync", "--check", "--game", "cs2", "--fail-if-stale"]) == cli.EXIT_OK
    capsys.readouterr()

    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "newer")
    assert cli.main(["sync", "--check", "--game", "cs2", "--fail-if-stale"]) == cli.EXIT_PARTIAL
    printed = capsys.readouterr().out
    assert "checkout moved on" in printed
    assert "indexed " in printed and "HEAD now " in printed


def test_check_reports_sources_missing_for_a_build_loaded_from_elsewhere(tmp_path, capsys):
    """`load` from an arbitrary clone records no root, so the check must say so rather than claim current."""
    _load("cs2", write_tree(tmp_path / "elsewhere"))

    assert cli.main(["sync", "--check", "--game", "cs2", "--fail-if-stale"]) == cli.EXIT_PARTIAL
    printed = capsys.readouterr().out
    assert "sources missing" in printed
    assert str(_checkout("cs2") / "game" / "csgo" / "steam.inf") in printed


def test_fail_if_stale_alone_is_a_usage_error():
    """The gate flag only means something with --check; argparse's usage exit (2) says so."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["sync", "--fail-if-stale"])
    assert excinfo.value.code == 2


# ------------------------------------------------------------------- migrations


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]


def test_fresh_migration_leaves_no_scratch_tables(tmp_path):
    """A brand-new database must not carry a rebuild's intermediate table beside the real one."""
    db = Database(tmp_path / "fresh.db")
    with db.writer() as conn:
        version = schema_version(conn)
        names = _table_names(conn)

    latest = max(v for v, _ in _migrations())
    assert version == latest, "the runner applied every migration (otherwise nothing was rebuilt)"
    assert [n for n in names if _SCRATCH_TABLE.search(n)] == []
    # The tables the scratch-using migrations rebuilt, present under their final names.
    for rebuilt in ("build_runs", "evidence", "host_lock_events", "job_events", "job_links", "jobs"):
        assert rebuilt in names


def test_an_old_database_migrates_without_losing_its_timeline(tmp_path):
    """v12 → current drives 0013's stash-and-swap of `jobs`; the rows and the new CHECK must survive it.

    This is the path an existing store walks. It matters most for any later "consolidation" of a
    scratch step into its final form: the fresh path can be clean while the upgrade silently wipes a
    timeline, which is exactly what 0013's stash dance exists to prevent.
    """
    conn = connect(tmp_path / "old.db")
    try:
        for version, sql in _migrations():  # the real migration SQL, just stopped at 12
            if version <= 12:
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        assert schema_version(conn) == 12

        conn.execute(
            "INSERT INTO jobs (id, kind, title, target, status) VALUES ('j-1a2b3c4d', 'test', 'T', 'cs2', 'running')"
        )
        conn.execute("INSERT INTO job_events (id, job_id, level, message) VALUES (7, 'j-1a2b3c4d', 'info', 'started')")
        conn.execute("INSERT INTO job_links (job_id, kind, ref_id) VALUES ('j-1a2b3c4d', 'test_run', 't-1')")
        conn.commit()

        assert migrate(conn) == max(v for v, _ in _migrations())

        assert [n for n in _table_names(conn) if _SCRATCH_TABLE.search(n)] == []
        assert conn.execute("SELECT status FROM jobs WHERE id = 'j-1a2b3c4d'").fetchone()[0] == "running"
        assert conn.execute("SELECT message FROM job_events WHERE id = 7").fetchone()[0] == "started"
        assert conn.execute("SELECT ref_id FROM job_links WHERE job_id = 'j-1a2b3c4d'").fetchone()[0] == "t-1"
        # 0013's whole point: the status CHECK now admits a run with no verdict.
        conn.execute("UPDATE jobs SET status = 'inconclusive' WHERE id = 'j-1a2b3c4d'")
    finally:
        conn.close()
