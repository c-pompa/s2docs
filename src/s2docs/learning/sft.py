"""SFT prep: build the dataset and the training config; train nothing.

    uv run s2docs learning sft        # writes <data_dir>/learning/sft/{sft_dataset.jsonl,train_config.json}

Two gates, both deliberate:

  * **The baseline gate** — ``require_baseline=True`` refuses to prepare without a recorded eval run
    (``eval_history.jsonl`` with at least one row), because without a baseline there is no way to show
    the fine-tune helped. Enforced by :class:`SftGateError`.
  * **The readiness floor** — ``MIN_EXAMPLES = 500``. Below it the honest answer is "use few-shot
    prompting", so :func:`report` says so instead of implying a target nobody should chase.

The dataset's assistant turns are placeholders by design (see ``harvest.sft_pairs``): the target text
is produced by re-running the job in a frozen environment, never by copying an old transcript — a
stale answer teaches a stale API, so the row count is a readiness number, not a training-set size.

The config names the **real** fleet target (cpompa-ai-cloud lab4 distill -> lab5 GRPO), not a
placeholder — a config with `<fill: the model>` in it is not a handoff. See
``docs/learning/TRAINING_HANDOFF.md`` for the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs.db.connection import Database
from s2docs.learning import gate as gate_mod
from s2docs.learning import harvest, learning_dir

__all__ = ["MIN_EXAMPLES", "TARGET_EXAMPLES", "SftGateError", "prep", "report", "config", "dataset_path"]

# Below this, few-shot prompting beats fine-tuning; above it, it is worth a distill run.
MIN_EXAMPLES = 500
# A healthy corpus size once the loop has real usage behind it — reported, never a target to pad to.
TARGET_EXAMPLES = 2000

# The real training target. Hard-coded on purpose: a handoff that says "<fill: the model>" hands the
# lab nothing, and the fleet model is a decision, not a per-run guess.
BASE_MODEL = "Qwen/Qwen3-0.6B"
FRAMEWORK = "cpompa-ai-cloud lab4 (LoRA distill) -> lab5 (GRPO)"
RUN_COMMAND = "cd <cpompa-ai-cloud>/lab && uv run python -m lab4_distill.distill"
HANDOFF_DOC = "docs/learning/TRAINING_HANDOFF.md"


class SftGateError(RuntimeError):
    """A gate refused to prepare a run — never a warning, so no trainer can ignore it."""


def _dir() -> Path:
    d = learning_dir() / "sft"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dataset_path() -> Path:
    return _dir() / "sft_dataset.jsonl"


def config_path() -> Path:
    return _dir() / "train_config.json"


def config() -> dict:
    """The train config for the real target. `dataset` points at the export corpus lab4 consumes."""
    corpus = learning_dir() / "export" / "corpus.txt"
    return {
        "base_model": BASE_MODEL,
        "framework": FRAMEWORK,
        "method": "lora",
        "lora_rank": 16,
        "learning_rate": 1e-4,
        "epochs": 3,
        "where": "home-pc (RTX 3080, WSL2)",
        "command": RUN_COMMAND,
        "dataset": str(corpus),
        "eval": "uv run s2docs learning gate   # must not regress vs eval_history.jsonl",
        "gate": f"do not train below {MIN_EXAMPLES} examples; target {TARGET_EXAMPLES}",
        "handoff_doc": HANDOFF_DOC,
        "notes": ("Behaviour/format only. Facts stay in the index — a fine-tune must never carry "
                  "API facts."),
    }


def prep(db: Database, *, origin: str = "human", since_days: int | None = None,
         require_baseline: bool = False) -> dict:
    """Write the dataset and the train config. Returns ``{"examples", "dataset", "config"}``.

    ``require_baseline=True`` raises :class:`SftGateError` when no eval run has been recorded — the
    baseline is what makes the fine-tune's effect showable.
    """
    if require_baseline and gate_mod.last_good_recall() is None:
        raise SftGateError(
            "no eval baseline recorded — run `uv run s2docs learning gate` first; without a baseline "
            "there is no way to show the fine-tune helped")
    pairs = harvest.sft_pairs(db, origin=origin, since_days=since_days)
    dataset = dataset_path()
    dataset.write_text("".join(json.dumps(p) + "\n" for p in pairs), encoding="utf-8")
    cfg = config_path()
    cfg.write_text(json.dumps(config(), indent=2), encoding="utf-8")
    return {"examples": len(pairs), "dataset": str(dataset), "config": str(cfg)}


def report(db: Database) -> dict:
    """Readiness numbers for the training side. Rule 5: every number ships its sample and its gate."""
    dataset = dataset_path()
    n = sum(1 for _ in dataset.open()) if dataset.exists() else 0
    baseline = gate_mod.last_good_recall()
    ready = n >= MIN_EXAMPLES
    return {
        "dataset_examples": n,
        "min_examples": MIN_EXAMPLES,
        "target_examples": TARGET_EXAMPLES,
        "ready_to_train": ready,
        "eval_baseline_recall": baseline,
        "gate": "eval baseline recorded before training; the gate must not regress after",
        "honest_note": (
            f"below the {MIN_EXAMPLES}-example minimum, few-shot prompting beats fine-tuning — this "
            "number is a readiness check, not a goal" if not ready else
            "enough approved runs to attempt a distill; keep the gate verdict next to the result"),
    }
