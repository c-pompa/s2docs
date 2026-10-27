# Hammered orchestration — prompt → asset → in-game, and the asset catalog

Companion to `HAMMERED.md` (product, contract, licence policy, phases). This file is the
**engineering shape** of the chain that file sells: one record per stage, one catalog of what the
user made, and the quality gates an asset must pass before it may be called a game asset.

Status: **spec**, written after the first end-to-end proof (a textured TripoSR prop rendered inside
CS2 on 2026-10-24). Nothing here contradicts `HAMMERED.md` §5–§7; it fills in the parts the proof
showed were missing.

## 1. One chain, one record

Every stage already has a record type. Do not invent a second pipeline — link the existing ones.

| stage | record it already has | tool | what it proves |
|---|---|---|---|
| concept (text → image) | artifact on the chain | ComfyUI worker (3070) | the prompt became pixels |
| mesh (image → GLB) | artifact + sha256 | TripoSR worker (3080) / provider | the pixels became geometry |
| asset (GLB → `.vmdl`/`.vmat`/`.vtex`) | artifact set + hashes | `s2docs asset model`, `s2docs asset material` | the geometry became a Source 2 asset |
| build | `builds` row (`build_start`/`build_wait`) | build service on a Windows host | `resourcecompiler` accepted it |
| deploy | `deployments` row (`deploy_push`/`deploy_verify`) | deploy tool | the host serves exactly these bytes |
| in-game | `jobs` row + verdict + screenshots | job client + peek | a human saw it in the frame |

**`chains`** is the only new table: `id, prompt, game, addon, stage ids (concept, mesh, asset,
build, deploy, job), status, licence, created_by, created_at`. It is a *join*, not a workflow
engine: each stage stays owned by the service that already records it, and a chain is resumable
because every stage's id is durable.

Why not one giant job: a compile slot is exclusive per box and takes minutes while a mesh takes
seconds, so a chain must be able to sit between stages without holding a lease.

## 2. The asset catalog

The catalog answers "what did I make, and can I prove it works". It is a **view with a promotion
step over existing records**, never a second store of files.

`assets`: `id, kind, name, slug, prompt, concept_artifact, mesh_sha256, tris, vmdl_path,
vmat_path, build_id, deploy_id, frame_job_id, frame_artifact, licence, tags, created_by, created_at`.

- kind `model` — the Hammered prop chain.
- kind `action` — a cs_script the agent wrote (§3).
- API: `POST /api/assets` (promote a chain), `GET /api/assets`, `GET /api/assets/{id}`,
  `GET /api/assets/{id}/provenance`, `POST /api/assets/{id}/retire`.
- MCP: `asset_promote_chain`, `asset_list`, `asset_get` — so a session can save and re-use what it
  made without leaving the plugin.
- Page: `#/cs2/assets`, grid cards whose image **is the in-game frame**, filters by kind/tag, and a
  provenance panel that links build → deploy → job → screenshot.

Promotion rules (each one exists because skipping it produces a catalog of lies):

1. **No entry without a succeeded build** — an uncompiled `.vmdl` is not an asset.
2. **No `model` entry without an in-game frame** attached to a job whose verdict is not `fail`.
3. **Licence is recorded at promotion**, and a chain may not be promoted if any input was a Valve
   asset (`HAMMERED.md` §6).
4. Provenance is append-only: retiring an asset keeps its rows.

## 3. Action kind — text → cs_script

Same chain, different asset stage:

`prompt → draft .js → validate_cs_script → build (vjs_c) → deploy → job with a user verdict`

- The draft is grounded in the index (`search_docs`, the cs_script API surface) — an agent that
  invents API must fail validation rather than ship.
- The catalog stores the source, the compiled `.vjs_c` hash, and the job id that judged it.
- An `action` has no mesh, so the mesh/build gates in §2 do not apply; the gates that do apply are
  validate + compile + a non-fail verdict.

### The compile + deploy stages, as built (measured 2026-10-27 on pomps-pc)

`s2docs pipeline script --compile [--deploy-host <id>]` (Python: `run_script_slice(compile=True,
deploy_host_id=…)`, implementation `s2docs/build/script_compile.py`) makes `build` and `deploy` real:

1. the `.js` is pushed to `<CS2>/content/csgo_addons/<addon>/scripts/<name>.js` and MD5-verified;
2. one server-owned run (`builds.start`) executes `resourcecompiler -nop4 -i <that .js>` over ssh;
3. the `.vjs_c` is pulled back to `<data>/scripts/<addon>/` against the host's `Get-FileHash` and
   attached to the run (`builds.record_artifacts`);
4. `--deploy-host` copies it **loose** to that host's `<CS2>/game/csgo/scripts/<name>.vjs_c`, MD5 readback.

What the run actually produces, verbatim from the probe (`vjs_probe` addon, CS2 in ssh's session 0, no
interactive task, no display):

```
No display adapters found. Falling back to software rasterizer. (nAdapterCount=0, nAdapter=0)
- csgo_addons\vjs_probe\scripts\vjsprobe.js
-----------------------------------------------------------------
 OK: 1 compiled, 0 failed, 0 skipped, 0m:00s
-----------------------------------------------------------------
Leaked KeyValues blocks: 594
resourcecompiler exit 0
SCRIPT COMPILE OK …\game\csgo_addons\vjs_probe\scripts\vjsprobe.vjs_c
```

`vjsprobe.vjs_c`: 1412 bytes, md5 `62cd1aade67a2ce048fd3e3bd307fb74` (identical on the host after the
loose deploy). A `.js` pass therefore needs neither a display adapter nor a logged-on session — which is
why `.js` compiles are allowed on **both** hosts while `.vmap` compiles stay `desktop-pc` only
(`live.sandbox_build.COMPILE_HOSTS`). The script path refuses any input that is not exactly one `.js`
file (a `.vmap`/`.vmdl`/directory is a hard error), so it can never be used to reach the map pass.

Two behaviours worth knowing before trusting a "OK" line:

- **resourcecompiler does not validate JS.** A script with `Instance.ThisDoesNotExist(;` compiled
  `OK: 1 compiled, 0 failed` (1008 B `.vjs_c`), and a file that is not JavaScript at all did the same
  (875 B). tsc `validate` is the only syntax/API gate — that is why a failed `validate` never compiles.
- **exit 0 is not proof of a fresh artifact.** rc can exit 0 after a crash, so the runner also requires
  the `.vjs_c` to exist with `LastWriteTime` at or after the run's start, and prints its success token
  only then. rc's real failure mode is exit 1 with
  `FATAL ERROR: Unable to determine mod from file "…"` (e.g. a missing input), and the output directory
  must exist before the redirect (rc does not create `game/csgo_addons/<addon>/scripts`: a missing dir
  fails the redirect with `out-file : Could not find a part of the path` before rc runs).

`verdict` stays **blocked**, and the reason is now the measured one: a cs_script only runs from a map
whose `point_script` references it — a runtime `ent_create` of `point_script` fails with
`Failed to load cs_script`. Making the verdict real needs a map compiled with `scripts/<name>.vjs` on its
point_script, on desktop-pc.

## 4. Quality gates — the four things the first proof lacked

The 2026-10-24 proof produced a prop that *renders* and is still not a game asset. Each gap becomes
a gate at the asset stage, so the next chain cannot repeat it silently:

| gate | rule | why |
|---|---|---|
| triangle budget | `tris <= target_tris` (prop-class default 5000, override per request) | the proof shipped 176,088 vertices / 2.41 MB for one rock |
| collision | the `.vmdl` carries a physics/collision block | a prop with no collision is visual-only: cannot be shot, stood on, or trusted to block |
| real surfaces | no `materials/default/*` placeholder in a shipped `.vmat`; normal + AO baked from the source mesh | the proof reused the game's default AO/normal, so lighting was flat |
| pivot + scale | origin at base-centre, 1 unit = 1 inch, no implicit scale in the mesh | otherwise every placement needs a magic offset |

A gate failure is reported on the asset stage with the measured value, never as a warning.

## 5. What slots in from the existing plan

- **Device gate + approval windows** (Phase C, `cpompa-ai-cloud` MR !276): decides *whether* a box
  may take a stage. `can_take()` is already written, tested and fails closed; it has **no consumer
  yet** — the claim endpoint in §6 is where it gets one.
- **Compile lock**: the s2docs host lock is the exclusive compile lease; `compile_busy()` reads it
  and treats an unreadable lock as busy. A chain stage that needs a compile must claim it and
  release on every exit path.
- **HITL**: verdicts (chat / console / web / relayed) and device approvals already exist; the chain
  uses them instead of inventing an approval of its own.
- **Jobs**: the job is the user-visible unit and the evidence container; a chain is the internal
  bookkeeping that makes a job resumable across stages.

## 6. Phases

| phase | deliverable | acceptance |
|---|---|---|
| P0 | land the quality gates (§4) in `s2docs asset` | a generated asset is ≤ budget, has collision, no placeholder surfaces, a sane pivot, and still renders in game |
| P1 | `chains` + `assets` + the page (§1–§2) | promote the already-proven prop; its card shows the frame and links build → deploy → job |
| P2 | the claim endpoint: `POST /v1/s2/claim` calling `can_take()` | a device outside its window, or with a compile held, is refused with the reason |
| P3 | the hosted skin (`/v1/s2/*` per `HAMMERED.md` §5) | a key-holding client runs the same chain over HTTP |

P0 is first because the catalog should promote assets worth keeping, not the one that exposed the
gaps.

## 7. Non-goals

- No second object store, no second job engine, no second FastAPI service.
- No Valve-derived assets in the catalog, ever (`HAMMERED.md` §6).
- No tenant work on `pomps-pc` before it is approved and scoped (it is the user's live machine).
- No secret in a portable `mcp.json`.
