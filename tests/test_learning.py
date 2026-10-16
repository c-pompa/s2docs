"""The learning loop: provenance, harvesters, digest, gate, sft, exemplar, export.

The loop turns recorded job outcomes into eval and training signal (see docs/LEARNING_LOOP.md).
Two invariants are decided here, before any artifact exists:

1. **Provenance at intake** — a job records who asked for it (`origin`, migration 0014), because a run
   a person watched is otherwise indistinguishable from driver/eval/probe traffic, and an artifact
   built mostly from synthetic rows describes the test suite rather than usage.
2. **One definition per concept** — the modules under `s2docs.learning` are the only implementations
   of "what did the human say", "what did this run prove", "was judging possible".

Everything below is offline: no host, no network, no model call. A miniature indexed build comes from
the shared `db`/`repo` fixtures in `tests/conftest.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2docs import hosts, jobs
from s2docs.db import connection
from s2docs.db.connection import Database

# --------------------------------------------------------------------- helpers


def _enqueue(db: Database, **over):
    """A valid intake with the fields the gate requires, overridable per test."""
    kwargs = dict(
        kind="test",
        title="occluder wire",
        target="ws/occluder-wire@abc123",
        purpose="verify the wire is occluded",
        expected="the wire is hidden behind the wall",
        host_choice="ask",
    )
    kwargs.update(over)
    return jobs.enqueue(db, **kwargs)


# ------------------------------------------------------------- 0014: provenance


def _db_at_v13(path: Path):
    """A schema built by the real migration files, stopped at 13 — so a test can prove what 0014 does
    to a database that predates it. The runner always applies everything, so it is replayed here.
    """
    conn = connection.connect(path)
    for version, sql in connection._migrations():
        if version <= 13:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    return conn


def test_migration_0014_adds_jobs_origin_and_defaults_existing_rows_to_agent(tmp_path: Path):
    """`jobs.origin` is additive (ALTER TABLE, not a rebuild like 0013) and every row that predates it
    reads 'agent': historical runs were never tagged, and guessing which of them a person asked for
    would be inventing data — a smaller honest sample beats a larger guessed one."""
    conn = _db_at_v13(tmp_path / "pre14.db")
    conn.execute("INSERT INTO jobs (id, kind, title, target, status) VALUES ('j-old', 'test', 't', 'x@a', 'passed')")
    conn.commit()

    assert connection.migrate(conn) == 14
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "origin" in cols, "jobs.origin is missing"
    assert cols["origin"]["dflt_value"] == "'agent'", cols["origin"]
    assert cols["origin"]["notnull"] == 1
    assert conn.execute("SELECT origin FROM jobs WHERE id = 'j-old'").fetchone()[0] == "agent"
    conn.close()


def test_migration_0014_keeps_the_timeline_and_the_queue_indexes(tmp_path: Path):
    """A column add must not disturb what 0012/0013 built: the children keep their rows, and the
    indexes are still there (the loop reads `(origin, created_at)` on every scan)."""
    conn = _db_at_v13(tmp_path / "pre14b.db")
    conn.execute("INSERT INTO jobs (id, kind, title, target) VALUES ('j-1', 'test', 't', 'x@a')")
    conn.execute("INSERT INTO job_events (job_id, level, message) VALUES ('j-1', 'info', 'hi')")
    conn.execute("INSERT INTO job_links (job_id, kind, ref_id) VALUES ('j-1', 'build', 'b-1')")
    conn.commit()

    assert connection.migrate(conn) == 14
    assert conn.execute("SELECT count(*) FROM job_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM job_links").fetchone()[0] == 1
    assert {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")} >= {
        "jobs_queue", "jobs_status", "job_events_job", "jobs_origin",
    }
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


# ------------------------------------------------------- provenance at intake


def test_enqueue_records_origin_and_rejects_an_unknown_one(tmp_path: Path):
    """The intake is the only writer of `origin`: a known value is stored, an unknown one is a 422
    naming the field (never a silent member of the set)."""
    db = Database(tmp_path / "data" / "s2docs.db")
    job = _enqueue(db, origin="driver")
    assert job.origin == "driver"

    with pytest.raises(jobs.JobValidationError) as exc:
        _enqueue(db, origin="wat")
    assert "origin" in exc.value.fields


def test_enqueue_defaults_to_agent_and_round_trips_every_known_origin(tmp_path: Path):
    """An omitted origin is `agent` — Hermes' own traffic, not a person's. Every declared value
    round-trips through the row mapper."""
    from s2docs import models as m

    db = Database(tmp_path / "data" / "s2docs.db")
    assert _enqueue(db, origin=None).origin == m.DEFAULT_JOB_ORIGIN == "agent"
    for value in m.JOB_ORIGINS:
        assert _enqueue(db, origin=value).origin == value


def test_origin_is_visible_on_the_job_page_payload(tmp_path: Path):
    """Whatever the loop reads, a human must be able to see too: the origin rides the REST job
    payload, so the page can say who asked for a run."""
    from fastapi.testclient import TestClient

    from s2docs.api.app import create_app

    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    with TestClient(create_app(db.path)) as client:
        created = client.post("/api/jobs", json={
            "kind": "demo", "title": "t", "target": "x@a", "purpose": "p", "expected": "e",
            "host_id": "desktop-pc", "origin": "human",
        })
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["origin"] == "human"
        listed = client.get("/api/jobs").json()
        items = listed["items"] if isinstance(listed, dict) else listed
        assert items[0]["origin"] == "human"
        # an unknown origin is a 422 naming the field, through the same intake gate the CLI and MCP use
        bad = client.post("/api/jobs", json={
            "kind": "demo", "title": "t", "target": "x@a", "purpose": "p", "expected": "e",
            "host_id": "desktop-pc", "origin": "definitely-not-an-origin",
        })
        assert bad.status_code == 422, bad.text


def test_cli_job_enqueue_records_the_origin_and_rejects_an_unknown_one(tmp_path, monkeypatch, capsys):
    """The CLI is a real caller, so it must be able to tag itself: `--origin drive` is rejected by
    argparse (choices), and `--origin driver` lands in the ledger."""
    import sys

    from s2docs import cli

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    from s2docs.config import get_settings

    get_settings.cache_clear()
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")

    argv = ["job", "enqueue", "--kind", "demo", "--title", "t", "--target", "x@a",
            "--purpose", "p", "--expected", "e", "--host", "desktop-pc", "--origin", "driver"]
    monkeypatch.setattr(sys, "argv", ["s2docs", *argv])
    assert cli.main(argv) == cli.EXIT_OK, capsys.readouterr().err

    with db.reader() as conn:
        assert conn.execute("SELECT origin FROM jobs").fetchone()[0] == "driver"

    bad = ["job", "enqueue", "--kind", "demo", "--title", "t", "--target", "x@a",
           "--purpose", "p", "--expected", "e", "--host", "desktop-pc", "--origin", "nope"]
    monkeypatch.setattr(sys, "argv", ["s2docs", *bad])
    with pytest.raises(SystemExit):          # argparse rejects a value outside the closed set
        cli.main(bad)


def test_mcp_job_enqueue_carries_origin(tmp_path):
    """The MCP tool is the surface an agent calls, so it must accept the tag too — otherwise every
    agent-run job is untagged and the loop cannot tell it from a person's."""
    import anyio
    from mcp import Client

    from s2docs.mcp_server import create_mcp_server

    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "desktop-pc", "pompa@x", shell="powershell")
    server = create_mcp_server(db)

    async def call():
        async with Client(server) as c:
            res = await c.call_tool("job_enqueue", {
                "kind": "demo", "title": "t", "target": "x@a", "purpose": "p", "expected": "e",
                "host_id": "desktop-pc", "origin": "eval",
            })
            return res

    res = anyio.run(call)
    assert res.structured_content["origin"] == "eval"
    with db.reader() as conn:
        assert conn.execute("SELECT origin FROM jobs").fetchone()[0] == "eval"
