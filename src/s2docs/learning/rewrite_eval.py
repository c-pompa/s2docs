"""The rewrite evaluation gate: measure the deterministic rewriter, in-repo and reproducibly.

``rewrite.py`` exists because ``db.fts`` ANDs every token, so a real user question retrieves nothing.
That claim was measured — but the measurement lived in a scratch script, so it was neither reviewable
nor able to gate anything. This module promotes it to a first-class gate over a frozen, in-repo eval
set (``data/rewrite_eval.json``, 20 verified rows + 7 recorded known gaps).

Three retrieval conditions are scored over the same rows:

    baseline  = the user's raw prose, unscoped            (what happens today)
    rewrite   = the deterministic rewriter's query+game   (what we built)
    oracle    = the curated keyword form, game-scoped      (upper bound: perfect rewriting)

A *fourth* condition, ``model``, joins when a caller supplies its own rewriter — the seam a fine-tune
is scored through. ``score``/``evaluate``/``main`` take a ``rewriter`` callable (``f(text) -> Rewrite``)
and the CLI exposes it as ``--rewriter pkg.module:function``. The supplied callable is scored as an
additional condition; the ``rewrite`` condition keeps measuring the built-in deterministic rewriter, so
the two can be compared directly on the same frozen rows.

**Acceptance rule.** With a supplied rewriter the exit code is decided on the *model's* score, and the
model's floor is the deterministic rewriter's own recall@5 (0.40). A model must **beat** the
deterministic rewriter to be adopted — equal-to is not beating it, so that comparison is strict. When
no rewriter is supplied, behaviour is exactly as before: the gate is decided on the deterministic
``rewrite`` condition.

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
from collections.abc import Callable
from importlib import import_module, resources
from typing import cast

from s2docs.config import DEFAULT_PUBLIC_URL, get_settings
from s2docs.learning.rewrite import Rewrite, rewrite

__all__ = ["FLOORS", "K", "RewriteEvalError", "eval_set", "load_rewriter", "score", "decide",
           "evaluate", "report_lines", "main"]

# The rewriter seam: anything that turns prose into a ``Rewrite`` (the deterministic function, or a
# trained model). Named here so the pluggable parameters read the same everywhere.
Rewriter = Callable[[str], Rewrite]

# The measured baselines, recorded as floors. Re-measured on the 45-row set against the live index
# (http://127.0.0.1:8812/api/search), recall@5: baseline (prose, unscoped) 0.11, deterministic
# rewrite (query+game) 0.27, oracle (curated keywords, game-scoped) 0.82. These are *floors*: at or
# above is a pass. Changing one is a deliberate diff, never a way to make a failing run green.
#
# They were re-measured because the set grew from 20 to 45 rows and EVERY condition moved down:
# the additions are deliberately harder (more Deadlock, more class/identifier rows), so carrying the
# old numbers forward would have failed the gate on a set it was never measured on. Note the oracle
# is 0.82, not 1.00 — ~18% of the expanded set is not retrievable even with hand-picked keywords,
# which is a property of the rows (opaque class names, thin documents), not of the rewriter. That
# ceiling is why "the ORACLE dropped" must stay a could-not-evaluate outcome rather than a
# regression: with a hard set, an oracle floor is a corpus-integrity canary, not a target.
FLOORS: dict[str, float] = {"baseline": 0.11, "rewrite": 0.27, "oracle": 0.82}

K = 5  # the rank window every floor is measured over (recall@1, recall@5, MRR)
_TIMEOUT = 15

_LABELS: dict[str, str] = {
    "baseline": "baseline (prose, unscoped)",
    "rewrite": "rewrite (deterministic)",
    "oracle": "oracle (curated kw, scoped)",
    "model": "model (supplied rewriter)",
}


class RewriteEvalError(RuntimeError):
    """Could not evaluate: a declared input (the eval set, or the live index) was missing or
    unreadable — an error, never a pass. The analogue of ``gate.GateError``."""


def load_rewriter(spec: str) -> Rewriter:
    """Resolve ``'pkg.module:function'`` to a rewriter callable, or raise ``RewriteEvalError``.

    Lazy and total: a bad spec, an unimportable module, a missing attribute, a non-callable, or an
    exception raised *inside* the callable all become a ``RewriteEvalError`` (could-not-evaluate). A
    model that is unavailable must never be scored as a pile of zeros and reported as a pass — it is
    'could not evaluate', and the CLI maps that to exit 6.
    """
    if ":" not in spec:
        raise RewriteEvalError(f"--rewriter must be 'module:function', got {spec!r}")
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise RewriteEvalError(f"--rewriter must be 'module:function', got {spec!r}")
    try:
        module = import_module(module_name)
    except Exception as exc:                                  # ImportError, or a module that fails to run
        raise RewriteEvalError(f"cannot import --rewriter module {module_name!r}: {exc}") from exc
    fn = getattr(module, attr, None)
    if fn is None:
        raise RewriteEvalError(f"--rewriter {spec!r}: module {module_name!r} has no {attr!r}")
    if not callable(fn):
        raise RewriteEvalError(f"--rewriter {spec!r} resolved to {type(fn).__name__}, not a callable")
    return cast(Rewriter, fn)


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


def _model_condition(rewriter: Rewriter):
    """The fourth condition: the supplied rewriter, with its own exceptions recast as
    ``RewriteEvalError`` so a model that cannot run is could-not-evaluate, not a crash or a zero."""
    def cond(row):
        try:
            rw = rewriter(row["prose"])
        except RewriteEvalError:
            raise
        except Exception as exc:
            raise RewriteEvalError(f"supplied rewriter raised on row {row['id']!r}: {exc}") from exc
        return rw.query, rw.game

    return cond


def score(rows: list[dict], *, search=None, k: int = K, rewriter: Rewriter = rewrite) -> dict:
    """Score the conditions over ``rows`` and return per-condition metrics + the fixable misses.

    ``search`` is the injectable seam (default ``_default_search``); tests pass canned hits so this
    runs with no server. ``rewriter`` is the pluggable rewriter seam; it *defaults to the built-in
    deterministic* ``rewrite``, so omitting it changes nothing. Pass a different callable (a trained
    model, say) to score it as an additional ``model`` condition alongside the deterministic
    ``rewrite`` condition — the two are then directly comparable on the same rows.

    Returns ``{"n", "conditions": {name: {recall@1, recall@5, mrr, per}}, "fixable": [...],
    "fixable_model": [...]}`` where ``fixable`` describes the built-in deterministic rewriter and
    ``fixable_model`` (non-empty only when a rewriter was supplied) describes that rewriter, each
    drawn from its own scored ranks and carrying its own emitted query.
    ``per`` is one ``{"id","rank"}`` per row (rank ``None`` = miss)."""
    do_search = search if search is not None else _default_search
    n = len(rows)
    conds = _conditions()
    if rewriter is not rewrite:                 # an explicitly supplied model joins as a fourth condition
        conds["model"] = _model_condition(rewriter)
    conditions: dict[str, dict] = {}
    ranks: dict[str, dict[str, int | None]] = {}
    for name, cond in conds.items():
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

    # The most useful line for a human: a row the oracle finds and a rewriter does not is fixable.
    #
    # FILL EVERY LIST FROM THE CONDITION THAT WAS SCORED -- both the rows and the query shown.
    # Reading the module-level ``rewrite`` (the built-in deterministic one) while the ranks came from
    # ``conds["model"]`` is silent and badly misleading: a supplied fine-tune gets its misses listed
    # next to another rewriter's queries, so the debugging session reads prose echoes like
    # "spectators see enemy player outlines xray" and concludes the MODEL emitted them. It did not --
    # that was the deterministic rewriter. Hence two lists, each labelled with its own condition.
    def fixable_for(name: str, *, with_notes: bool) -> list[dict]:
        out = []
        for row in rows:
            if ranks[name][row["id"]] is not None or ranks["oracle"][row["id"]] is None:
                continue
            if with_notes:
                # the very callable scored as this condition, so notes belong to these ranks
                rw = rewriter(row["prose"])
                query, game, notes = rw.query, rw.game, tuple(rw.notes)
            else:
                query, game = conds[name](row)      # exactly what this condition searched
                notes = ()
            out.append({
                "id": row["id"],
                "prose": row["prose"],
                "expect_key": row["expect_key"],
                "rewrite_query": query,
                "rewrite_game": game,
                "rewrite_notes": list(notes),
                "oracle_keywords": row["keywords"],
                "oracle_game": row.get("game"),
            })
        return out

    fixable = fixable_for("rewrite", with_notes=False)
    fixable_model = fixable_for("model", with_notes=True) if "model" in conds else []
    return {"n": n, "conditions": conditions, "fixable": fixable, "fixable_model": fixable_model}


def decide(scores: dict, *, floors: dict[str, float] | None = None) -> tuple[int, str | None]:
    """The gate: ``(exit_code, reason)``. Oracle first — it is the corpus canary.

    With no supplied rewriter this is decided on the deterministic ``rewrite`` condition (at or above
    its floor passes). With a supplied rewriter the gate is decided on the ``model`` condition, whose
    floor is the deterministic rewriter's recall@5: **a model must beat the deterministic rewriter to
    be adopted, and equal-to is not beating it** — so that comparison is strict (``<=`` fails)."""
    f = floors or FLOORS
    conds = scores["conditions"]
    oracle = conds["oracle"]["recall@5"]
    if oracle < f["oracle"]:
        return 2, (f"oracle recall@5 {oracle:.0%} < floor {f['oracle']:.0%} — the corpus or the index "
                   "changed underneath; the rewriter cannot be judged")
    if "model" in conds:
        model = conds["model"]["recall@5"]
        det = conds["rewrite"]["recall@5"]
        floor = f["rewrite"]
        if model <= floor:
            return 1, (f"model recall@5 {model:.0%} <= deterministic floor {floor:.0%} "
                       f"(deterministic rewriter scored {det:.0%}) — a model must BEAT the "
                       "deterministic rewriter to be adopted; equal is not beating it")
        return 0, None
    rw = conds["rewrite"]["recall@5"]
    if rw < f["rewrite"]:
        return 1, f"rewrite recall@5 {rw:.0%} < floor {f['rewrite']:.0%}"
    return 0, None


def evaluate(*, search=None, rows: list[dict] | None = None, rewriter: Rewriter | None = None) -> dict:
    """Score the eval set and apply the gate. A JSON-friendly result for the CLI.

    ``rewriter`` is the optional pluggable rewriter (``f(text) -> Rewrite``). ``None`` keeps the
    three-condition behaviour; a callable adds the fourth ``model`` condition and makes the gate
    decide on it."""
    data = eval_set()
    rws = rows if rows is not None else data["rows"]
    scores = score(rws, search=search, rewriter=rewriter if rewriter is not None else rewrite)
    code, reason = decide(scores)
    floors = dict(FLOORS)
    if "model" in scores["conditions"]:            # the model's floor is the deterministic floor
        floors["model"] = FLOORS["rewrite"]
    return {
        "code": code,
        "reason": reason,
        "n": len(rws),
        "known_gaps": len(data["known_gaps"]),
        "conditions": {name: {kk: vv for kk, vv in metrics.items() if kk != "per"}
                       for name, metrics in scores["conditions"].items()},
        "floors": floors,
        "fixable": scores["fixable"],
        "fixable_model": scores["fixable_model"],
    }


def report_lines(result: dict) -> list[str]:
    """The human report: the condition table (three rows, or four with a supplied rewriter), then the
    fixable misses (rewriter missed, oracle hit). Also used by the CLI, so the human view has exactly
    one definition."""
    lines = [f"rewrite-eval: {result['n']} rows + {result['known_gaps']} known gaps", ""]
    lines.append(f"{'condition':32} {'recall@1':>9} {'recall@5':>9} {'MRR':>7}   floor")
    for name, label in _LABELS.items():
        if name not in result["conditions"]:       # only render conditions actually scored
            continue
        c = result["conditions"][name]
        lines.append(f"{label:32} {c['recall@1']:9.0%} {c['recall@5']:9.0%} {c['mrr']:7.3f}"
                     f"   {result['floors'][name]:.0%}")
    lines.append("")
    # Two sections, because the two are different rewriters and conflating them misleads. The
    # deterministic section always prints (it is the floor); the supplied-rewriter section prints
    # only when a model was actually scored, and then it is the one a fine-tune is debugged with.
    def _section(title: str, rows_: list[dict], who: str) -> None:
        if not rows_:
            return
        lines.append(f"{title}: {len(rows_)}")
        for f in rows_:
            lines.append(f"  {f['id']}: prose={f['prose']}")
            lines.append(f"      {who} gave {f['rewrite_query']!r} game={f['rewrite_game']}"
                         f"  |  oracle wants {f['oracle_keywords']!r} game={f['oracle_game']}")

    fix = result["fixable"]
    if fix:
        _section("fixable misses (DETERMINISTIC rewriter missed, oracle hit)", fix, "rewriter")
    else:
        lines.append("fixable misses (DETERMINISTIC rewriter missed, oracle hit): none")
    fix_model = result.get("fixable_model") or []
    if "model" in result["conditions"]:
        lines.append("")
        if fix_model:
            _section("fixable misses (SUPPLIED rewriter missed, oracle hit)", fix_model,
                     "supplied rewriter")
        else:
            lines.append("fixable misses (SUPPLIED rewriter missed, oracle hit): none")
    return lines


def main(argv: list[str] | None = None, *, search=None, rewriter: Rewriter | None = None) -> int:
    """Standalone entry point (``python -m`` / tests). Prints the report and returns the gate code.

    ``rewriter`` is the optional pluggable rewriter; a callable raised from it surfaces here as
    ``COULD NOT EVALUATE`` (code 2) rather than a traceback."""
    try:
        result = evaluate(search=search, rewriter=rewriter)
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
