"""Confirm PUSH channel: a human-confirm POST lands in data_dir/confirms.jsonl + latest_confirm.json.

The whole point is that a waiting agent can watch a local file (stat + read, ~1-2 s) instead of
polling GET /api/evidence every 5 s, so these tests pin the file contract: exactly one line per
human-confirm, latest matches the last line, other evidence kinds leave the files alone, and a
server restart does not lose them.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from s2docs import confirms, evidence, hosts, test_runs
from s2docs.api.app import create_app
from s2docs.db.connection import Database


@pytest.fixture
def cdb(tmp_path) -> Database:
    db = Database(tmp_path / "confirms.db")
    hosts.register_host(db, "build-pc", "pompa@192.0.2.172", shell="powershell")
    return db


@pytest.fixture
def client(cdb):
    with TestClient(create_app(cdb.path)) as c:
        yield c


def _lines() -> list[dict]:
    path = confirms.confirms_jsonl_path()
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _latest() -> dict:
    return json.loads(confirms.latest_confirm_path().read_text(encoding="utf-8"))


def _post_confirm(client, run_id: str, verdict: str = "pass") -> dict:
    r = client.post(
        "/api/evidence",
        json={"kind": "human-confirm", "test_run_id": run_id, "data": {"verdict": verdict, "notes": "ok"}},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------- push contract
def test_human_confirm_writes_one_line_and_matching_latest(cdb, client):
    """One human-confirm POST: the jsonl gains exactly one line, and latest_confirm.json equals it."""
    assert _lines() == []  # nothing written before the first confirm
    run = test_runs.start(cdb, "iso_test")
    row = _post_confirm(client, run.id, verdict="pass")

    lines = _lines()
    assert len(lines) == 1
    rec = lines[0]
    assert rec["id"] == row["id"]
    assert rec["test_run_id"] == run.id
    assert rec["kind"] == "human-confirm"
    assert rec["data"]["verdict"] == "pass"
    assert rec["created_at"]  # the timestamp
    # latest is byte-identical to the last jsonl line
    raw = confirms.confirms_jsonl_path().read_text(encoding="utf-8").splitlines()[-1]
    assert confirms.latest_confirm_path().read_text(encoding="utf-8") == raw + "\n"
    assert _latest() == rec


def test_non_confirm_evidence_does_not_touch_files(cdb, client):
    """Any other kind (and a rejected confirm) leaves both files exactly as they were."""
    run = test_runs.start(cdb, "iso_test")
    _post_confirm(client, run.id, verdict="pass")
    before_lines = _lines()
    before_latest = _latest()

    for kind in ("note", "console_transcript", "pixel_diff", "file_hash", "screenshot", "other"):
        r = client.post("/api/evidence", json={"kind": kind, "test_run_id": run.id, "data": {"x": 1}})
        assert r.status_code == 201, r.text
    # a confirm that is rejected (unknown run) must not write either
    assert client.post("/api/evidence", json={"kind": "human-confirm", "test_run_id": "t-ghost"}).status_code == 400

    assert _lines() == before_lines
    assert _latest() == before_latest


def test_two_rapid_confirms_append_two_lines_latest_is_second(cdb, client):
    """Two back-to-back confirms: two jsonl lines, and latest is the second (the newest wins)."""
    run = test_runs.start(cdb, "iso_test")
    first = _post_confirm(client, run.id, verdict="fail")
    second = _post_confirm(client, run.id, verdict="pass")

    lines = _lines()
    assert [r["id"] for r in lines] == [first["id"], second["id"]]
    assert lines[-1]["data"]["verdict"] == "pass"
    assert _latest()["id"] == second["id"]
    assert _latest() == lines[-1]
    # the durable evidence store kept both too
    assert len(evidence.list_evidence(cdb, kind="human-confirm")) == 2


def test_files_survive_server_restart(cdb, tmp_path):
    """The files are data-dir state, not process memory: a fresh app/process still sees them."""
    with TestClient(create_app(cdb.path)) as c1:
        run = test_runs.start(cdb, "iso_test")
        row = _post_confirm(c1, run.id, verdict="pass")
    snapshot_lines = confirms.confirms_jsonl_path().read_text(encoding="utf-8")
    snapshot_latest = confirms.latest_confirm_path().read_text(encoding="utf-8")

    # a brand-new app object (a restarted server) points at the same data dir
    with TestClient(create_app(cdb.path)) as c2:
        assert _latest()["id"] == row["id"]
        assert confirms.confirms_jsonl_path().read_text(encoding="utf-8") == snapshot_lines
        assert confirms.latest_confirm_path().read_text(encoding="utf-8") == snapshot_latest
        # and a post-restart confirm appends to the same journal, not a fresh one
        row2 = _post_confirm(c2, run.id, verdict="fail")
    assert [r["id"] for r in _lines()] == [row["id"], row2["id"]]


def test_health_exposes_confirm_paths(cdb, client):
    """Agents discover the watch paths from /api/health rather than hardcoding them."""
    body = client.get("/api/health").json()
    assert body["confirms_jsonl"] == str(confirms.confirms_jsonl_path())
    assert body["latest_confirm"] == str(confirms.latest_confirm_path())
    assert body["confirms_jsonl"].endswith("confirms.jsonl")
    assert body["latest_confirm"].endswith("latest_confirm.json")


def test_publish_never_raises_on_write_failure(cdb, monkeypatch):
    """The push is a side channel: a filesystem error must not blow up the recording path."""
    run = test_runs.start(cdb, "iso_test")
    row = evidence.add(cdb, kind="human-confirm", test_run_id=run.id, data={"verdict": "pass"})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(confirms, "confirms_jsonl_path", boom)
    confirms.publish(row)  # must not raise
