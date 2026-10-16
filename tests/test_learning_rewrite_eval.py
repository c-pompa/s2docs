"""The rewrite evaluation gate: the rewriter's own scoreboard, in-repo and reproducible.

The rewriter exists because prose retrieves nothing (``db.fts`` ANDs every token). That claim was
*measured*, but the measurement lived in a scratch script and could not gate anything. This module is
that measurement promoted to a first-class gate: a frozen, in-repo eval set (20 rows + 7 known gaps),
three retrieval conditions, and an exit contract.

  baseline  = the user's raw prose, unscoped           (what happens today)
  rewrite   = the deterministic rewriter's query+game  (what we just built)
  oracle    = the curated keyword form, game-scoped     (upper bound: perfect rewriting)

The eval set is *non-circular*: every ``expect_key`` is backed by an ``evidence`` passage quoted from
the target document, so no expectation was derived from a search ranking. Expectations are frozen —
editing a row to make a score move is the exact failure this whole design exists to prevent.

Everything here is offline and deterministic: the search function is a module-level seam
(``_default_search``) that tests replace with canned hits, exactly like ``gate._search``.
"""

from __future__ import annotations

from s2docs.learning.rewrite import rewrite
from s2docs.learning.rewrite_eval import (
    FLOORS,
    decide,
    eval_set,
    main,
    score,
)

# --------------------------------------------------------------------- the eval set


def test_eval_set_is_frozen_and_complete():
    """The set ships as package data: 20 verified rows + 7 recorded known gaps, nothing invented."""
    data = eval_set()
    assert len(data["rows"]) == 20
    assert len(data["known_gaps"]) == 7
    ids = [r["id"] for r in data["rows"]]
    assert len(ids) == len(set(ids)), "duplicate row ids would double-count a condition"


def test_eval_rows_carry_their_evidence_so_they_are_not_circular():
    """Every row must cite a passage from the document that answers it; an expectation without
    evidence is a ranking, not a fact, and this gate refuses to be built on a ranking."""
    for row in eval_set()["rows"]:
        assert row["evidence"].strip(), f"{row['id']} has no evidence passage"
        assert row["source"].strip(), f"{row['id']} does not name where the question came from"


def test_a_known_row_is_unmodified():
    """A guard against silent edits: the first row's expectation is frozen. If someone changes an
    expectation to move a score, this fails loudly rather than passing quietly."""
    (row,) = [r for r in eval_set()["rows"] if r["id"] == "cs2-spectator-xray-walls"]
    assert row["expect_key"] == "spec_show_xray"
    assert row["expect_kind"] == "convar"
    assert row["game"] == "cs2"


# --------------------------------------------------------------------- scoring


def _rows():
    """Two synthetic rows whose prose reduces cleanly: 'what is alpha' -> 'alpha'."""
    return [
        {"id": "a", "prose": "what is alpha", "keywords": "alpha kw", "game": "cs2",
         "expect_key": "alpha", "expect_kind": "convar"},
        {"id": "b", "prose": "what is beta", "keywords": "beta kw", "game": "cs2",
         "expect_key": "beta", "expect_kind": "convar"},
    ]


def _fake(mapping):
    """A canned search keyed on the exact query string, so each condition is independently scored."""

    def search(query, game=None, limit=10):
        return [{"key": k} for k in mapping.get(query, [])]

    return search


def test_a_row_that_is_a_hit_scores_rank_one_in_every_condition():
    """A hit is a hit: the expected key is found, at rank 1, and recall@1 records it."""
    fake = _fake({
        "what is alpha": ["alpha"],          # baseline
        "alpha": ["alpha"],                  # rewrite
        "alpha kw": ["alpha"],               # oracle
    })
    s = score([_rows()[0]], search=fake)
    for name in ("baseline", "rewrite", "oracle"):
        assert s["conditions"][name]["recall@1"] == 1.0, name
        assert s["conditions"][name]["recall@5"] == 1.0, name
        assert s["conditions"][name]["mrr"] == 1.0, name


def test_a_row_that_is_a_miss_scores_zero_and_is_named():
    """A miss is a miss: the row is recorded with rank None, so the human report can name what failed."""
    fake = _fake({})  # nothing retrieves anything
    s = score([_rows()[0]], search=fake)
    assert s["conditions"]["rewrite"]["recall@5"] == 0.0
    assert s["conditions"]["rewrite"]["per"] == [{"id": "a", "rank": None}]


def test_per_condition_aggregation_and_mrr():
    """Each condition aggregates independently over the same rows, and MRR weights by rank: a rank-2
    hit is 1/2, a rank-1 hit is 1 — so the three conditions cannot share one score."""
    fake = _fake({
        "what is alpha": [],                 # baseline: miss
        "what is beta": [],                  # baseline: miss
        "alpha": ["alpha"],                  # rewrite: rank 1
        "beta": ["other", "beta"],           # rewrite: rank 2
        "alpha kw": ["alpha"],               # oracle: rank 1
        "beta kw": ["beta"],                 # oracle: rank 1
    })
    s = score(_rows(), search=fake)

    base, rw, orc = (s["conditions"][n] for n in ("baseline", "rewrite", "oracle"))
    assert (base["recall@1"], base["recall@5"], base["mrr"]) == (0.0, 0.0, 0.0)
    assert (rw["recall@1"], rw["recall@5"], rw["mrr"]) == (0.5, 1.0, 0.75)
    assert (orc["recall@1"], orc["recall@5"], orc["mrr"]) == (1.0, 1.0, 1.0)


def test_the_rewrite_condition_uses_the_rewriter_and_the_oracle_uses_curated_keywords():
    """The seam is observable: the rewrite condition searches the reduced prose + the rewriter's game,
    the oracle the curated keywords + the row's game. A fake that records its calls proves it."""
    calls = []

    def spy(query, game=None, limit=10):
        calls.append((query, game))
        return []

    score(_rows(), search=spy)
    assert ("what is alpha", None) in calls      # baseline: raw prose, unscoped
    assert ("alpha", None) in calls              # rewrite: reduced, game undetermined
    assert ("alpha kw", "cs2") in calls          # oracle: curated keywords, game-scoped


def test_fixable_misses_are_rewriter_misses_where_the_oracle_hits():
    """The report's most useful line: a row the oracle finds but the rewriter does not is fixable."""
    fake = _fake({
        "what is alpha": [],
        "what is beta": [],
        "alpha": [],                         # rewriter misses this one
        "beta": ["beta"],                    # rewriter hits this one
        "alpha kw": ["alpha"],               # oracle hits both
        "beta kw": ["beta"],
    })
    s = score(_rows(), search=fake)
    assert [f["id"] for f in s["fixable"]] == ["a"]
    assert s["fixable"][0]["rewrite_query"] == "alpha"
    assert s["fixable"][0]["oracle_keywords"] == "alpha kw"


# --------------------------------------------------------------------- the gate


def test_decide_passes_at_the_recorded_floors():
    """At or above every floor, the gate is a pass (code 0, no reason)."""
    scores = {"conditions": {"baseline": {"recall@5": 0.20},
                             "rewrite": {"recall@5": 0.40},
                             "oracle": {"recall@5": 1.00}}}
    code, reason = decide(scores)
    assert code == 0 and reason is None


def test_decide_flags_a_rewriter_regression_as_exit_one():
    """The rewriter dropping below its floor — while the oracle still hits — is a regression: exit 1,
    and the reason names the rewriter, not the corpus."""
    scores = {"conditions": {"baseline": {"recall@5": 0.20},
                             "rewrite": {"recall@5": 0.30},
                             "oracle": {"recall@5": 1.00}}}
    code, reason = decide(scores)
    assert code == 1
    assert reason is not None and "rewrite" in reason


def test_decide_flags_an_oracle_drop_as_could_not_evaluate():
    """An oracle drop means the corpus or the index changed underneath, not that the rewriter got
    worse — so it is could-not-evaluate (2), never a rewrite regression (1)."""
    scores = {"conditions": {"baseline": {"recall@5": 0.15},
                             "rewrite": {"recall@5": 0.40},
                             "oracle": {"recall@5": 0.80}}}
    code, reason = decide(scores)
    assert code == 2
    assert reason is not None and "oracle" in reason


def test_floors_are_the_recorded_baselines():
    """The floors are the measured 2026-10-16 numbers, recorded here so a change is a deliberate diff."""
    assert FLOORS == {"baseline": 0.20, "rewrite": 0.40, "oracle": 1.00}


# --------------------------------------------------------------------- main() end to end


def _oracle_only(search_returns_hit: bool):
    """A fake that hits ONLY the oracle's exact keyword queries (or only misses them), so the rewrite
    and baseline conditions stay at zero. Enough to drive main()'s exit paths offline."""
    kw = {r["keywords"]: r["expect_key"] for r in eval_set()["rows"]}

    def search(query, game=None, limit=10):
        if search_returns_hit and query in kw:
            return [{"key": kw[query]}]
        return []

    return search


def test_main_passes_exit_zero_when_the_rewriter_meets_its_floor(capsys):
    """End to end offline: with an oracle in control but the rewriter not regressed, main returns 0 and
    prints the three-condition table."""
    # A fake that scores the oracle 1.0 AND lets the rewrite condition meet its floor: reuse the real
    # rewriter's output as the hit key for each row.
    from s2docs.learning.rewrite import rewrite

    rows = eval_set()["rows"]
    rewrite_hits = {rewrite(r["prose"]).query: r["expect_key"] for r in rows}
    kw = {r["keywords"]: r["expect_key"] for r in rows}

    def search(query, game=None, limit=10):
        if query in kw:
            return [{"key": kw[query]}]
        if query in rewrite_hits:
            return [{"key": rewrite_hits[query]}]
        return []

    rc = main([], search=search)
    out = capsys.readouterr().out
    assert rc == 0
    assert "oracle" in out and "rewrite" in out and "baseline" in out


def test_main_exits_one_on_a_rewriter_regression(capsys):
    """Offline: oracle in control, rewriter at zero -> the regression path, exit 1, reason on stderr."""
    rc = main([], search=_oracle_only(True))
    err = capsys.readouterr().err
    assert rc == 1
    assert "rewrite" in err.lower()


def test_main_exits_two_when_the_oracle_drops(capsys):
    """Offline: nothing retrieves the oracle's curated keywords -> could-not-evaluate, exit 2 — a
    corpus/index change, and never a pass."""
    rc = main([], search=_oracle_only(False))
    err = capsys.readouterr().err
    assert rc == 2
    assert "COULD NOT EVALUATE" in err


# --------------------------------------------------------------------- the CLI verb


def _run_cli(monkeypatch, tmp_path, argv):
    from s2docs import cli
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    return cli.main(argv)


def test_cli_rewrite_eval_maps_a_could_not_evaluate_to_six(monkeypatch, tmp_path, capsys):
    """The load-bearing mapping: the module's own 2 (could-not-evaluate) must NOT surface as this
    repo's 2/EXIT_LOCKED — the CLI maps it to EXIT_PARTIAL (6), like `learning gate`."""
    from s2docs.learning import rewrite_eval

    monkeypatch.setattr(rewrite_eval, "_default_search", _oracle_only(False))
    rc = _run_cli(monkeypatch, tmp_path, ["learning", "rewrite-eval"])
    assert rc == 6
    assert "COULD NOT EVALUATE" in capsys.readouterr().err


def test_cli_rewrite_eval_treats_an_unreadable_index_as_could_not_evaluate(monkeypatch, tmp_path, capsys):
    """A transport failure is could-not-evaluate (mapped to 6), never a stack trace or a pass — an
    unreachable index must not be scored as a pile of misses."""
    from s2docs.learning import rewrite_eval

    def boom(query, game=None, limit=10):
        raise rewrite_eval.RewriteEvalError("index unreachable")

    monkeypatch.setattr(rewrite_eval, "_default_search", boom)
    rc = _run_cli(monkeypatch, tmp_path, ["learning", "rewrite-eval"])
    assert rc == 6
    assert "COULD NOT EVALUATE" in capsys.readouterr().err


def test_cli_rewrite_eval_human_and_json(monkeypatch, tmp_path, capsys):
    """A pass prints the three-condition table (exit 0); --json returns the same numbers as one
    object on stdout."""
    import json as _json

    from s2docs.learning import rewrite_eval

    kw = {r["keywords"]: r["expect_key"] for r in rewrite_eval.eval_set()["rows"]}
    rw = {rewrite(r["prose"]).query: r["expect_key"] for r in rewrite_eval.eval_set()["rows"]}

    def search(query, game=None, limit=10):
        if query in kw:
            return [{"key": kw[query]}]
        if query in rw:
            return [{"key": rw[query]}]
        return []

    monkeypatch.setattr(rewrite_eval, "_default_search", search)
    assert _run_cli(monkeypatch, tmp_path, ["learning", "rewrite-eval"]) == 0
    assert "oracle" in capsys.readouterr().out

    assert _run_cli(monkeypatch, tmp_path, ["learning", "rewrite-eval", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["schema"] == "s2docs/v1"
    assert payload["rewrite_eval"]["conditions"]["oracle"]["recall@5"] == 1.0
