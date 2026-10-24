"""Digest: the weekly read a human or agent acts on. Deterministic, no model calls.

Ported from the hermes coding loop (`~/hermes-infra/coding-loop/collect.py` + its digest writer):
collect outcomes, classify them against a **closed** class set, write ``signals.jsonl`` and
``lessons.json``, and render a report that names its own unreadable sources instead of quietly
reporting zero. That "a skip is not a pass" section is the reason anyone trusts the rest of the
report — a digest that reads as a quiet week because it could not read its input is worse than none.

Ported shape, three files, all under ``<data_dir>/learning/``::

    signals.jsonl                one row per classified event in the window
    lessons.json                 per-class lesson proposals for the writeback lane (never applied here)
    reports/digest-<date>.md     the human-readable digest

A lesson is a rule, never a log: one lesson per class per run, only when the class has at least
``MIN_SIGNALS_PER_LESSON`` independent signals and at least one named evidence ref. Nothing is
applied by this module — the writeback lane (``docs/learning/weekly_writeback.prompt.md``) reads
``lessons.json`` and patches the owning skill.
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs.db.connection import Database
from s2docs.learning import events as _events
from s2docs.learning import iso, jobs_index, learning_dir

__all__ = ["CLASSES", "MIN_SIGNALS_PER_LESSON", "classify", "digest"]

# Closed set. A class that is not here is not a class: it is ``unknown``, and it is listed as such in
# the report rather than dropped (the coding loop's `classes.json` discipline — no silent bucket).
CLASSES: tuple[str, ...] = ("failed_verdict", "no_verdict", "closed_error", "artifacts_missing",
                            "gate_unmet", "unknown")

# A lesson needs two independent signals and a named ref before anyone may act on it.
MIN_SIGNALS_PER_LESSON = 2

# Where a lesson belongs. An unmapped class is reported, never invented.
_OWNER: dict[str, str | None] = {
    "failed_verdict": "cs2-test-orchestration",
    "no_verdict": "cs2-test-orchestration",
    "closed_error": "source2-modding",
    "artifacts_missing": "cs2-test-orchestration",
    "gate_unmet": "source2-modding",
    "unknown": None,
}

# The artifacts the report points a reader at, and whether each is a JSONL (row-counted) or a
# single JSON document.
_ARTIFACTS = ("sft_pairs.jsonl", "corrections.jsonl", "hard_negatives.jsonl", "gap_report.json")


def _data(event: dict) -> dict:
    """An event's ``data`` payload as a dict, tolerating a JSON string (the record's shape is not this
    module's to assume)."""
    raw = event.get("data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def classify(event: dict) -> str:
    """One definition of the failure vocabulary (rule 4): read the recorded status/level, never a
    proxy. Returns a member of ``CLASSES`` — ``unknown`` is still a member, so nothing is dropped."""
    level = event.get("level")
    data = _data(event)
    status = (event.get("job") or {}).get("status")
    if status == "inconclusive" or (status == "passed" and data.get("verdict") != "pass"):
        return "no_verdict"
    if level in ("error", "crash"):
        return "closed_error"
    if data.get("gate_unmet") or "gate" in str(data.get("reason", "")).lower():
        return "gate_unmet"
    if level == "warn" and data.get("artifact_missing"):
        return "artifacts_missing"
    if status == "failed":
        return "failed_verdict"
    return "unknown"


def digest(db: Database, *, origin: str | None = "human", since_days: int | None = 30) -> dict:
    """Collect, classify, and write ``signals.jsonl`` + ``lessons.json`` + ``reports/digest-<date>.md``.

    Never raises for an unreadable source: the whole point of the "Harness notes" section is that a
    source it could not read is *reported* (with the same ``LearningSourceMissing`` text `events`
    raises), so the report can never read as a quiet week by accident. The only default scope is real
    usage (rule 2), and ``origin=None`` is an explicit, justified caller request.
    """
    notes: list[str] = []
    try:
        index = jobs_index(db)
    except Exception as exc:                       # noqa: BLE001 — the point is to report it
        index = {}
        notes.append(f"job ledger unreadable: {exc}")
    try:
        evs = _events(db, origin=origin, since_days=since_days)
    except Exception as exc:                       # noqa: BLE001 — rule 6: name the source, never zero
        evs = []
        notes.append(f"job-event log unreadable: {exc}")

    signals = []
    for e in evs:
        signals.append({
            "ts": e.get("created_at"),
            "job_id": e.get("job_id"),
            "origin": e.get("origin"),
            "class": classify(e),
            "message": e.get("message"),
            "evidence_ref": e.get("id"),
        })
    counts: dict[str, int] = {}
    for s in signals:
        counts[s["class"]] = counts.get(s["class"], 0) + 1

    out_dir = learning_dir()
    (out_dir / "signals.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in signals), encoding="utf-8")
    lessons = _lessons(counts, signals, notes)
    (out_dir / "lessons.json").write_text(json.dumps(lessons, indent=2), encoding="utf-8")

    day = iso()[:10]
    path = out_dir / "reports" / f"digest-{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(signals, counts, lessons, notes, origin, since_days, len(index)),
                    encoding="utf-8")
    return {
        "report": str(path),
        "lessons": str(out_dir / "lessons.json"),
        "signals": len(signals),
        "counts": counts,
        "notes": notes,
    }


def _lessons(counts: dict[str, int], signals: list[dict], notes: list[str]) -> dict:
    """Per-class proposals with named evidence refs. Nothing is applied here — this is a proposal
    file a human or the writeback lane reads."""
    proposals = []
    for klass, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n < MIN_SIGNALS_PER_LESSON:
            continue
        refs = [s["job_id"] for s in signals if s["class"] == klass and s.get("job_id")][:5]
        proposals.append({"class": klass, "count": n, "refs": refs,
                          "owner_hint": _OWNER.get(klass), "adopted": None})
    return {
        "generated_at": iso(),
        "lessons": proposals,
        "rule": ("one lesson per class per run, only with count >= "
                 f"{MIN_SIGNALS_PER_LESSON} and a named ref; a lesson is a rule, never a log"),
        "notes": notes,
    }


def _row_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.suffix == ".jsonl":
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return 1


def _render(signals: list[dict], counts: dict[str, int], lessons: dict, notes: list[str],
            origin: str | None, since_days: int | None, jobs_total: int) -> str:
    lines = [
        f"# s2docs learning digest — {iso()[:10]}",
        "",
        f"Window: last {since_days} days · origin: `{origin or 'all'}` · "
        f"jobs in ledger: **{jobs_total}** · signals: **{len(signals)}**",
        "",
        "## Signals by class",
        "",
        "| class | count | owner skill |",
        "|---|---|---|",
    ]
    table = [f"| `{k}` | {v} | `{_OWNER.get(k) or 'unmapped'}` |"
             for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    lines += table or ["| _(none)_ | 0 | |"]

    lines += ["", "## Artifacts on disk", ""]
    d = learning_dir()
    for name in _ARTIFACTS:
        p = d / name
        n = _row_count(p)
        lines.append(f"- `{name}` — {'missing' if not p.exists() else f'{n} row(s)'}")

    lines += [
        "",
        "## Proposed lessons (writeback lane)",
        "",
        "One lesson per class per run, only with "
        f"count >= {MIN_SIGNALS_PER_LESSON} and a named ref. A lesson is a rule, never a log.",
        "",
    ]
    body = [f"- `{lesson['class']}` ({lesson['count']}) → {lesson['owner_hint'] or 'unmapped'}; refs: "
            f"{', '.join(lesson['refs']) or '(none)'}" for lesson in lessons["lessons"]]
    lines += body or ["- _(nothing clears the bar)_"]

    lines += ["", "## Harness notes (a skip is not a pass)", ""]
    lines += [f"- {n}" for n in notes] or ["- every declared source was readable"]
    return "\n".join(lines) + "\n"
