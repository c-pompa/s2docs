-- 0017: build_runs records the pid that owns a run, so the run outlives its starter.
--
-- A build is launched detached — its own session on POSIX, its own process group on Windows — so a
-- 38-minute map compile keeps running when `s2docs serve`, the MCP server or the CLI that started it
-- goes away. The row therefore has to say *which* process to ask about after that restart.
--
-- `handle` (0005) already held str(pid), but only for display: free text, with nothing guaranteeing it
-- was a pid, and nothing reading it. `pid` is the typed, queryable column `builds.recover` asks the OS
-- about; `handle` stays as the human-facing field. Rows written before this migration keep pid NULL
-- and `recover` falls back to a numeric `handle`, so the runs stranded by the old code are recoverable
-- too.
--
-- A pid alone is not a perfect identity — the OS recycles the number, and a run abandoned long enough
-- for its pid to be reused reads as alive. That is accepted: recoverable-but-late beats interrupting a
-- live compile, and `build cancel <id>` is the escape hatch.
--
-- Additive on purpose: an ADD COLUMN with no default, so build_runs is not rebuilt (no CHECK
-- constraint changes) and `assets.build_id`'s FK keeps pointing at the same table.
ALTER TABLE build_runs ADD COLUMN pid INTEGER;

-- The recovery sweep only ever looks at live rows; a partial index keeps it off a full table scan as
-- the run history grows, without indexing the (majority) terminal ones.
CREATE INDEX build_runs_live ON build_runs (status) WHERE status IN ('queued', 'running');
