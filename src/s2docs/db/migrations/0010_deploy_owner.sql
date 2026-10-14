-- 0010: deployments record the host-lock owner that was effective for the push.
-- deploy_push now takes the host lease for the copy+verify window (or reuses the caller's lease),
-- so a deploy can no longer land on top of another owner's work. The effective owner — 'deploy:<id>'
-- when the server claimed the lease itself, else the caller-supplied owner whose lease was reused —
-- is recorded here for the audit trail and the Activity page.

ALTER TABLE deployments ADD COLUMN owner TEXT;
