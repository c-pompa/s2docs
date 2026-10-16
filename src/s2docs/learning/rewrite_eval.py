"""The rewrite evaluation gate: measure the deterministic rewriter, in-repo and reproducibly.

``rewrite.py`` exists because ``db.fts`` ANDs every token, so a real user question retrieves nothing.
That claim was measured — but the measurement lived in a scratch script, so it was neither reviewable
nor able to gate anything. This module promotes it to a first-class gate over a frozen, in-repo eval
set (``data/rewrite_eval.json``, 20 verified rows + 7 recorded known gaps).

Three retrieval conditions are scored over the same rows:

    baseline  = the user's raw prose, unscoped            (what happens today)
    rewrite   = the deterministic rewriter's query+game   (what we built)
    oracle    = the curated keyword form, game-scoped      (upper bound: perfect rewriting)

If ``oracle`` is high and ``rewrite`` is low, the rewriter is the problem, not the corpus. If
``oracle`` is also low, the corpus changed underneath and no rewriter can be judged — that is
*could-not-evaluate*, not a rewrite regression.

Exit contract (the same shape as ``gate.py``; the CLI maps it with ``_map_gate_exit``):

    0  the deterministic rewriter is at or above its recorded floor
    1  regression — the rewriter dropped below its floor while the oracle held
    2  could not evaluate — the oracle dropped below its floor (corpus/index changed), or the eval
       set / live index was unreadable. Never a pass.

The eval set is non-circular: every ``expect_key`` is backed by an ``evidence`` passage quoted from
the target document, so nothing was derived from a search ranking. **Expectations are frozen** —
editing a row to make a score move is the exact failure this design exists to prevent.

Deterministic and offline-capable: the search function is a module-level seam (``_default_search``)
that tests replace with canned hits, exactly like ``gate._search``.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from importlib import resources

from s2docs.config import DEFAULT_PUBLIC_URL, get_settings
from s2docs.learning.rewrite import rewrite

__all__ = ["FLOORS", "K", "RewriteEvalError", "eval_set", "score", "decide", "evaluate",
           "report_lines", "main"]

# The measured baselines, recorded as floors. Measured on 2026-10-16 against the live index
# (http://10.88.15.208:8812/api/search), recall@5: baseline (prose, unscoped) 0.20, deterministic
# rewrite (query+game) 0.40, oracle (curated keywords, game-scoped) 1.00. These are *floors*: at or
# above is a pass. Changing one is a deliberate diff, never a way to make a failing run green.
FLOORS: dict[str, float] = {"baseline": 0.20, "rewrite": 0.40, "oracle": 1.00}

K = 5  # the rank window every floor is measured over (recall@1, recall@5, MRR)
_TIMEOUT = 15

_LABELS: dict[str, str] = {
    "baseline": "baseline (prose, unscoped)",
    "rewrite": "rewrite (deterministic)",
    "oracle": "oracle (curated kw, scoped)",
}


class RewriteEvalError(RuntimeError):
    """Could not evaluate: a declared input (the eval set, or the live index) was missing or
    unreadable — an error, never a pass. The analogue of ``gate.GateError``."""


def eval_set() -> dict:
    """The frozen eval set, shipped as package data (``resources.files``, like ``db/migrations``).

    Missing is a ``RewriteEvalError`` — the loop ships its expectations, it never invents them."""
    path = resources.files("s2docs.learning") / "data" / "rewrite_eval.json"
    if not path.is_file():
        raise RewriteEvalError(f"no rewrite eval set at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _default_search(query: str, game: str | None = None, *, limit: int = 10) -> list[dict]:
    """The live index, over HTTP — the module-level seam tests replace with canned hits.

    Signature: ``(query, game, limit) -> [{'key': ...}, ...]``. A transport failure raises
    ``RewriteEvalError`` (could-not-evaluate): an unreachable index must never be scored as a set of
    misses, which would masquerade as a rewriter regression or an oracle drop of unknown cause."""
    base = (get_settings().public_url or DEFAULT_PUBLIC_URL).rstrip("/")
    params = {"q": query, "limit": str(limit)}
    if game:
        params["game"] = game
    url = f"{base}/api/search?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310 (fixed http base)
            return json.load(resp).get("items", [])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RewriteEvalError(f"index unreachable at {url}: {exc}") from exc


def _rank_of(hits: list[dict], key: str) -> int | None:
    for i, hit in enumerate(hits, 1):
        if hit.get("key") == key:
            return i
    return None


def _conditions():
    """The three conditions, each mapping a row to the ``(query, game)`` it searches with."""
    return {
        "baseline": lambda r: (r["prose"], None),
        "rewrite": lambda r: ((rw := rewrite(r["prose"])).query, rw.game),
        "oracle": lambda r: (r["keywords"], r.get("game")),
    }


def score(rows: list[dict], *, search=None, k: int = K) -> dict:
    """Score all three conditions over ``rows`` and return per-condition metrics + the fixable misses.

    ``search`` is the injectable seam (default ``_default_search``); tests pass canned hits so this
    runs with no server. Returns ``{"n", "conditions": {name: {recall@1, recall@5, mrr, per}},
    "fixable": [...]}`` where ``per`` is one ``{"id", "rank"}`` per row (rank ``None`` = miss)."""
    do_search = search if search is not None else _default_search
    n = len(rows)
    conditions: dict[str, dict] = {}
    ranks: dict[str, dict[str, int | None]] = {}
    for name, cond in _conditions().items():
        hits1 = hits5 = 0
        rr = 0.0
        per: list[dict] = []
        for row in rows:
            query, game = cond(row)
            rank = _rank_of(do_search(query, game=game, limit=k), row["expect_key"]) if query else None
            per.append({"id": row["id"], "rank": rank})
            if rank == 1:
                hits1 += 1
            if rank is not None:
                hits5 += 1
                rr += 1.0 / rank
        conditions[name] = {
            "recall@1": round(hits1 / n, 4) if n else 0.0,
            "recall@5": round(hits5 / n, 4) if n else 0.0,
            "mrr": round(rr / n, 4) if n else 0.0,
            "per": per,
        }
        ranks[name] = {p["id"]: p["rank"] for p in per}

    # The most useful line for a human: a row the oracle finds and the rewriter does not is fixable.
    fixable = []
    for row in rows:
        if ranks["rewrite"][row["id"]] is None and ranks["oracle"][row["id"]] is not None:
            rw = rewrite(row["prose"])
            fixable.append({
                "id": row["id"],
                "prose": row["prose"],
                "expect_key": row["expect_key"],
                "rewrite_query": rw.query,
                "rewrite_game": rw.game,
                "rewrite_notes": list(rw.notes),
                "oracle_keywords": row["keywords"],
                "oracle_game": row.get("game"),
            })
    return {"n": n, "conditions": conditions, "fixable": fixable}


def decide(scores: dict, *, floors: dict[str, float] | None = None) -> tuple[int, str | None]:
    """The gate: ``(exit_code, reason)``. Oracle first — it is the corpus canary."""
    f = floors or FLOORS
    conds = scores["conditions"]
    oracle = conds["oracle"]["recall@5"]
    if oracle < f["oracle"]:
        return 2, (f"oracle recall@5 {oracle:.0%} < floor {f['oracle']:.0%} — the corpus or the index "
                   "changed underneath; the rewriter cannot be judged")
    rw = conds["rewrite"]["recall@5"]
    if rw < f["rewrite"]:
        return 1, f"rewrite recall@5 {rw:.0%} < floor {f['rewrite']:.0%}"
    return 0, None


def evaluate(*, search=None, rows: list[dict] | None = None) -> dict:
    """Score the eval set and apply the gate. A JSON-friendly result for the CLI."""
    data = eval_set()
    rws = rows if rows is not None else data["rows"]
    scores = score(rws, search=search)
    code, reason = decide(scores)
    return {
        "code": code,
        "reason": reason,
        "n": len(rws),
        "known_gaps": len(data["known_gaps"]),
        "conditions": {name: {kk: vv for kk, vv in metrics.items() if kk != "per"}
                       for name, metrics in scores["conditions"].items()},
        "floors": dict(FLOORS),
        "fixable": scores["fixable"],
    }


def report_lines(result: dict) -> list[str]:
    """The human report: the three-condition table, then the fixable misses (rewriter missed, oracle
    hit). Also used by the CLI, so the human view has exactly one definition."""
    lines = [f"rewrite-eval: {result['n']} rows + {result['known_gaps']} known gaps", ""]
    lines.append(f"{'condition':32} {'recall@1':>9} {'recall@5':>9} {'MRR':>7}   floor")
    for name, label in _LABELS.items():
        c = result["conditions"][name]
        lines.append(f"{label:32} {c['recall@1']:9.0%} {c['recall@5']:9.0%} {c['mrr']:7.3f}"
                     f"   {result['floors'][name]:.0%}")
    lines.append("")
    fix = result["fixable"]
    if fix:
        lines.append(f"fixable misses (rewriter missed, oracle hit): {len(fix)}")
        for f in fix:
            lines.append(f"  {f['id']}: prose={f['prose']}")
            lines.append(f"      rewriter gave {f['rewrite_query']!r} game={f['rewrite_game']}"
                         f"  |  oracle wants {f['oracle_keywords']!r} game={f['oracle_game']}")
    else:
        lines.append("fixable misses: none")
    return lines


def main(argv: list[str] | None = None, *, search=None) -> int:
    """Standalone entry point (``python -m`` / tests). Prints the report and returns the gate code."""
    try:
        result = evaluate(search=search)
    except RewriteEvalError as exc:
        print(f"COULD NOT EVALUATE: {exc}", file=sys.stderr)
        return 2
    for line in report_lines(result):
        print(line)
    if result["code"] == 2:
        print(f"COULD NOT EVALUATE: {result['reason']}", file=sys.stderr)
    elif result["code"] == 1:
        print(f"REWRITE EVAL FAILED: {result['reason']}", file=sys.stderr)
    return result["code"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
