"""Trivial rewriter implementations used as ``--rewriter`` import targets in tests.

The gate resolves a supplied rewriter by import path (``pkg.module:function``), so the tests need a
real, importable module rather than a local lambda. Each function honours the rewriter contract
(``f(text: str) -> Rewrite``) and pins exactly one behaviour:

  * ``echo_prose`` — a *perfect* stand-in: it echoes the (lowercased) prose as the query, which the
    tests' canned search maps straight onto the expected key, so the ``model`` condition scores 1.0.
  * ``junk`` — returns a query nothing can match, so the ``model`` condition scores 0.0 (below the
    deterministic floor) and the gate must fail it as a regression.
  * ``boom`` — raises, standing in for a model that cannot run: the gate must treat that as
    *could not evaluate*, never a pass and never a traceback.
"""

from __future__ import annotations

from s2docs.learning.rewrite import Rewrite


def echo_prose(text: str) -> Rewrite:
    """A perfect stand-in rewriter: the lowercased prose is itself the query (no game guess)."""
    return Rewrite(query=" ".join((text or "").lower().split()), game=None, dropped=(), notes=())


def junk(text: str) -> Rewrite:
    """A rewriter that produces an unmatchable query — always below the floor."""
    return Rewrite(query="zzz nothing matches this", game=None, dropped=(), notes=())


def boom(text: str) -> Rewrite:
    """A rewriter that cannot run (model unavailable) — the gate must record could-not-evaluate."""
    raise RuntimeError("fake model inference exploded")
