Weekly s2docs learning writeback: turn measured outcomes into rules the fleet actually reuses.

Your inputs, in this order:

1. `uv run s2docs learning harvest --json` — refresh the four artifacts (exit 6 means a declared
   source could not be read: say so and stop rather than reporting an empty week).
2. `uv run s2docs learning digest --since-days 7` — writes `signals.jsonl`, `reports/digest-<date>.md`
   and `lessons.json` under `$S2DOCS_DATA_DIR/learning/`.
3. `cat "$S2DOCS_DATA_DIR/learning/lessons.json"` — per-class proposals with evidence refs.
4. `uv run s2docs learning gate` — this week's verdict (0 pass, 1 regression, 2 could not evaluate).
5. The newest report: `ls -t "$S2DOCS_DATA_DIR/learning/reports"/digest-*.md | head -1`, then read it.

Then apply lessons. Rules, in order of precedence:

- **Evidence first.** Adopt a lesson only for a class with `count >= 2` and at least one concrete ref
  you can name. One lesson per class per week, maximum. No lesson from a single event.
- **Patch the owning skill, not a new one.** `skill_manage(action='patch')` on the `owner_hint` named
  in the proposal. Add ONE short, generic, imperative rule to the right section. Never add dates, job
  ids, branch names or incident narration — a lesson must read as a rule, not as a log.
- **No duplicates.** `skill_view` the target skill first; if it already states the rule, skip it and
  say so. Silence is a valid outcome: if nothing this week clears the bar, adopt nothing.
- **The gate is not yours to move.** Never edit an expectation in `golden_questions.json` to make a
  failing case pass, never lower a threshold in `thresholds.json`, never weaken a test or a checker to
  make a run look green. If a proposed lesson would need any of those, do not apply it — report it.
- **Never edit repo code from this job.** It patches skills and appends a ledger line, nothing else.

Finally append one JSON line per applied patch to `$S2DOCS_DATA_DIR/learning/writeback.jsonl` with
keys: `ts`, `class`, `skill`, `refs`, `chars_added`, `note`.

Answer in at most 10 lines: `adopted:` (class → skill, one line each), `skipped:` (class → reason),
and `gate:` (this week's verdict line). Nothing else.
