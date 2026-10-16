"""The verifiable reward: decide a Source 2 answer with a checker, not a model.

This is the training signal for the small local Source 2 specialist model (cpompa-ai-cloud
`lab/lab5_grpo`). It is deliberately the same shape as that lab's `rfs_grading.grade_answer`: an
exact ground-truth grader over a candidate answer — no learned reward model, no preference pairs,
no judge. `grade()` is pure and model-free: it makes no network or model call and is deterministic,
so the loop's measurement stage is inspectable and reproducible.

Every checker delegates to a validator this repo already ships and already exposes over the CLI
(`validate-script`, `validate-cfg`) and MCP:

    s2docs_lookup  the answer resolves, in the index, to the expected identifier
    s2docs_cfg     the answer validates as console commands against the indexed convars/commands
    s2docs_script  the answer type-checks as a cs_script against point_script.d.ts (tsc)
    exact          the answer equals the expected string (normalised)

If you cannot state the checker, you do not have a training problem — you have an opinion. An
unknown checker raises (`UnknownChecker`) rather than returning a default: a silent default would
make every problem look passed, and a reward nothing has ever seen fail is decoration.
"""

from __future__ import annotations

from dataclasses import dataclass

# The closed set. `checker_for` only ever emits members of this tuple; `grade` refuses anything else.
CHECKERS: tuple[str, ...] = ("s2docs_lookup", "s2docs_cfg", "s2docs_script", "exact")

# Default game whose index is the oracle. The canonical deployment indexes cs2 and deadlock.
DEFAULT_GAME = "cs2"


class UnknownChecker(ValueError):
    """A checker name outside `CHECKERS`. Raised, never defaulted — see the module docstring."""


@dataclass(frozen=True, slots=True)
class Check:
    """A mechanically checkable expectation: which checker, and the ground truth it compares to."""

    name: str
    expected: str


def checker_for(row: dict) -> Check | None:
    """Map an evidence row to a checker, or None when the evidence is not mechanically checkable.

    Priority is deliberate: a script reference that carries an expected exit code is a script
    problem even if it also carries other hints. A row with none of the recognised keys is not a
    training problem here — the caller (`export.problems`) skips it rather than inventing a reward.
    """
    ref = row.get("ref") or ""
    data = row.get("data") or {}
    if ref.endswith((".js", ".vjs")) and "expect_exit" in data:
        return Check("s2docs_script", str(data["expect_exit"]))
    if data.get("expect_identifier"):
        return Check("s2docs_lookup", str(data["expect_identifier"]))
    if data.get("expect_cfg"):
        return Check("s2docs_cfg", str(data["expect_cfg"]))
    if data.get("expect_exact"):
        return Check("exact", str(data["expect_exact"]))
    return None


def _norm(text: str) -> str:
    """Case- and whitespace-insensitive comparison; the only normalisation the reward applies."""
    return " ".join((text or "").split()).strip().casefold()


def grade(answer: str, expected: str, *, checker: str, db=None, game: str = DEFAULT_GAME) -> bool:
    """Run one checker over a candidate `answer`. No model, no network — deterministic only.

    `expected` is the ground truth the checker compares against (an exit code, an identifier, a cfg
    line, or an exact string — whatever `checker_for` recorded). `db` is required by the three
    index-backed checkers, because the index is the oracle; a missing index is an error, not a pass.
    """
    if checker not in CHECKERS:
        raise UnknownChecker(f"unknown checker {checker!r}; expected one of {CHECKERS}")

    if checker == "exact":
        return _norm(answer) == _norm(expected)

    if db is None:
        raise ValueError(f"{checker} needs a db (the index is the oracle)")

    from s2docs.db.repository import Repository

    if checker == "s2docs_lookup":
        # The answer is resolved through the same search the tools expose; it passes only when the
        # expected identifier is what the index returns for it.
        with db.reader() as conn:
            hits = Repository(conn).search(answer.strip(), game=game, limit=5).items
        return any(_norm(getattr(h, "key", "")) == _norm(expected) for h in hits)

    if checker == "s2docs_cfg":
        # Delegate to the shipped cfg validator: unknown commands, out-of-range values and context
        # mistakes are its job, not ours. A clean validation is necessary but not sufficient — the
        # answer must also carry the expected command.
        from s2docs.build.validate_cfg import validate_cfg

        with db.reader() as conn:
            result = validate_cfg(Repository(conn), answer, game=game)
        if not result.ok:
            return False
        return set(expected.split()) <= set(answer.split())

    if checker == "s2docs_script":
        # Delegate to the shipped tsc validator. Its CLI contract is exit 0 when the script is clean
        # and 1 when it has errors (see `cli._cmd_validate_script`); `expected` is that exit code.
        from s2docs.build.validate_script import validate_cs_script

        with db.reader() as conn:
            result = validate_cs_script(Repository(conn), answer, game=game, filename="script.js")
        return ("0" if result.ok else "1") == str(expected)

    raise UnknownChecker(f"checker {checker!r} is declared but not implemented")
