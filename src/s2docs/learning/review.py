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

The filed ``query`` is the goal's *subject*, not the run's whole goal text. A correction's ``question``
is a multi-line brief (identity line, then ``Purpose:``/``Expected:``), and the gate searches with
``Repository.search``, which ANDs up to 12 query tokens — so a brief filed verbatim is a query nothing
can match, and the case would fail for a reason unrelated to the human's answer. ``_subject()``
derives the short question; ``promote(query=...)`` overrides it when the human's question differs.
"""

from __future__ import annotations

import json
import secrets
import time

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


# The labelled brief lines in a goal, as ``harvest._goal_text`` writes them.
_BRIEF_LABELS = ("purpose", "expected")


def _subject(question: str) -> str:
    """The retrievable subject of a correction's question — not the whole brief.

    ``harvest.corrections`` records the run's full goal text: the identity line, then the
    ``Purpose:``/``Expected:`` brief. ``Repository.search`` ANDs up to 12 query tokens
    (``db.fts.build_match_query``), so filing that text verbatim asks a document to contain every
    prose word in the brief as well, and the case scores nothing for a reason that has nothing to do
    with the human's answer — the question was unretrievable, not the answer wrong.

    The subject is the goal's first non-brief line: the short human-written name the intake is built
    around (``title``). The ``target`` is deliberately not used — it identifies the run
    (``feature/test name + branch@sha``) and its branch/sha can never appear in the index, so ANDing
    it in would guarantee a zero-hit query. When the derived subject is wrong for a case, the caller
    passes ``query`` explicitly; that is what the parameter is for.
    """
    for line in (question or "").splitlines():
        text = " ".join(line.split())
        if not text:
            continue
        if text.split(":", 1)[0].strip().casefold() in _BRIEF_LABELS:
            continue
        return text.split("@", 1)[0].strip() or text
    return " ".join((question or "").split())


def queue(db: Database, *, since_days: int | None = None) -> list[dict]:
    """The correction queue, annotated with whether each row has already been promoted into the
    golden set. `promoted` is derived from the set's recorded ``source_job`` — never inferred from the
    query text, which a human may have refined."""
    rows = harvest.corrections(db, origin=None, since_days=since_days)
    promoted = {c["source_job"] for c in questions_file() if c.get("source_job")}
    for r in rows:
        r["promoted"] = r["job_id"] in promoted
    return rows


def promote(db: Database, job_id: str, *, expected: str, query: str | None = None,
            checker: str = "exact", source: str = "hitl", kind: str = "retrieval") -> dict:
    """File a human's known answer as a new frozen case.

    Returns what happened and never raises for a duplicate — a double click must not double the golden
    set. The ``expected`` value is the human's, filed verbatim; the index is never consulted to fill
    it in (that would make the case circular and the gate meaningless).

    ``query`` is what the gate will search with. Omitted, it is the goal's subject (``_subject``) — NOT
    the run's full goal text, which as a query ANDs the brief's whole prose into the match and makes
    the case unretrievable for a reason that has nothing to do with the human's answer. Pass ``query``
    when the derived subject is not the question a person would actually ask.
    """
    rows = {r["job_id"]: r for r in harvest.corrections(db, origin=None)}
    if job_id not in rows:
        raise KeyError(f"no correction for job {job_id!r}")
    row = rows[job_id]
    resolved = query if query is not None else _subject(row["question"])

    cases = questions_file()
    # Duplicate on the correction itself, or on the question it files. Promoting one correction twice
    # must be a no-op, and two corrections that reduce to the same subject are one question — filing
    # both would make the gate score a single question twice, so its numbers would move with no new
    # evidence behind them.
    if any(c.get("source_job") == job_id or _norm(c.get("query", "")) == _norm(resolved)
           for c in cases):
        return {"appended": False, "reason": "duplicate", "job_id": job_id}

    cid = "corr-" + secrets.token_hex(4)
    case = {
        "id": cid,
        "kind": kind,
        "query": resolved,
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
