-- 0014: jobs gains `origin` — who asked for this run, from a closed set.
--
-- The learning loop reads the job ledger to decide what to learn from. Without provenance it cannot
-- tell a run a person asked for and watched from a driver/eval/probe run, and an artifact built
-- mostly from synthetic rows describes the test suite rather than usage (ask-hittle's Sep-2026
-- hard-negatives audit: 163 exported, 58 real — "the fix is provenance, not filtering").
-- Provenance is recorded at intake and read back; it is never inferred from a proxy.
--
-- Additive on purpose: ALTER TABLE ... ADD COLUMN with a constant default, so `jobs` is not rebuilt.
-- 0013 had to recreate the table and stash its children because it changed a CHECK constraint;
-- nothing here changes a constraint, so none of that applies.
--
-- Deliberately no CHECK: SQLite cannot add one via ALTER, and a Python-side check at intake reports a
-- 422 naming the field — which is what an agent fixing its own request needs — instead of a raw
-- IntegrityError. The value set lives in `models.JOB_ORIGINS` / `s2docs.learning.ORIGINS`, so a typo
-- cannot silently become a valid member.
--
-- Every pre-existing row becomes 'agent': historical runs were never tagged, and guessing which of
-- them a human asked for would be inventing data. A smaller honest sample beats a larger guessed one.
ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent';

-- The loop's every read is "origin X, since T".
CREATE INDEX jobs_origin ON jobs (origin, created_at DESC);
