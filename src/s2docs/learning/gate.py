"""Golden-question eval: measure the index, and refuse to publish a number it cannot back.

    uv run s2docs learning gate               # score and append to eval_history.jsonl
    uv run s2docs learning gate --self-test   # falsify the gate itself

Exit 0 pass · 1 regression (recall under the floor, below the last recorded run, or a trap failed)
· 2 could not evaluate (golden set missing or too small, index unreadable) — never a pass.

Ported from ask-hittle's ``eval.py`` (rank metrics recall@k + MRR, a real trap class, paraphrase
invariance, ``MIN_RECALL``, an append-only ``eval_history.jsonl``, and a ``gate()`` that returns a
reason or ``None``) and the hermes coding loop's ``gate.sh`` (an oracle control before any number is
published; COULD-NOT-EVALUATE is never a pass; a gate measures, it does not promote).

The s2docs analogue of "the generator refused honestly" is **a query with no legitimate hit must
return none** — the index must never invent an answer. That is the `trap` class here.

Deterministic and offline: the only oracle is `Repository.search`, run against the caller's reader
connection. No network, no model call.
"""

from __future__ import annotations

import json
import sys
import time

from s2docs.db.connection import Database
from s2docs.learning import learning_dir

DEFAULT_THRESHOLDS = {"min_recall": 0.9, "min_questions": 3, "ks": [1, 3, 5], "max_regression": 0.0}


class GateError(RuntimeError):
    """Could not evaluate: a declared input was missing or unreadable — an error, never a pass.

    Rule 6, applied to the gate itself: a run over a shrunken or absent golden set is not evidence
    the index is good, so it raises instead of scoring zero failures and exiting 0.
    """


def questions_file() -> list[dict]:
    """The frozen golden set. Missing is a GateError — the loop ships its questions, it never
    invents them."""
    path = learning_dir() / "golden_questions.json"
    if not path.exists():
        raise GateError(f"no golden questions at {path}")
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


def thresholds() -> dict:
    """The calibrated bounds, or the module defaults when none are configured."""
    path = learning_dir() / "thresholds.json"
    if not path.exists():
        return dict(DEFAULT_THRESHOLDS)
    return json.loads(path.read_text(encoding="utf-8"))


def _search(db: Database, query: str, *, kinds, limit: int = 20) -> list:
    from s2docs.db.repository import Repository

    with db.reader() as conn:
        return list(Repository(conn).search(query, kinds=kinds, limit=limit).items)


def _rank_of(expect_key: str, hits: list) -> int | None:
    for i, h in enumerate(hits, 1):
        if getattr(h, "key", None) == expect_key:
            return i
    return None


def run(db: Database, *, questions: list[dict] | None = None, ks: tuple[int, ...] = (1, 3, 5),
        verbose: bool = False, record: bool = True) -> dict:
    """Score every question against the index, append the run to history, and return the summary.

    `record=False` is for the falsification self-test: a planted, impossible question is not a
    measurement of the index, so it must not enter the append-only history the regression check
    reads. `questions_total` counts every question (retrieval + trap) so `min_questions` can guard
    the size of the set; `n` counts only the retrieval questions recall is measured over.
    """
    qs = questions if questions is not None else questions_file()
    per_question, scored = [], []
    for q in qs:
        row = {"id": q["id"], "kind": q.get("kind", "retrieval"), "query": q["query"]}
        hits = _search(db, q["query"], kinds=q.get("expect_kinds"))
        if q.get("expect_no_hits"):
            row["empty"] = not hits
            row["hits"] = len(hits)
        else:
            row["rank"] = _rank_of(q["expect_key"], hits)
            row["rr"] = 1.0 / row["rank"] if row["rank"] else 0.0
            for k in ks:
                row[f"recall@{k}"] = bool(row["rank"] and row["rank"] <= k)
            if q.get("paraphrase"):                     # invariance is measured, not assumed
                p = _search(db, q["paraphrase"], kinds=q.get("expect_kinds"))
                pm = _rank_of(q["expect_key"], p)
                row["paraphrase_hit"] = bool(pm)
                row["paraphrase_rank"] = pm
            scored.append(row)
        per_question.append(row)
        if verbose:
            ok = row.get("empty") if q.get("expect_no_hits") else bool(row.get("rr"))
            print(f"[{'PASS' if ok else 'FAIL'}] ({row['kind']}) {q['query']}")

    n = len(scored)
    summary = {
        "ts": time.time(),
        "n": n,
        "questions_total": len(qs),
        "recall": round(sum(bool(r["rr"]) for r in scored) / n, 3) if n else 0.0,
        "mrr": round(sum(r["rr"] for r in scored) / n, 3) if n else 0.0,
        **{f"recall@{k}": round(sum(r[f"recall@{k}"] for r in scored) / n, 3) if n else 0.0
           for k in ks},
        "traps": {"total": sum(1 for r in per_question if "empty" in r),
                  "passed": sum(1 for r in per_question if r.get("empty"))},
        "paraphrase": {"total": sum(1 for r in per_question if "paraphrase_hit" in r),
                       "passed": sum(1 for r in per_question if r.get("paraphrase_hit"))},
        "per_question": per_question,
    }
    if record:
        _append_history(summary)
    return summary


def _append_history(summary: dict) -> None:
    """Append-only, one line per run. The per-question detail is left out of the history line; the
    file is a trend, not a transcript."""
    path = learning_dir() / "eval_history.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in summary.items() if k != "per_question"}) + "\n")


def last_good_recall() -> float | None:
    """The recall of the most recent recorded run, or None when nothing has been measured yet.

    `None` here is meaningful: SFT prep refuses to train without a baseline, and the gate treats a
    first run as having nothing to regress against.
    """
    path = learning_dir() / "eval_history.jsonl"
    if not path.exists():
        return None
    best = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "recall" in rec:
            best = rec["recall"]
    return best


def gate(summary: dict, *, min_recall: float = 0.9, max_regression: float = 0.0,
         last_good: float | None = None, min_questions: int = 1) -> str | None:
    """A failure reason, or None when the eval passes.

    Insufficient data — too few questions to be a gate — raises instead of returning a reason: you
    cannot turn 'we did not measure' into a number, and exiting 0 there would be a lie.
    """
    total = summary.get("questions_total", summary["n"])
    if total < min_questions:
        raise GateError(f"only {total} question(s) in the golden set; need >= {min_questions}")
    if summary["n"] and summary["recall"] < min_recall:
        return f"recall {summary['recall']} < {min_recall}"
    if summary["traps"]["total"] != summary["traps"]["passed"]:
        return f"trap failures: {summary['traps']['total'] - summary['traps']['passed']}"
    if summary["paraphrase"]["total"] != summary["paraphrase"]["passed"]:
        return f"paraphrase invariance failures: {summary['paraphrase']['total'] - summary['paraphrase']['passed']}"
    if last_good is not None and summary["n"] and summary["recall"] < last_good - max_regression:
        return f"recall {summary['recall']} below last recorded {last_good}"
    return None


def self_test(db: Database) -> int:
    """Falsify the gate: an impossible expectation must make it fail.

    A gate nobody has seen fail is decoration (the coding loop's control before any number is
    published). Returns 0 when the gate correctly rejected the plant, 2 when it did not — so a
    broken gate is a non-zero exit, exactly like a broken evaluation.
    """
    qs = questions_file()
    planted = [dict(q) for q in qs[:1]]
    planted[0]["expect_key"] = "zzz_definitely_not_indexed"
    planted[0].pop("expect_no_hits", None)
    s = run(db, questions=planted, record=False)
    return 0 if gate(s, min_recall=1.0) else 2


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    from s2docs.config import get_settings

    try:
        db = Database(get_settings().db_path)
        if "--self-test" in argv:
            rc = self_test(db)
            print("gate self-test: "
                  + ("OK (the gate can fail)" if rc == 0 else "BROKEN — it passed a plant"))
            return rc
        th = thresholds()
        s = run(db, ks=tuple(th.get("ks", (1, 3, 5))),
                verbose=("-v" in argv or "--verbose" in argv))
        reason = gate(s, min_recall=th["min_recall"], max_regression=th.get("max_regression", 0.0),
                      last_good=last_good_recall(), min_questions=th.get("min_questions", 1))
    except GateError as exc:
        print(f"COULD NOT EVALUATE: {exc}", file=sys.stderr)
        return 2
    print(f"recall@5: {s['recall']:.0%}  MRR: {s['mrr']:.3f}  "
          f"traps: {s['traps']['passed']}/{s['traps']['total']}  "
          f"paraphrase: {s['paraphrase']['passed']}/{s['paraphrase']['total']}")
    if reason:
        print(f"EVAL GATE FAILED: {reason}", file=sys.stderr)
        return 1
    return 0
