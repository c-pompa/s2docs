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
| `clear` | `what` (one id) — omit for everything spawned | sandbox: `hs_clear <id>` \| `hs_clear all`; console: `ent_remove_all <classname>`; judged by its removal count (`Removed N <class>` / `[hs] cleared N`) or by a follow-up `ent_find` with no rows |

Ids are `^[a-z][a-z0-9_]{0,47}$`, so `chicken;quit` is a validation error, not a command. `say` is
sanitised down to `[A-Za-z0-9 .,:'()/+-]` and can only ever produce `say Hermes: …`.

**Spawnables** (`GET /api/live/spawnables`, `s2docs.live.spawnables`):

| id | classname | template | height (u) | console_ok |
|---|---|---|---|---|
| `chicken` | `chicken` | `hs_tpl_chicken` | 24 | yes — S1, 2026-10-25 |
| `car_wreck` | `prop_dynamic` | `hs_tpl_car_wreck` | 64 — **unmeasured** | no |
| `car_hatch` | `prop_dynamic` | `hs_tpl_car_hatch` | 64 — **unmeasured** | no |
| `car_van` | `prop_dynamic` | `hs_tpl_car_van` | 96 — **unmeasured** | no |

The three vehicles are VPK-listed single-model props (`dust_car_wreck_body`, `cara_82hatchback`, `van`):
dust2's own hatchback/taxi/flatbed ship as 4-part models, so they would spawn as one decapitated piece. Their
heights are only used for `stack` spacing and are **placeholders** until the Task 4.8 live stack readback
measures them; fix them there and regenerate.

`console_ok` is `True` only where a recorded spike saw the entity **render**; a spawn of an unproven
class is refused with "needs the sandbox" instead of silently drawing nothing (that is what went wrong
with `ent_create` on retail before S1).

## The allowlist rule

`recipes.ALLOWED` is one anchored regex. After a plan is compiled, every line is `fullmatch`ed against it,
and a mismatch raises before anything is sent — so a bug in a recipe cannot become a console command.

```
sv_cheats 1 | mp_autoteambalance 0 | mp_limitteams 0 | bot_kick | bot_add_t | bot_add_ct |
mp_warmup_end | mp_restartgame 1 | jointeam [123]( 1)? | sleep N | hs_ping | spawn_group_load hermes_sandbox |
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

A console spawn takes the player's position from **`getpos_exact`**, which prints the **feet**. Measured on
pomps-pc (CS2 `1.41.9.2/14192`, 2026-10-25, standing still, two identical samples), the same spot read:

```
getpos        -> setpos        -428.000000 -843.000000 156.874420;setang       0.000000 133.500031 0.000000
getpos_exact  -> setpos_exact  -428.000000 -843.000000  95.296288;setang_exact 0.000000 133.500031 0.000000
```

So `getpos` is the eye at +61.58 over the feet. The old code subtracted a guessed `GETPOS_EYE_HEIGHT` of 64
from `getpos`; that constant is **gone** — the recipe adds no offset, and `getpos` output is deliberately not
parsed (`service.parse_pose` only matches the `setpos_exact …;setang_exact …` shape).

A `clear` is verified by its own removal line when it reports at least one removal — `Removed 3 chickens` from
`ent_remove_all` (measured on pomps-pc 2026-10-25) or `[hs] cleared N` from the sandbox — and otherwise by a
follow-up `ent_find <class>` that lists no entity rows. It is `failed` when rows remain, so `clear` no longer
comes back `sent`. `hs_clear all` names no class to re-check and is judged on its own `[hs] cleared N` line.

The `ent_find` readback counts entity ROWS (`   'chicken' : '' (entindex 354)`), never the class name:
ent_find's header line (`...containing substrings: "chicken"`) contains the name even when nothing exists,
which once made an empty search count as one chicken.

## The sandbox: `hermes_sandbox` (helper map + cs_script runtime)

Stock de_dust2 is never modified. Everything that has to *create* entities is done by a second, tiny addon
streamed onto the running map with `spawn_group_load hermes_sandbox`:

```
hermes_sandbox/maps/hermes_sandbox.vmap        entity-only map: 1 point_script + 2 entities per spawnable
hermes_sandbox/maps/scripts/hermes_sandbox.js  the cs_script that point_script runs
```

Both are **generated** by `s2docs.live.sandbox_map` from the templates in `src/s2docs/live/sandbox/`
(`hermes_sandbox.vmap.in`, `runtime.js`, `formation.mjs`) plus `spawnables.STOCK`:

- `hermes_sandbox.vmap.in` is a copy of iso-spectate's `iso_spectate_ui.vmap` with its three entities deleted
  and one `{{ENTITIES}}` line left in the world's `children` array. Entity-only on purpose: a hand-authored
  WORLD mesh crashes `resourcecompiler` (`docs/HAMMERED.md`), while an entity-only helper map compiles in
  seconds.
- Per spawnable the generator writes a `point_template` (`hs_tpl_<id>`, `Template01 hs_src_<id>`) and its child
  entity (`hs_src_<id>`: classname, plus `model` for a `prop_dynamic`). The template is what puts a model in
  the map's resource manifest — which `ent_create` never did, and why it rendered nothing on retail.
- `runtime.js` carries `/*FORMATION*/` (filled with `formation.mjs`, minus `export`) and `/*HEIGHTS*/{}` (filled
  from the spawnables), so the shipped script is ONE file whose only import is `cs_script/point_script`.

Rendering is byte-stable (uuid5 ids from a fixed namespace, spawnables sorted by id), so a deploy's MD5 only
changes when the content does. Run `node --test 'tests/js/*.test.mjs'` for the JS half of the shared formation
vectors and `pytest tests/test_live_sandbox_map.py` for the generated map/script.

The runtime registers cheat commands only, and answers on the console:

| command | reply |
|---|---|
| `hs_ping` | `[hs] pong <live entities>` |
| `hs_spawn <id> <count> <formation> <spacing> <0\|1>` | `[hs] spawned <k> <id> at x,y,z;…` or `[hs] error <why>` |
| `hs_clear <id\|all>` | `[hs] cleared <k>` |
| `hs_model <models/…vmdl> [scale]` | `[hs] precaching …`, then `[hs] model … spawned name=…` (spike S2, Task 4.9) |

`s2docs validate-script` on the generated script is clean: **0 errors**, and only `deprecated_api` warnings
for `Instance.RegisterCheatCommand` and `Instance.Msg` (16 as of the `hs_model` addition). They are kept on
purpose — they are the only way a script takes console input and writes console output, and iso-spectate's
shipped `spectate.vjs` uses the same pair.

**Spike S2 (`hs_model`, Task 4.9).** `hs_model <models/…vmdl> [scale]` asks the running server to
`Instance.Precache` a compiled model and, once `IsLoadedForAll()` says it is there, spawns the generic
`hs_tpl_prop` template and swaps the model onto it with `SetModel`/`SetModelScale`. The template is a roasted
chicken on purpose: a roasted chicken where the requested model should be is the tell that `SetModel` did not
take.

**Measured 2026-10-27 (pomps-pc, CS2 1.41.9.2/14192): the route works end to end.** The model was compiled on
desktop-pc, pulled with an MD5 readback, pushed loose into `<CS2>/game/csgo/` at its own relative paths,
`spawn_group_load hermes_sandbox` streamed the group in, and `hs_model models/tsr_eefa8331/tsr_eefa8331.vmdl`
answered `[hs] precaching models/…` then `[hs] model models/… spawned name=models/…`. The user confirmed the
prop appeared and disappeared (a pixel diff agreed). One trap is now part of the recipe: **lock the round
first** — `mp_ignore_round_win_conditions 1`, `mp_roundtime 60`, `mp_roundtime_defuse 60`, `mp_freezetime 0`
(`recipes.ROUND_LOCK_LINES`) and only then `mp_restartgame 1`. With no bots the round otherwise ends after
~2 minutes and the restart wipes every runtime spawn, which is exactly what the user saw on the first attempt.

`s2docs.live.service.show_model(db, host_id, model, clear=False)` is that route in code: it refuses to send
anything unless `status` lists the spawngroup **and** `hs_ping` answers, sends the lock + `mp_restartgame 1` +
`hs_model`, and returns `verified` only when the reply carries
`[hs] model <exact path> spawned name=<exact path>` **and** the `[hs] pong` counter rose. A different name
(`SetModel` lost → roasted chicken), an `[hs] error …` line, or no line at all within the drain is `failed`;
a host with no sandbox is `blocked`. The readback is mechanical: `s2docs pipeline run` still closes
`inconclusive` with `next='request a visual verdict'` unless the caller passes `--verdict pass`.


### Building and loading it

```python
from s2docs.live import sandbox_build as sb
run = sb.build(db, "desktop-pc")      # 1. render + push the sources, start a server-owned compile
sb.pull_compiled(db, "desktop-pc")    # 2. bring the .vpk/.vjs_c back, md5-checked against the host
sb.deploy_loose(db, "pomps-pc")       # 3. push them to the PLAYING host's loose game/csgo/maps tree
```

1. **Compiles run on `desktop-pc` only** (`COMPILE_HOSTS`). pomps-pc hard-froze on a compile once; any other
   host raises instead of starting `resourcecompiler`. The run is server-owned (`builds.start`), so its log
   and exit code stay reachable after the request returns.
2. The compiled files have to come back to the Mac because the deploy service pushes *from* here:
   `hosts.scp_pull_argv` (reverse of `scp_argv`, same `--`-before-operands hardening, refuses an empty,
   option-like or newline-carrying remote path) plus one `Get-FileHash` readback per file.
3. Loose files, not the addon: an addon-mounted script did not load in iso-spectate.
4. `POST /api/live/sandbox/load {host_id, deploy_id}` sends `sv_cheats 1`, `spawn_group_load hermes_sandbox`,
   `sleep 3`, `status`, `hs_ping`, and is `verified` only when `status` lists the spawngroup **and** `hs_ping`
   answers. It refuses a `409` unless the deployment is verified, on that host, and carries
   `maps/hermes_sandbox.vpk` — a missing vpk makes `spawn_group_load` kick the client and kill the server.
5. `POST /api/live/launch {host_id, map}` starts CS2 through a one-shot scheduled task with an **interactive**
   principal (a service-spawned `cs2.exe` has no desktop), with the PowerShell script on ssh's **stdin** — never
   an inline `-Command "…"` string — then polls netcon for up to 120 s.

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
| `POST /api/live/sandbox/load {host_id, deploy_id}` | stream `hermes_sandbox` onto the running map; `verified` only on spawngroup + `[hs] pong` |
| `POST /api/live/launch {host_id, map}` | start CS2 on the host (`de_*` only) and wait for its netcon |

`show_model` (the `hs_model` route above) is reached from `s2docs pipeline run --ingame-host <host>` —
`live.asset_artifacts` collects the model build's compiled files off the compile host, pushes them loose to
the target host with an MD5 readback, and the stage is `verified` only on the game's own
`[hs] model <path> spawned name=<path>` line plus a rising `hs_ping` count. It is CLI/pipeline-only for now
(no REST route): it needs a build run id and a host that is already in a map with the sandbox loaded, which
is the pipeline's job, not a one-shot action.

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
