# Live actions — a sentence about a running CS2 game, turned into allowlisted console lines

A **live action** is one validated request applied to the CS2 session that is already running on a host,
and then judged by what the game reports back. Nothing a model writes reaches the console: a sentence is
turned into an **intent** from a closed vocabulary, the intent is compiled by a **recipe** into lines that
must each fullmatch an **allowlist**, the lines go out over **netcon**, and the result is a row in
`live_actions` with a readback.

```
sentence / REST body / MCP call
   -> IntentEnvelope          (s2docs.live.intent — closed vocabulary, bounds checked)
   -> Plan (lines, expect)    (s2docs.live.recipes — every line must fullmatch ALLOWED)
   -> netcon                  (s2docs.live.netcon — TCP, one console line per command)
   -> readback                (its own round trip: `status` player rows / `ent_find` / `[hs] ...`)
   -> live_actions row        (verified | sent | failed | refused)
```

`src/s2docs/live/service.py` is the only code that talks to netcon. REST (`/api/live/*`), MCP
(`live_*`) and the CLI (`s2docs live …`) all call `service.act()` / `service.status()`.

## The vocabulary (closed)

`GET /api/live/intent-schema` returns the JSON schema a model fills **plus** the spawnable ids it may use;
the plugin builds its prompt from that response, so adding a spawnable never means editing a prompt.
An envelope is `{"intents": [...], "say": "optional one-line reply"}` — 1 to 8 intents.

| intent | fields | compiled to |
|---|---|---|
| `bots` | `team_size` (players per side **including you** when `me` is set, `5v5` = 5) **or** `t` / `ct` (exact bot counts) — the two forms are exclusive; `me` (`t`\|`ct`\|`spectator`); `restart` (default true) | `mp_autoteambalance 0`, `mp_limitteams 0`, `bot_kick`, `jointeam <n> 1` when `me` is set, then `bot_add_t` × t and `bot_add_ct` × ct, `mp_warmup_end`, `mp_restartgame 1` |
| `spawn` | `what` (a spawnable id), `count` 1-16, `formation` (`line`\|`circle`\|`stack`\|`grid`), `spacing` 8-512, `follow` | sandbox: `hs_spawn <id> <count> <formation> <spacing> <follow>`; console: `ent_create <classname> {"origin" "x y z"}` per item |
| `clear` | `what` (one id) — omit for everything spawned | sandbox: `hs_clear <id>` \| `hs_clear all`; console: `ent_remove_all <classname>` |

Ids are `^[a-z][a-z0-9_]{0,47}$`, so `chicken;quit` is a validation error, not a command. `say` is
sanitised down to `[A-Za-z0-9 .,:'()/+-]` and can only ever produce `say Hermes: …`.

**Spawnables** (`GET /api/live/spawnables`, `s2docs.live.spawnables`):

| id | classname | template | height (u) | console_ok |
|---|---|---|---|---|
| `chicken` | `chicken` | `hs_tpl_chicken` | 24 | yes — S1, 2026-10-25 |

`console_ok` is `True` only where a recorded spike saw the entity **render**; a spawn of an unproven
class is refused with "needs the sandbox" instead of silently drawing nothing (that is what went wrong
with `ent_create` on retail before S1).

## The allowlist rule

`recipes.ALLOWED` is one anchored regex. After a plan is compiled, every line is `fullmatch`ed against it,
and a mismatch raises before anything is sent — so a bug in a recipe cannot become a console command.

```
sv_cheats 1 | mp_autoteambalance 0 | mp_limitteams 0 | bot_kick | bot_add_t | bot_add_ct |
mp_warmup_end | mp_restartgame 1 | jointeam [123]( 1)? | sleep N | hs_ping |
hs_spawn <id> <n> <formation> <spacing> <0|1> | hs_clear <id> | ent_remove_all <classname> |
ent_create <classname> {"origin" "<x> <y> <z>"} | say Hermes: <safe text>
```

**Adding a recipe = adding a regex branch + a case in `test_every_line_matches_the_allowlist`.** No
exception: if a line cannot be described by a branch, the feature is not part of this vocabulary yet.

`sv_cheats 1` is prepended once when a spawn/clear needs it; bots and `say` do not need it.

## Statuses

| status | meaning |
|---|---|
| `verified` | the recipe's readback check passed (bot count from `status` player rows, `ent_find` count, or the `[hs] spawned N …` line) |
| `sent` | lines were sent and the recipe defines no readback (nothing was checked — not a success claim) |
| `failed` | the check ran and disagreed ("expected 9 bots in status, saw 1"), or netcon dropped mid-sequence |
| `refused` | valid request, impossible here: unknown spawnable, follow without the sandbox, no player position for a console spawn. `lines` is `[]`: nothing was sent |

Bot counting reads **`status` player rows** (`^\s*\d+\s+BOT\s+\d+\s+\d+\s+\S+\s+\d+\s+'…'$`) from a
readback round trip of its own. It never greps the stream for "BOT": join lines
(`ClientPutInServer … [Colin]`, `"Colin<10><BOT>…"`) contain it too, which is exactly how a false
`verified` used to happen.

## Formations

`line` trails behind the player at `spacing` intervals; `stack` puts everything in front at
`2 × spacing` and raises each item by the spawnable's height; `circle` surrounds the player
(radius = `max(spacing, spacing·n/2π)`); `grid` fills rows in front. Source yaw: `0` = +X, `90` = +Y.
The Python and the JS sandbox share the same expected values in
`tests/fixtures/live/formation_vectors.json` (Task 4.3 asserts the JS side against the same file).

A console spawn subtracts `GETPOS_EYE_HEIGHT` (64 u) from `getpos`, because `getpos` reports the **eye**:
without it, chickens spawn in the player's face instead of on the floor.

## Where netcon lives: the registry, not an env var

`PUT /api/live/hosts/{host_id} {"netcon_addr": "10.88.20.235:2121"}` stores the address in the
`live_hosts` table. It is **data, not config**: the instance on `:8812` is started by a launchd plist that
carries only `S2DOCS_DATA_DIR`, so an `S2DOCS_NETCON` env var would have been invisible to it (and
editing the plist is a fleet change). The address must match `host:port`; anything else is `409`.

`s2docs live set-netcon --host pomps-pc --addr 10.88.20.235:2121` does the same from the CLI.

## Surfaces

| call | effect |
|---|---|
| `GET /api/live/spawnables` | what a spawn may name |
| `GET /api/live/intent-schema` | intent JSON schema + spawnable ids (the plugin's prompt source) |
| `POST /api/live/plan {host_id, envelope}` | dry run: the lines an envelope compiles to, assuming the sandbox is loaded. No host contact. `409` when the request cannot run |
| `POST /api/live/actions {host_id, envelope, source, text?, request_id?}` | run it and judge it by readback → `201` `LiveAction` |
| `GET /api/live/actions?host_id=&limit=` | recent actions, newest first |
| `GET /api/live/status?host_id=` | reachable, map, loaded spawngroups, sandbox answering, `cs2_build` |
| `GET /api/live/hosts` | the netcon registry |
| `PUT /api/live/hosts/{id}` | record an address |
| `GET /api/live/capabilities?host_id=` | the capability ledger for the build that host runs now |
| `POST /api/live/capabilities {cs2_build, name, status, evidence}` | record one spike result |

A host that cannot take an action now (no netcon address, unreachable, leased by a test run) is a `409`
with `conflict: true`; a malformed envelope is `422`.

MCP: `live_spawnables`, `live_plan`, `live_act` (the only write tool — `open_world_hint`), `live_status`,
`live_capabilities`. CLI: `s2docs live status|set-netcon|act|actions|caps|cap-record` (`--json` for one
`s2docs/v1` object; exit codes 3 not found, 4 conflict, 9 validation).

## The capability ledger (per CS2 build)

Several of the things later phases want are experimental (`Instance.Precache`, `script_reload`,
`point_worldtext`, HUD clicks, offscreen panels). A pass on one CS2 build is **not** assumed on the next:
`live_capabilities` is keyed by the build string netcon's `status` already prints —

```
version  : 1.41.9.2/14192 10955 insecure  public   ->  key "1.41.9.2/14192"
```

— so `GET /api/live/capabilities?host_id=pomps-pc` answers "what was measured on the build you are running
now". Anything not measured is `unknown` (with a `what` hint describing the spike that would prove it), and
a feature that needs a capability calls `capabilities.require(db, build, name)` before promising it.
A failed capability raises with the spike to run. A build with no version line (CS2 not in a map) is
`200` with `cs2_build: null` and the reason: that is the game's state, not a server fault.

Record results with `s2docs live cap-record --build 1.41.9.2/14192 --name console_chicken --status pass
--evidence "ent_create chicken rendered, ent_remove_all removed 1"`.

Capability names: `bots_team_switch`, `console_chicken`, `runtime_precache`, `script_reload`,
`world_text`, `hud_clicks`, `offscreen_panel`.

## Spike results (what this rests on)

Measured 2026-10-25 on **pomps-pc**, CS2 `1.41.9.2/14192` (retail, `-netconport`):

- **S0 — bots and team switch: PASS.** `bot_add_t` / `bot_add_ct` produced the requested counts, and
  `jointeam 2 1` moved the **local** player to T (`jointeam <team> 1` is the form that works; the plain
  `jointeam 2` opens the team menu and does nothing). This is `ME_TEAM_LINE`.
- **S1 — console chicken: PASS.** With 0 chickens present, `ent_create chicken` created one the user could
  **see**, and `ent_remove_all chicken` removed exactly 1. Hence `Spawnable("chicken", …, console_ok=True)`
  and the `console_ok` gate on every other console spawn.

Both are also recorded in the capability ledger (`bots_team_switch`, `console_chicken`).

One measured detail these rows shape: a real bot row in `status` looks like
`  10      BOT    0    0     active      0 'Colin'`, and an empty slot like
`65535 [NoChan]    0    0 challenging      0 ''`. The human row carries the SteamID and a ping.

## Testing

Everything here is offline: `tests/test_live_netcon.py` runs a fake TCP "CS2 console" (`FakeConsole`)
that answers canned output, and the service tests use a `FakeClient` with per-line replies, so the
readback logic is exercised without a game. `tests/test_live_intent.py`,
`tests/test_live_recipes.py` (including the allowlist cases), `tests/test_live_formation.py` (shared
vectors), `tests/test_live_service.py`, `tests/test_live_capabilities.py`.

The one thing offline tests cannot prove is that the game renders what was spawned — that is what the
spikes and the readback are for.
