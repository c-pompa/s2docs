-- 0002: Valve hand-written Hammer FGDs (labels, help text, defaults, choices).
-- `root` names the FGD entry point the class was resolved from:
--   hammer          game/<mod>/<mod>.fgd and its @includes (what Hammer's entity tool shows)
--   modeldoc        models_gamedata.fgd (ModelDoc gamedata nodes)
--   cs2_item_editor tools/cs2_item_editor/cs2_item_editor.fgd
--   addoninfo       tools/workshop_addoninfo.fgd

CREATE TABLE hammer_classes (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    root        TEXT NOT NULL,
    source_file TEXT NOT NULL,
    classname   TEXT NOT NULL,
    class_type  TEXT NOT NULL,
    description TEXT,
    tool_name   TEXT,
    tool_tip    TEXT,
    tool_group  TEXT,
    UNIQUE (build_id, root, classname)
);
CREATE INDEX hammer_classes_name ON hammer_classes (build_id, classname);

CREATE TABLE hammer_class_bases (
    class_id  INTEGER NOT NULL REFERENCES hammer_classes(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,
    base_name TEXT NOT NULL,
    PRIMARY KEY (class_id, ordinal)
) WITHOUT ROWID;

CREATE TABLE hammer_members (
    id            INTEGER PRIMARY KEY,
    class_id      INTEGER NOT NULL REFERENCES hammer_classes(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('keyvalue', 'input', 'output')),
    name          TEXT NOT NULL,
    type          TEXT,
    label         TEXT,
    default_value TEXT,
    description   TEXT,
    choices       TEXT,  -- JSON [[value, label], ...]
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX hammer_members_class ON hammer_members (class_id, ordinal);

-- Example code shipped by Valve or the community (cs_script samples, ...).
CREATE TABLE examples (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER REFERENCES builds(id) ON DELETE CASCADE,
    game_id     TEXT NOT NULL REFERENCES games(id),
    api         TEXT NOT NULL,          -- e.g. cs_script
    name        TEXT NOT NULL,          -- e.g. hello.js
    path        TEXT NOT NULL,          -- path inside the source
    origin      TEXT NOT NULL,          -- valve | community
    description TEXT,
    content     TEXT NOT NULL,
    UNIQUE (build_id, api, name)
);

-- Human docs gain a stable key and source path; FTS rows reference docs by key.
ALTER TABLE docs ADD COLUMN doc_key TEXT;
ALTER TABLE docs ADD COLUMN source_path TEXT;
ALTER TABLE docs ADD COLUMN description TEXT;
CREATE UNIQUE INDEX docs_key ON docs (doc_key);
