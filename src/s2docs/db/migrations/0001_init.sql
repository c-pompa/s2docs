-- 0001: core schema. One row per ingested game build; every data table hangs
-- off builds(id) so a build can be replaced atomically and old builds diffed.

CREATE TABLE games (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    app_id      INTEGER,
    mod_dir     TEXT,
    source_repo TEXT
);

CREATE TABLE builds (
    id              INTEGER PRIMARY KEY,
    game_id         TEXT NOT NULL REFERENCES games(id),
    build_number    INTEGER NOT NULL,
    patch_version   TEXT,
    source_revision TEXT,
    version_date    TEXT,
    source_commit   TEXT,
    ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    is_current      INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    UNIQUE (game_id, build_number)
);
CREATE UNIQUE INDEX builds_one_current ON builds (game_id) WHERE is_current = 1;

-- Console variables and commands -------------------------------------------
CREATE TABLE convars (
    id            INTEGER PRIMARY KEY,
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('convar', 'command')),
    name          TEXT NOT NULL COLLATE NOCASE,
    default_value TEXT,
    min_value     TEXT,
    max_value     TEXT,
    description   TEXT,
    UNIQUE (build_id, kind, name)
);
CREATE INDEX convars_build_name ON convars (build_id, name);

CREATE TABLE convar_flags (
    convar_id INTEGER NOT NULL REFERENCES convars(id) ON DELETE CASCADE,
    flag      TEXT NOT NULL,
    PRIMARY KEY (convar_id, flag)
) WITHOUT ROWID;
CREATE INDEX convar_flags_flag ON convar_flags (flag);

-- Hammer entities (FGD) -----------------------------------------------------
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    side        TEXT NOT NULL CHECK (side IN ('server', 'client')),
    classname   TEXT NOT NULL,
    class_type  TEXT NOT NULL,
    cpp_class   TEXT,
    description TEXT,
    UNIQUE (build_id, side, classname)
);

CREATE TABLE entity_bases (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,
    base_name TEXT NOT NULL,
    PRIMARY KEY (entity_id, ordinal)
) WITHOUT ROWID;

CREATE TABLE entity_members (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('keyvalue', 'input', 'output')),
    name        TEXT NOT NULL,
    type        TEXT,
    description TEXT,
    notes       TEXT,
    component   TEXT,
    choices     TEXT  -- JSON [[value, label], ...]
);
CREATE INDEX entity_members_entity ON entity_members (entity_id, ordinal);
CREATE INDEX entity_members_lookup ON entity_members (kind, name COLLATE NOCASE);

-- Schema system (classes + enums) -------------------------------------------
CREATE TABLE schema_classes (
    id              INTEGER PRIMARY KEY,
    build_id        INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    module          TEXT NOT NULL,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('class', 'enum')),
    underlying_type TEXT,
    metadata        TEXT NOT NULL DEFAULT '[]',  -- JSON list of metadata strings
    UNIQUE (build_id, module, name)
);
CREATE INDEX schema_classes_name ON schema_classes (build_id, name);

CREATE TABLE schema_class_bases (
    class_id  INTEGER NOT NULL REFERENCES schema_classes(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,
    base_name TEXT NOT NULL,
    PRIMARY KEY (class_id, ordinal)
) WITHOUT ROWID;
CREATE INDEX schema_class_bases_base ON schema_class_bases (base_name);

CREATE TABLE schema_fields (
    id       INTEGER PRIMARY KEY,
    class_id INTEGER NOT NULL REFERENCES schema_classes(id) ON DELETE CASCADE,
    ordinal  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    type     TEXT,
    value    TEXT,
    metadata TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX schema_fields_class ON schema_fields (class_id, ordinal);
CREATE INDEX schema_fields_name ON schema_fields (name);

-- Game events ---------------------------------------------------------------
CREATE TABLE game_events (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    precedence  INTEGER NOT NULL,  -- higher overrides lower (core < game < mod)
    name        TEXT NOT NULL,
    description TEXT,
    UNIQUE (build_id, source_file, name)
);
CREATE INDEX game_events_name ON game_events (build_id, name);

CREATE TABLE game_event_fields (
    event_id    INTEGER NOT NULL REFERENCES game_events(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (event_id, ordinal)
) WITHOUT ROWID;

-- Official script APIs (cs_script point_script.d.ts) ------------------------
CREATE TABLE script_symbols (
    id         INTEGER PRIMARY KEY,
    build_id   INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    api        TEXT NOT NULL,
    module     TEXT,
    parent     TEXT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    signature  TEXT NOT NULL,
    doc        TEXT,
    extends    TEXT,
    deprecated INTEGER NOT NULL DEFAULT 0,
    ordinal    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX script_symbols_key ON script_symbols (build_id, api, ifnull(parent, ''), name, ordinal);
CREATE INDEX script_symbols_parent ON script_symbols (build_id, api, parent);

-- Pulse visual-scripting bindings ------------------------------------------
CREATE TABLE pulse_bindings (
    id            INTEGER PRIMARY KEY,
    build_id      INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    module        TEXT NOT NULL,
    library       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    signature     TEXT NOT NULL,
    friendly_name TEXT,
    description   TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX pulse_bindings_lookup ON pulse_bindings (build_id, library, name);

-- Raw source files worth serving verbatim (e.g. point_script.d.ts) ----------
CREATE TABLE raw_files (
    build_id INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    path     TEXT NOT NULL,
    content  TEXT NOT NULL,
    PRIMARY KEY (build_id, path)
) WITHOUT ROWID;

-- Human-written documentation (VDC, source2.wiki, ...) — populated later ----
CREATE TABLE docs (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL UNIQUE,
    game_id    TEXT REFERENCES games(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    license    TEXT,
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Unified full-text index ---------------------------------------------------
-- name:       identifier as-is ('_' is a token char, so prefix queries work: mp_round*)
-- name_parts: identifier split on '_' / camelCase ('round time', 'on player jump')
CREATE VIRTUAL TABLE search_fts USING fts5 (
    name, name_parts, title, body,
    kind UNINDEXED, game_id UNINDEXED, build_id UNINDEXED, ref_key UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2 tokenchars '_'",
    prefix = '2 3 4'
);
