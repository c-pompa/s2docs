"""Confirm PUSH channel: land every human-confirm row in files a waiting agent can watch.

A Hermes agent waiting on a human verdict cannot listen on HTTP, and polling ``GET /api/evidence``
every ~5 s is both slow and wasteful. It *can* read and stat a local file cheaply. So on every
``POST /api/evidence`` with ``kind='human-confirm'`` the server also pushes the row to two files in
the data dir:

- ``confirms.jsonl`` — append-only journal, one compact JSON record per confirm (the push history).
- ``latest_confirm.json`` — the most recent record, rewritten atomically (tmp + ``os.replace``) so a
  reader never sees a half-written file.

The record is the full :class:`s2docs.models.Evidence` row (``id``, ``created_at`` — the timestamp,
``test_run_id``, ``data`` payload, plus ``project_id``/``step_id``/``kind``/``ref``). The jsonl line
and the latest file hold byte-identical JSON, so "latest matches the last line" is checkable.

Both paths are discoverable at runtime from ``GET /api/health`` (``confirms_jsonl`` /
``latest_confirm``); agents should read them from there rather than hardcoding.

The push is best-effort side-channel only: the evidence row is already durable in the DB before this
runs, so a filesystem hiccup must never fail (or 500) the POST that recorded it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from s2docs import models as m
from s2docs.config import get_settings

logger = logging.getLogger("s2docs")

CONFIRMS_JSONL_NAME = "confirms.jsonl"
LATEST_CONFIRM_NAME = "latest_confirm.json"


def confirms_jsonl_path() -> Path:
    """The append-only confirm journal: ``<data_dir>/confirms.jsonl``."""
    return get_settings().data_dir / CONFIRMS_JSONL_NAME


def latest_confirm_path() -> Path:
    """The atomically-rewritten newest confirm: ``<data_dir>/latest_confirm.json``."""
    return get_settings().data_dir / LATEST_CONFIRM_NAME


def record(row: m.Evidence) -> dict:
    """The pushed record for one evidence row — the full row, so nothing the agent needs is dropped."""
    return row.model_dump()


def publish(row: m.Evidence) -> None:
    """Push one human-confirm row to the watchable files: append to the jsonl, refresh latest atomically.

    Never raises: a push failure is logged and swallowed — the durable evidence row is what matters,
    and the caller's POST already committed it.
    """
    payload = record(row)
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    try:
        jsonl = confirms_jsonl_path()
        latest = latest_confirm_path()
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        # Atomic latest: write a sibling tmp file, fsync it, then rename over the real path so a
        # concurrent reader sees either the old record or the new one, never a torn write.
        tmp = latest.with_name(f"{latest.name}.tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, latest)
    except Exception:  # side channel must not break the recording path
        logger.exception("confirm push failed for evidence %s", row.id)
