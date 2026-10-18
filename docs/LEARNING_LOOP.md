# The s2docs learning loop — the operator's rules

What turns finished runs into eval and training signal, and the rules that keep the artifacts
trustworthy. Read this before changing anything under `src/s2docs/learning/`.

North star: a small local specialist that **builds and maintains Source 2 games** for us — the s2docs
index is its tool, the job ledger is its outcome record, and this loop is how it gets better.

## The stages

```
run happens ─▶ jobs.enqueue(origin=human|agent|driver|eval|probe|warmup)
            ─▶ human verdicts (evidence kind='human-confirm')
            ─▶ jobs.finish(status, {what, why, learned, next})
            ─▶ data/job_events.jsonl + s2docs.db            (append-only)
                     │
   s2docs learning harvest ─▶ sft_pairs.jsonl · corrections.jsonl · hard_negatives.jsonl · gap_report.json
                     │
   s2docs learning digest  ─▶ signals.jsonl · reports/digest-<date>.md · lessons.json   (a work queue)
                     │
   s2docs learning gate    ─▶ eval_history.jsonl + exit 0 pass / 1 regression / 2 could-not-evaluate
                     │
   s2docs learning sft     ─▶ sft/sft_dataset.jsonl + train_config.json   (reads the gate's floor)
                     │
   s2docs learning export  ─▶ export/{corpus.txt, problems.json, golden_cases.jsonl}  ─▶ cpompa-ai-cloud labs
                     │
   s2docs learning review  ─▶ a human answers a correction ─▶ a new frozen golden case
```

Every stage writes files a person can read and delete. **No stage calls a model.** The server
measures; the agent layer reasons. That split is what makes an artifact inspectable — you can read
which rule produced which row.

Artifacts live in `$S2DOCS_DATA_DIR/learning/`. Nothing there is checked in.

## The six rules

Ported from ask-hittle's `docs/LEARNING_LOOP.md`, where each one was paid for by a bad artifact.

1. **Never infer intent from a proxy.** Read the recorded status or verdict — never an answer length,
   never an event count. A length rule conflates "refused honestly", "produced nothing" and "never
   ran", and it inflated one gap report by ~5×.
2. **Default to real usage.** Every reader scopes to `origin='human'`. Reading everything is an
   explicit `origin=None` in the calling code, with a reason.
3. **Tag synthetic callers at intake.** Tooling sends `driver`, the golden-question suite `eval`,
   diagnostics `probe`, the post-deploy smoke `warmup`. An unknown value is rejected at intake, so a
   typo cannot silently become valid.
4. **One definition per concept.** `src/s2docs/learning/` holds the only implementations of "what did
   the human say", "what did this run prove", "was judging possible". A second implementation drifts.
5. **Every number ships its sample.** Rows, origin, window. "No gaps" over nine jobs means *not
   enough usage*, not *complete coverage*.
6. **A skip is not a pass.** An unreadable source raises (`LearningSourceMissing`) or exits 2. A
   digest reporting "0 signals" because it could not read its input reads as a quiet week.

## Why provenance came first

Before migration 0014 a job had no `origin`, so a run a person asked for and watched was
indistinguishable from driver/eval/probe traffic. Building a training set on that ledger would have
produced an artifact describing *the test suite*. ask-hittle hit exactly this: its hard-negatives
artifact exported 163 rows of which 58 were real, and the fix was provenance, not filtering.

Two consequences to keep:

- **Pre-0014 rows are `agent`.** They were never tagged; guessing which of them a person asked for
  would be inventing data. A smaller honest sample beats a larger guessed one.
- **`origin` is validated in Python at intake, not by a CHECK constraint** (SQLite cannot add one via
  ALTER). Every writer goes through `jobs.enqueue`; a direct SQL write could store junk. Accepted.

## Artifact definitions

| artifact | definition | consumer |
|---|---|---|
| `sft_pairs.jsonl` | `passed` jobs where every recorded verdict is `pass` and `summary.learned` is non-empty, deduped by `target`, newest wins | behaviour/tone fine-tuning (lab-4 distill corpus) |
| `corrections.jsonl` | `failed`/`error`/`cancelled`/`inconclusive` runs, `needs_human_label: true` | a **queue** — becomes a golden case only once a human writes the expectation |
| `hard_negatives.jsonl` | evidence a step was gated on, in runs where a verdict existed **and** the step was failed, repeated ≥3 times | checkers/artifacts that are wrong, not the corpus |
| `gap_report.json` | runs nothing could ground, split into `true_gaps` vs `likely_retrieval_misses` | what to index next — and what *not* to write docs for |

`corrections` is a queue, not a dataset: an unlabeled correction scored as if it were a case is how an
eval set starts lying. `hard_negatives` counts only rows where judging was *possible* — count a
no-verdict run and every artifact in a driver run looks like a distractor.

## The gate

`data/learning/golden_questions.json` is **frozen**. A trap question must return *nothing* (the index
must not invent an answer), retrieval cases carry a `paraphrase` whose invariance is measured, and
every run appends to `eval_history.jsonl`.

- Exit `0` pass · `1` regression (recall under the floor, below the last good run, a trap or
  paraphrase failure) · `2` **could not evaluate** (too few cases, unreadable index) — never a pass.
- `s2docs learning gate --self-test` plants an impossible expectation and requires the gate to fail.
  A gate nobody has seen fail is decoration. **CI runs only this**; the full gate needs an indexed
  data directory and runs from cron.
- **Never edit an expectation to make a failing case pass.** Add a new question instead. Same for a
  threshold a regression is about to trip.

## Fine-tuning is a gate, not a pipeline

`s2docs learning sft` builds the dataset and the train config and refuses to be useful below the
minimum example count, and `require_baseline=True` refuses without a recorded gate run — otherwise
there is no way to show the fine-tune helped. Below the minimum, few-shot prompting beats
fine-tuning; the report says so rather than implying a target.

The corpus is **behaviour, not facts**. The facts already live in the index and stay there
(ask-hittle: "SFT for tone/format only. Facts stay in RAG"). A distill corpus of API facts would teach
the model to answer from memory instead of calling `search_docs` — the opposite of a tool-using
specialist.

## The human side (HITL)

- **Approved items become exemplars; contradicted ones never do.** A confirmation whose job recorded
  an unmet gate, a missing artifact or a late verdict is skipped, because an exemplar is the retrieval
  source for "what does a good run look like" and one wrong lesson in it poisons the rest.
- **The queue is drained by a person.** `s2docs learning review` lists unlabeled corrections;
  promoting one files a new frozen case with the human's expectation. Append-only: an existing case is
  never edited, not even by a later, better correction.
- **The weekly writeback patches the owning skill with ONE rule.** No dates, no job ids, no incident
  narration — a lesson reads as a rule, not as a log. Two independent signals and a named ref are the
  bar; silence is a valid outcome. The prompt lives at `docs/learning/weekly_writeback.prompt.md` and
  the cron job that runs it is owned by **`cpompa-ai-cloud`** (it owns `hermes-infra/coding-loop`).
  s2docs ships the prompt; it does not schedule it.

## Append-only means append-only

Correct a recorded verdict by appending a superseding row through the API (`POST /api/evidence`, or
`POST /api/jobs/{id}/verdict` while the job runs) or by correcting a close-out with
`POST /api/jobs/{id}/restatus` (terminal→terminal, reason mandatory, audited). Never hand-edit
`confirms.jsonl` / `latest_confirm.json`, never `UPDATE` the `evidence` table: the server holds the DB
in WAL mode and a hand edit breaks the guarantee the whole loop rests on.

## Baseline

Recorded 2026-10-15 on `feature/dev/learning-loop`, before the loop existed:
**320 passed, 5 skipped** (36 s). Update this line when a suite run changes it — the number is only
useful next to a date.
