-- 0016: live actions — what a sentence did to a running game, and in-game chat requests waiting for Hermes.
-- One row per executed (or refused) plan. text is verbatim user input; intents_json is the validated
-- envelope; lines_json is exactly what was sent; readback is the console tail the check was judged on.
CREATE TABLE live_actions (
    id            TEXT PRIMARY KEY,
    host_id       TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,                  -- plugin | mcp | chat | cli
    text          TEXT,
    intents_json  TEXT NOT NULL,
    lines_json    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL,                  -- verified | sent | failed | refused
    detail        TEXT,                           -- why refused/failed, or the check that passed
    readback      TEXT,
    request_id    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX live_actions_host ON live_actions (host_id, created_at DESC);

CREATE TABLE live_requests (
    id          TEXT PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    player      TEXT,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | ignored
    note        TEXT,
    action_id   TEXT REFERENCES live_actions(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX live_requests_state ON live_requests (host_id, state, created_at);

-- Where each host's CS2 netcon listens. Data, not config: set from the plugin or REST, survives restarts,
-- and needs no edit to the launchd plist that runs :8812.
CREATE TABLE live_hosts (
    host_id      TEXT PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    netcon_addr  TEXT NOT NULL,                   -- ip-or-name:port, e.g. 10.88.20.235:2121
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
