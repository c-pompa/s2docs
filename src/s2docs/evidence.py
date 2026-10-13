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
MAX_REF_LEN = 4096
MAX_DATA_BYTES = 65536


class EvidenceError(ValueError):
    pass


def _row(row: sqlite3.Row) -> m.Evidence:
    return m.Evidence(
        id=row["id"],
        project_id=row["project_id"],
        step_id=row["step_id"],
        kind=row["kind"],
        ref=row["ref"],
        data=json.loads(row["data"]),
        created_at=row["created_at"],
    )


def add(
    db: Database,
    *,
    kind: str,
    project_id: str,
    step_id: str | None = None,
    ref: str | None = None,
    data: dict | None = None,
) -> m.Evidence:
    """Store one evidence row. Validates the kind and caps `ref`/`data` so a caller can't bloat the DB.

    A (project_id, step_id) pair that isn't a real project step is rejected (the composite FK); so is
    an unknown project.
    """
    if kind not in EVIDENCE_KINDS:
        raise EvidenceError(f"kind must be one of {EVIDENCE_KINDS}, not {kind!r}")
    if not project_id or not str(project_id).strip():
        raise EvidenceError("project_id is required")
    if ref is not None and len(ref) > MAX_REF_LEN:
        raise EvidenceError(f"ref is too long ({len(ref)} > {MAX_REF_LEN} chars)")
    payload = json.dumps(data or {})
    if len(payload.encode("utf-8")) > MAX_DATA_BYTES:
        raise EvidenceError(f"data is too large ({len(payload)} > {MAX_DATA_BYTES} bytes)")
    eid = "e-" + secrets.token_hex(4)
    with db.writer() as conn, conn:
        try:
            conn.execute(
                "INSERT INTO evidence (id, project_id, step_id, kind, ref, data) VALUES (?, ?, ?, ?, ?, ?)",
                (eid, project_id, step_id, kind, ref, payload),
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
