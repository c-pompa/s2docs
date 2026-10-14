"""Evidence store: the structured record an orchestration gate actually checks.

The `evidence` gate used to accept notes/artifacts alone, which once let a step be marked done
before its README existed. An evidence row is durable, typed proof linked to a project step:

- `kind` says what it is: a console transcript, a file hash, a pixel diff, a screenshot, a plain
  note, or other. Notes are recorded but do NOT satisfy the gate.
- `ref` points at the thing (path, url or command); `data` carries the structured detail a checker
  wants — e.g. a pixel diff's pct + noise floor, file md5s, a transcript excerpt.

`count_for_step(..., exclude_note=True)` is what `engine._check_gate` calls, so the server, not the
caller, decides whether a step has real evidence.
"""

from __future__ import annotations

import json
import secrets
import sqlite3

from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

EVIDENCE_KINDS: tuple[str, ...] = m.EVIDENCE_KINDS
VERDICTS: tuple[str, ...] = m.VERDICTS
CONFIRM_SOURCES: tuple[str, ...] = m.CONFIRM_SOURCES
MAX_REF_LEN = 4096
MAX_DATA_BYTES = 65536


class EvidenceError(ValueError):
    pass


class EvidenceValidationError(EvidenceError):
    """A well-formed row with a bad payload (e.g. a human-confirm verdict outside the allowed set).

    Distinct from the generic :class:`EvidenceError` so the HTTP layer can answer 422 (not 400) and name
    the offending field(s)."""

    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


def _row(row: sqlite3.Row) -> m.Evidence:
    return m.Evidence(
        id=row["id"],
        project_id=row["project_id"],
        step_id=row["step_id"],
        kind=row["kind"],
        ref=row["ref"],
        data=json.loads(row["data"]),
        test_run_id=row["test_run_id"],
        created_at=row["created_at"],
    )


def add(
    db: Database,
    *,
    kind: str,
    project_id: str | None = None,
    step_id: str | None = None,
    ref: str | None = None,
    data: dict | None = None,
    test_run_id: str | None = None,
) -> m.Evidence:
    """Store one evidence row. Validates the kind and caps `ref`/`data` so a caller can't bloat the DB.

    A (project_id, step_id) pair that isn't a real project step is rejected (the composite FK); so is
    an unknown project.

    `project_id` may be omitted when `test_run_id` is given: the project is resolved from that run
    (and stays NULL when the run itself has no project — e.g. a director-driven iso run). This is what
    lets a human-confirm verdict be stored as durable evidence without the caller knowing a project.
    """
    if kind not in EVIDENCE_KINDS:
        raise EvidenceError(f"kind must be one of {EVIDENCE_KINDS}, not {kind!r}")
    if test_run_id:
        with db.reader() as conn:
            run_row = conn.execute("SELECT project_id FROM test_runs WHERE id = ?", (test_run_id,)).fetchone()
        if run_row is None:
            raise EvidenceError(f"unknown test_run {test_run_id!r}")
        if not (project_id and str(project_id).strip()):
            project_id = run_row["project_id"]  # may still be None
    elif not (project_id and str(project_id).strip()):
        raise EvidenceError("project_id is required (or a test_run_id to resolve it from)")
    if not (project_id and str(project_id).strip()):
        project_id = None
    # A human-confirm row IS a verdict: it must carry data.verdict in {pass, fail, skip} (else 422) and,
    # when present, a data.source drawn from the known set. Validated here (after the run/project resolve,
    # so an unknown run still reports as that) so every writer — POST /api/evidence, POST /jobs/{id}/verdict,
    # MCP, CLI — gets the same gate from the one shared add() path.
    if kind == "human-confirm":
        payload_data = data or {}
        verdict = payload_data.get("verdict")
        if verdict not in VERDICTS:
            raise EvidenceValidationError(
                f"human-confirm verdict must be one of {VERDICTS}, not {verdict!r}", ["verdict"]
            )
        source = payload_data.get("source")
        if source is not None and source not in CONFIRM_SOURCES:
            raise EvidenceValidationError(
                f"human-confirm source must be one of {CONFIRM_SOURCES}, not {source!r}", ["source"]
            )
    if ref is not None and len(ref) > MAX_REF_LEN:
        raise EvidenceError(f"ref is too long ({len(ref)} > {MAX_REF_LEN} chars)")
    payload = json.dumps(data or {})
    if len(payload.encode("utf-8")) > MAX_DATA_BYTES:
        raise EvidenceError(f"data is too large ({len(payload)} > {MAX_DATA_BYTES} bytes)")
    eid = "e-" + secrets.token_hex(4)
    with db.writer() as conn, conn:
        try:
            conn.execute(
                "INSERT INTO evidence (id, project_id, step_id, kind, ref, data, test_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (eid, project_id, step_id, kind, ref, payload, test_run_id),
            )
        except sqlite3.IntegrityError as exc:
            raise EvidenceError(f"unknown project/step ({project_id!r}, {step_id!r}): {exc}") from exc
    return get(db, eid)


def get(db: Database, evidence_id: str) -> m.Evidence:
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    if row is None:
        raise NotFound(f"no evidence {evidence_id!r}")
    return _row(row)


def list_evidence(
    db: Database, *, project_id: str | None = None, step_id: str | None = None, kind: str | None = None
) -> list[m.Evidence]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence WHERE (? IS NULL OR project_id = ?) AND (? IS NULL OR step_id = ?) "
            "AND (? IS NULL OR kind = ?) ORDER BY created_at DESC",
            (project_id, project_id, step_id, step_id, kind, kind),
        ).fetchall()
    return [_row(r) for r in rows]


def count_for_step(db: Database, project_id: str, step_id: str, *, exclude_note: bool = False) -> int:
    """Count evidence rows for one step; `exclude_note=True` counts only real (non-note) proof."""
    with db.reader() as conn:
        return conn.execute(
            "SELECT count(*) FROM evidence WHERE project_id = ? AND step_id = ? AND (? = 0 OR kind != 'note')",
            (project_id, step_id, 1 if exclude_note else 0),
        ).fetchone()[0]


def add_and_publish(
    db: Database,
    *,
    kind: str,
    project_id: str | None = None,
    step_id: str | None = None,
    ref: str | None = None,
    data: dict | None = None,
    test_run_id: str | None = None,
) -> m.Evidence:
    """The one code path that records an evidence row AND fires the confirm push channel for it.

    `POST /api/evidence` and `POST /api/jobs/{id}/verdict` both go through here, so a human-confirm row
    (a webapp verdict as much as a chat `!pass`) lands in ``<data_dir>/confirms.jsonl`` +
    ``latest_confirm.json`` exactly the same way. The push is a best-effort side channel (see
    :func:`s2docs.confirms.publish`); the durable row is what the caller relies on.
    """
    row = add(
        db,
        kind=kind,
        project_id=project_id,
        step_id=step_id,
        ref=ref,
        data=data,
        test_run_id=test_run_id,
    )
    if row.kind == "human-confirm":
        from s2docs import confirms

        confirms.publish(row)
    return row
