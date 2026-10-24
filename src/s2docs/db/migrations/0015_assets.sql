-- 0015: the asset catalog — a view with a promotion step over the records we already keep.
--
-- "What did I make, and can I prove it works" is answered today by scrolling builds, deployments and
-- jobs and remembering which three belong together. The catalog is ONE row per promoted asset that
-- points at those records; it is never a second file store (the bytes stay where the build/deploy/job
-- put them) and never a second workflow engine (each stage stays owned by its service).
--
-- The row is a *claim* about existing evidence, so the gates are enforced in the service layer
-- (s2docs.assets) before the INSERT — each gate exists because skipping it produces a catalog of
-- lies:
--   1. build_id must name a build run whose status is 'succeeded' (an uncompiled .vmdl is not an asset);
--   2. a kind='model' row needs frame_job_id + frame_artifact — a frame the job actually stored —
--      and that job's verdict must not be fail (nothing proves a prop renders but the frame);
--   3. licence is NOT NULL: it is recorded at promotion, never inferred later;
--   4. retiring is a soft delete (retired_at), so the provenance rows survive (append-only).
--
-- build_id is NOT NULL and its FK has no ON DELETE action on purpose: deleting a host cascades its
-- build_runs, and an asset whose build vanished would be a provenance lie. The FK refuses that
-- delete instead. deploy_id / frame_job_id are optional links to rows that must exist.
--
-- `slug` is UNIQUE: the human handle a page links to and an agent re-uses, so two assets cannot share
-- one. It is lowercased kebab-case, derived from name when the caller omits it.
--
-- No CHECK on `kind`: SQLite cannot add one later via ALTER (see 0014's note), so the value set lives
-- in models.ASSET_KINDS and a bad value is a 422 naming the field. The CHECK here is a belt on the
-- table as first created; the service is the gate that reports it well.
--
-- `tags` is a JSON array of lowercase strings, filtered with json_each (JSON1 is compiled into the
-- Python 3.11 sqlite3 this project runs on).

CREATE TABLE assets (
    id               TEXT PRIMARY KEY,              -- asset id, e.g. 'a-1a2b3c4d'
    kind             TEXT NOT NULL CHECK (kind IN ('model', 'action')),
    name             TEXT NOT NULL,                 -- human name, shown on the card
    slug             TEXT NOT NULL UNIQUE,          -- stable handle, lowercase kebab-case
    prompt           TEXT,                          -- the prompt the asset came from
    concept_artifact TEXT,                          -- concept image artifact (path or URL)
    mesh_sha256      TEXT,                          -- GLB content hash (64 hex)
    tris             INTEGER CHECK (tris IS NULL OR tris >= 0),
    vmdl_path        TEXT,
    vmat_path        TEXT,
    build_id         TEXT NOT NULL REFERENCES build_runs(id),  -- promotion gate: must be 'succeeded'
    deploy_id        TEXT REFERENCES deployments(id),
    frame_job_id     TEXT REFERENCES jobs(id),      -- the job whose frame proves it renders
    frame_artifact   TEXT,                          -- artifact name of that frame inside the job
    licence          TEXT NOT NULL,                 -- recorded at promotion; never inferred
    tags             TEXT NOT NULL DEFAULT '[]',    -- JSON array of lowercase tags
    created_by       TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    retired_at       TEXT,                          -- soft retire: the row (and its links) stay
    retired_reason   TEXT
);

-- The list is newest-first; kind and the retired filter are the two ways a reader narrows it.
CREATE INDEX assets_created ON assets (created_at DESC, id DESC);
CREATE INDEX assets_kind ON assets (kind, created_at DESC);
CREATE INDEX assets_build ON assets (build_id);
