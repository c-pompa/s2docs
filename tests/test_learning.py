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
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")
    with TestClient(create_app(db.path)) as client:
        created = client.post("/api/jobs", json={
            "kind": "demo", "title": "t", "target": "x@a", "purpose": "p", "expected": "e",
            "host_id": "build-pc", "origin": "human",
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
            "host_id": "build-pc", "origin": "definitely-not-an-origin",
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
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")

    argv = ["job", "enqueue", "--kind", "demo", "--title", "t", "--target", "x@a",
            "--purpose", "p", "--expected", "e", "--host", "build-pc", "--origin", "driver"]
    monkeypatch.setattr(sys, "argv", ["s2docs", *argv])
    assert cli.main(argv) == cli.EXIT_OK, capsys.readouterr().err

    with db.reader() as conn:
        assert conn.execute("SELECT origin FROM jobs").fetchone()[0] == "driver"

    bad = ["job", "enqueue", "--kind", "demo", "--title", "t", "--target", "x@a",
           "--purpose", "p", "--expected", "e", "--host", "build-pc", "--origin", "nope"]
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
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")
    server = create_mcp_server(db)

    async def call():
        async with Client(server) as c:
            res = await c.call_tool("job_enqueue", {
                "kind": "demo", "title": "t", "target": "x@a", "purpose": "p", "expected": "e",
                "host_id": "build-pc", "origin": "eval",
            })
            return res

    res = anyio.run(call)
    assert res.structured_content["origin"] == "eval"
    with db.reader() as conn:
        assert conn.execute("SELECT origin FROM jobs").fetchone()[0] == "eval"


# ------------------------------------------------------- readers: scope by origin
# The conftest fixture points S2DOCS_DATA_DIR at tmp_path/data, which is also where the job-event
# journal lands, so both the DB and the JSONL are per-test and never touch a checkout.


def db_for(tmp_path: Path) -> Database:
    """The database the CLI/settings/loop all resolve: ``<data_dir>/s2docs.db``."""
    return Database(tmp_path / "data" / "s2docs.db")


def _seed_job(tmp_path: Path, *, origin: str = "human", status: str = "passed", title: str = "occluder wire"):
    """One job with a timeline event and a close-out: the minimum any reader needs."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin=origin, title=title, target=f"ws/{title}@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.event(db, job.id, level="info", message="launch", data={"step": "s3"})
    jobs.finish(db, job.id, status=status,
                summary={"what": "w", "why": "y", "learned": "l", "next": "n"})
    return db, jobs.get(db, job.id)


def test_events_are_scoped_by_the_jobs_origin_and_report_their_sample(tmp_path: Path):
    """Rule 2: the default read is real usage. A driver run's events are in the log and must not
    appear in a `human` read — but they are reachable with an explicit `origin=None`."""
    from s2docs.learning import events

    db, human = _seed_job(tmp_path, origin="human", title="human run")
    _seed_job(tmp_path, origin="driver", title="driver run")

    scoped = events(db)                       # default: origin='human'
    assert {e["job_id"] for e in scoped} == {human.id}
    assert all(e["origin"] == "human" for e in scoped)
    assert scoped[0]["level"] == "info" and scoped[0]["job"]["status"] == "passed"

    every = events(db, origin=None)
    assert len(every) == 2, "the driver run is reachable, explicitly"

    with pytest.raises(ValueError):
        events(db, origin="not-an-origin")


def test_events_refuse_a_missing_source_instead_of_returning_nothing(tmp_path: Path):
    """Rule 6, the load-bearing one: an unreadable source raises. A loop that reports '0 signals'
    because it could not read its input reads as a quiet week, which is a lie of omission."""
    from s2docs.learning import LearningSourceMissing, events

    db = db_for(tmp_path)                     # no journal has been written yet
    with pytest.raises(LearningSourceMissing) as exc:
        events(db, origin=None)
    assert "job-event log" in str(exc.value)


def test_report_counts_jobs_by_origin_and_flags_the_thin_sample(tmp_path: Path):
    """Rule 5: the readiness number ships the sample, and 'real usage' is counted separately from
    synthetic traffic so a loop built on driver runs cannot look busy."""
    from s2docs.learning import report

    db, _ = _seed_job(tmp_path, origin="human")
    _seed_job(tmp_path, origin="driver", title="d1")
    _seed_job(tmp_path, origin="eval", title="e1")

    out = report(db)
    assert out["jobs_by_origin"] == {"driver": 1, "eval": 1, "human": 1}
    assert out["jobs_total"] == 3
    assert out["real_usage_jobs"] == 1
    assert out["synthetic_jobs"] == 2
    assert out["events_total"] == 3


def test_learning_dir_follows_the_data_dir_and_creates_itself(tmp_path: Path):
    """A per-call derivation: the dir follows S2DOCS_DATA_DIR (the test fixture moves it), and asking
    for it is enough to create it — every writer below assumes that."""
    from s2docs.learning import learning_dir

    d = learning_dir()
    assert d == tmp_path / "data" / "learning"
    assert d.is_dir()
