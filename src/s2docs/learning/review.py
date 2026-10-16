"""The human side of the loop: review the correction queue, promote what a person can answer.

Nothing here is automatic by design. A failing run is *evidence that something is wrong*, not a
question with a known answer — inventing the expected answer to fill the queue is how an eval set
starts lying. A human writes the expectation; the code only files it, deduped on the normalized query
and **append-only** (ported from cpompa-ai-cloud's ``scripts/eval/add_correction.py``).

Promotion is append-only: an existing case is never edited, not even by a later, better correction.

Circularity guard — the reason ``promote()`` never consults the index: a golden set seeded from what
``Repository.search`` already returns scores recall 100% and measures nothing. ``promote()`` files the
human's ``expected`` verbatim and never calls the index to discover it; a case whose expectation the
human got wrong must produce a run the gate *fails*.

A promoted case is written in the same shape the gate reads (``gate.questions_file``): ``id``,
``kind``, ``query``, ``expect_key``, plus ``category``/``checker``/``source``/``source_job``/``added``
provenance.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from s2docs.db.connection import Database
from s2docs.learning import harvest, learning_dir

__all__ = ["queue", "promote", "questions_file"]

_FROZEN_NOTE = ("Never edit an expectation to make a failing case pass. Add a new question instead.")


def questions_file() -> list[dict]:
    """The cases currently in the golden set. Empty (not an error) when the file does not exist yet:
    the queue is what creates it, and asking before any promotion is a legitimate state."""
    path = learning_dir() / "golden_questions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


def _write(cases: list[dict]) -> None:
    path = learning_dir() / "golden_questions.json"
    path.write_text(json.dumps({"frozen": True, "note": _FROZEN_NOTE, "questions": cases},
                               indent=2), encoding="utf-8")


def _norm(text: str) -> str:
    """Whitespace-collapsed, casefolded key for dedupe — the same normalization
    ``add_correction.normalize_prompt`` uses, so a re-worded duplicate is still a duplicate."""
    return " ".join((text or "").split()).casefold()


def queue(db: Database, *, since_days: int | None = None) -> list[dict]:
    """The correction queue, annotated with whether each row has already been promoted into the
    golden set. `promoted` is derived from the set's recorded ``source_job`` — never inferred from the
    query text, which a human may have refined."""
    rows = harvest.corrections(db, origin=None, since_days=since_days)
    promoted = {c["source_job"] for c in questions_file() if c.get("source_job")}
    for r in rows:
        r["promoted"] = r["job_id"] in promoted
    return rows


def promote(db: Database, job_id: str, *, expected: str, checker: str = "exact",
            source: str = "hitl", kind: str = "retrieval") -> dict:
    """File a human's known answer as a new frozen case.

    Returns what happened and never raises for a duplicate — a double click must not double the golden
    set. The ``expected`` value is the human's, filed verbatim; the index is never consulted to fill
    it in (that would make the case circular and the gate meaningless).
    """
    rows = {r["job_id"]: r for r in harvest.corrections(db, origin=None)}
    if job_id not in rows:
        raise KeyError(f"no correction for job {job_id!r}")
    row = rows[job_id]

    cases = questions_file()
    if any(_norm(c.get("query", "")) == _norm(row["question"]) for c in cases):
        return {"appended": False, "reason": "duplicate", "job_id": job_id}

    cid = "corr-" + secrets.token_hex(4)
    case = {
        "id": cid,
        "kind": kind,
        "query": row["question"],
        "expect_key": expected,
        "category": "correction",
        "checker": checker,
        "source": source,
        "source_job": job_id,
        "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    cases.append(case)
    _write(cases)
    return {"appended": True, "id": cid, **case}
