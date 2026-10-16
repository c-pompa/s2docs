# Training handoff — s2docs artifacts → the cpompa-ai-cloud training labs

The loop measures and exports; the labs train. This file is the contract between them.
**Whoever runs the labs owns them** — the training side lives in `cpompa-ai-cloud`, not here.

Produced by `uv run s2docs learning export` into `$S2DOCS_DATA_DIR/learning/export/`:

| file | consumer | shape |
|---|---|---|
| `corpus.txt` | `lab/lab4_distill/distill.py` — drop-in replacement for `lab/lab4_distill/corpus.txt` | plain text, one `## job <id> — <title>` block per approved run (request / proved / learned / next). The lab chunks it at `seq_len` (256) and holds the last 5 chunks out as val, so it needs ≥ 8 full chunks of ~1 KB each to run at all. |
| `problems.json` | `lab/lab5_grpo` | `[{"problem", "answer", "checker", "job_id", "evidence_ref"}]`. Every entry carries a checker, because an answer nothing can verify is a preference pair in disguise. |
| `golden_cases.jsonl` | `scripts/eval/golden_set.jsonl` (then `split_golden.py`) | `{"id","category","prompt","checker","expected"}` |

## Why the reward is a validator, not a model

lab-5 needs a checker that decides correctness without a model — its `rfs_grading.reward_rlvr` grades
against exact ground truth for the same reason. s2docs already ships exactly those checkers:

| checker | decides | source of truth |
|---|---|---|
| `s2docs_script` | the answer is a cs_script that type-checks | the shipped `point_script.d.ts`, via `validate_cs_script` |
| `s2docs_cfg` | the answer is a real console command/variable | the indexed convar/command set, via `validate_cfg` |
| `s2docs_lookup` | the answer is an identifier that exists | the FTS index (`Repository.search`) |
| `exact` | the answer equals the expected string (normalised) | the case itself |

No LLM judge, no preference pairs, no reward model. An unknown checker raises rather than defaulting,
because a silent default would make every problem look passed.

## Rules for whoever runs the labs

- **Copy the artifacts into the cpompa-ai-cloud checkout.** Never train from a live `$S2DOCS_DATA_DIR`
  path — that directory belongs to a running server.
- **Split before training.** `python3 scripts/eval/split_golden.py` refuses to overwrite an existing
  split; run it once per golden-set revision, and treat the holdout as consumed. It is a pure function
  of (input, seed) — never regenerate it because a result was disappointing.
- **Report a before and an after, next to a gate verdict.** lab-5's `before_mean` / `after_mean` and
  lab-4's `result.json` KL trajectory without a gate line are numbers, not results. A fine-tune
  without a gate verdict is not evidence of anything.
- **Promotion is a human action.** The labs measure; `rolectl` / the router config applies. Never let
  a script promote a model, and never widen the router's candidate list from a lab run.
- **Keep the corpus behavioural.** Facts stay in the index. If a lab's corpus starts carrying API
  facts, the model learns to answer from memory and stops calling `search_docs` — which is the whole
  value of the thing.

## Ordering

1. `s2docs learning harvest` → the four artifacts exist (`docs/LEARNING_LOOP.md` has the definitions).
2. `s2docs learning gate` → a baseline in `eval_history.jsonl`. **No fine-tune before this** — without
   a baseline there is no way to show the training helped, and `sft.prep(require_baseline=True)`
   refuses.
3. `s2docs learning sft` → the dataset + train config (and an honest readiness number).
4. `s2docs learning export` → this document's three files.
5. lab-4 distill → lab-5 GRPO → the labs' own `run_gate.sh` and the s2docs gate, both re-run.
