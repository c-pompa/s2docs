# Hammered — a hosted backend for the s2docs plugin

Status: **plan + decisions** (nothing here ships yet except the local half, which already does).
Questions this answers: what the audited vendor actually runs, what the same shape would be for us, what we can
sell, on which hardware, and what has to be decided before a line of the vendor service is written.

## 1. What the vendor actually runs (measured, not assumed)

We unpacked their client: `npm pack <audited-package>@<version>` → 992 KB tarball,
`package/dist/*.js` only.

| question | answer | evidence |
|---|---|---|
| does the MCP server contain a model? | **no** — zero weights, zero inference code | `find -iname '*.safetensors\|*.onnx\|*.pt'` → nothing; `grep -i 'torch\|diffusion\|cuda\|weights'` → nothing |
| what does it talk to? | exactly one host, `https://api.vendor.ai` | `grep -oE 'https://[a-z0-9.-]+'` → the vendor's API host |
| is Hermes/the agent's model in the loop? | **no** | server-side there is no Hermes; the local process is a request builder |
| where does the work happen? | the vendor's inference farm behind its REST API | the plugin pays per call: 5–62 credits, `VENDOR_API_KEY`, Pro plan |
| where does the money come from? | an account + credits on their API | `plugin-catalog/<vendor>.yaml` disclosure + their README |

So That vendor's product is **models + GPU capacity + billing**; the MCP server is the free, MIT-licensed
pipe that makes their API feel native inside an agent. That is why the catalog entry is 8 lines: the
thing being listed is a pointer to a repo, and the repo is a pointer to an API.

**Corollary for us:** "doing the same" is not a plugin change. The plugin is already the pipe (and
already installs from the catalog queue). What is missing is the **backend that has something the
user's laptop cannot do**, plus the billing in front of it.

## 2. What we actually have, and what is missing

| piece | state today | where |
|---|---|---|
| local thin client | **shipped** — `plugin/mcp.json` runs `uvx … s2docs mcp` over stdio, key-free, offline | this repo |
| doc index + lookups (convars, entities, schema, cs_script, Pulse, wiki) | **works**, 72 MCP tools | this repo |
| compile / deploy / evidence / job pipeline | **works**, bound to registered Windows hosts over SSH | this repo |
| concept image generation | **works** — ComfyUI (SDXL, IPAdapter, AnimateDiff) on the RTX 3070 behind Cloudflare Access | `comfyui.cpompa.com` |
| video generation | works on the RTX 5080 (pomps-pc) | live machine — never for tenants |
| image → 3D mesh | **missing** — no TRELLIS / Hunyuan3D / SF3D install anywhere in the fleet | — |
| Source 2 asset assembly (`.vmdl`/`.vmat`/`.vtex` + preview) | **missing** — the pipeline compiles and deploys, it does not author assets | this repo |
| multi-tenant keys, plans, unit metering, Stripe, webhooks | **built** | cpompa-ai-cloud (`/keys`, `/plans`, `/usage`, `/checkout/session`, Stripe webhooks) |
| public ingress | **built** — Cloudflare tunnel on the mini; hostnames `cpompa.com`, `api.cpompa.com` (:8030 prod), `ag-scheduler` | `~/.cloudflared/jackson-biz.yml` |

The gap is exactly the two stages that create the "wow": **image → mesh** and **mesh → a real Source 2
asset**. Everything in front of and behind them already exists.

## 3. What is worth selling (and what is a trap)

| stage | moat | capacity | verdict |
|---|---|---|---|
| doc lookups | none (it's our index, but anyone can rebuild it) | laptop | **give it away** — it is the funnel that installs the plugin |
| concept image (text → image) | low (any SDXL box does it) | RTX 3070, shared queue | bundle as a free/cheap step inside a paid asset job |
| **image → mesh** | medium-high (quality + wiring is hard) | RTX 5080 16 GB (own) or a provider API | **the product's front door** |
| **mesh → Source 2 asset** (scale, collision, materials, `.vmdl`/`.vmat`, preview) | **highest** — this is our automation + the index telling us what the engine accepts | CPU + Windows | **the moat**; nobody selling 3D assets does this. First gear shipped: `s2docs asset` writes a `.vmdl` from a mesh, and it **compiles** (below) |
| compile (resourcecompiler, VMap) | high per-run cost: a VMap compile is 20+ min of one Windows box | 1–2 boxes, one compile at a time | never sell raw minutes; bundle as a queued step with a per-job cap |
| "see it in game" (deploy + screenshot evidence) | high — needs the game, the host, the HUD hooks | same Windows boxes | the differentiator vs the vendor: *their* asset ends as a download; **ours ends as a frame from inside CS2** |

Pricing shape should follow the platform's existing single-tier + units model
(`docs/adr/0005-single-tier-pricing.md`), not a new ladder: one subscription, asset jobs metered in
units, a job's cost published *before* it runs (per-tool credit ranges are the precedent).

## 4. Architecture: Hammered is a product line, not a second stack

```
  Hermes + s2docs plugin            Hammered (hosted)                       fleet
  ───────────────────────           ─────────────────────                   ─────
  plugin/mcp.json  ──stdio──▶  s2docs mcp  ──https──▶  api.<hammered>  ──▶  Postgres (jobs, credits)
   (unchanged pipe)              client mode            FastAPI router        Redis (queue)
                                       │                        │             RTX 3070  ── concept
                                       └── offline tools ───────┘             RTX 5080  ── mesh
                                           keep working                         Windows  ── compile/deploy
```

- **Brand:** *Hammered* — the AI service that turns a text prompt into a Source 2 asset and back into
  the game. It rides the existing control plane (tenancy, keys, plans, units, Stripe, webhooks,
  ledger) rather than a new FastAPI app; the new surface is one router (`/v1/s2/*`) plus a worker.
- **Hostname:** a new ingress line in `~/.cloudflared/jackson-biz.yml` + a DNS record. Fastest path is
  `hammered.cpompa.com` (no purchase, token already in use); the branded path is a new domain
  (e.g. `hammered.gg` / `hammered.dev`) with `api.` pointed at the same tunnel. **Decision needed.**
- **Client:** the local server gains a *client mode* (`HAMMERED_API_KEY` + `HAMMERED_API_URL`). Hosted
  tools are advertised only when a key exists; without one the plugin is exactly what ships today.
  This is not a workaround — it is the only correct shape here: a portable `mcp.json` **cannot** pass a
  secret to a remote server (Agent Plugins v1 remote entries expand nothing in `url`/`headers`), which
  is precisely why the vendor ships an `npx` client holding `VENDOR_API_KEY` instead of a hosted URL entry.
- **Asset store:** job artifacts under the platform's storage, with signed URLs; a job's outputs are
  never public by default.
- **Windows/GPU nodes:** the existing node/PAIR fleet concept already models "hardware we own that
  serves workloads"; Hammered jobs are just another workload class, with the constraint that a
  compile slot is exclusive per box.

## 5. API contract v1 (what the client calls)

Async, task-shaped: submit → task id → poll (the plugin does the polling; the user sees progress).

| endpoint | body → result | units |
|---|---|---|
| `POST /v1/s2/concept` | `{prompt, game, style?, size?}` → `{task_id}` | 1–3 |
| `POST /v1/s2/mesh` | `{image_url \| source_task, target_tris, pivot?, route: "local"\|"provider"}` → `{task_id}` | 5–20 |
| `POST /v1/s2/asset` | `{mesh_task \| glb_url, game, name, materials?}` → `{task_id}` → `.vmdl`/`.vmat`/`.vtex` + preview PNG | 5–10 |
| `POST /v1/s2/ingame` | `{asset_task, host, addon, map?}` → `{task_id}` → deploy + screenshot evidence | 10 + compile |
| `GET /v1/s2/devices` | discovered fleet devices: GPU/VRAM, reachable worker ports, load, approval state | — |
| `POST /v1/s2/devices/{id}/approve` \| `/pause` | the HITL gate; names allowed workload classes + hours, writes an audit row | — |
| `GET /v1/s2/jobs/{id}` | `{status, phase, progress, route, artifacts[], error}` | — |
| `GET /v1/s2/usage` | units spent this window, per stage | — |
| `GET /v1/s2/catalog` | what this account may call, with unit ranges **before** the call | — |

Every job is metered once on settlement (the ledger already works that way), and the cost of a job is
shown to the user before it starts — the discipline those credit ranges imply.

## 6. Asset and licence policy (non-negotiable)

- **Never redistribute Valve assets.** Hammered ships *generated* assets only. An asset job's inputs
  may be a prompt or the user's own file; never a shipped `.vmdl`/`.vmat` extracted from the game.
- User keeps ownership of prompts and of the assets generated for them (same posture as the platform's
  pilot terms §6); Hammered claims a licence to run the job, nothing more.
- CS2/Deadlock addon distribution stays the user's act (Workshop/`addoninfo`), not ours.
- The index stays local and free: it is the reason the plugin is installed, and shipping it as an API
  invites Valve-adjacent redistribution questions we do not need.
- Telemetry: off by default; a job's prompt is retained only long enough to run it, with a documented
  retention window (mirror `docs/usage-retention.md`).

## 7. Phases

| phase | deliverable | acceptance |
|---|---|---|
| **0** (this doc) | brand, contract, decisions | decisions below answered |
| **1** | `/v1/s2/concept` on the existing control plane, metered, key-gated; ComfyUI worker on the 3070 | a key-holding client gets an image from a prompt; usage increments; a bad key 401s |
| **2** | client mode in `s2docs` (`HAMMERED_API_KEY`), hosted tools appear alongside local ones | a session with a key can call concept; without one, the install behaves exactly as today |
| **3** | image → mesh (TRELLIS/Hunyuan3D on the 5080, or a provider API behind the same task shape) | a mesh task returns a GLB that opens in the Desktop viewer |
| **4** | mesh → Source 2 asset + in-game proof (`.vmdl`/`.vmat` + deploy + screenshot) | one command, prompt → screenshot from inside CS2. *Partly landed 2026-10-23:* `s2docs asset` emits the `.vmdl`; a generated 498-byte document compiled on the real compiler (`resourcecompiler -i probe.vmdl -nop4` → `OK: 1 compiled, 0 failed`, artifact `probe.vmdl_c`). Materials (`.vmat`) still need a standard-shader example to copy rather than invent. |

Phase 4 is the one that makes this a business rather than a wrapper: the deliverable is a frame from
the game, not a file.

## 8. Decisions — answered 2026-10-23

| # | decision | answer |
|---|---|---|
| 1 | hostname | **`hammered.cpompa.com`** now (no purchase). `hammered.ai` is **taken** — RDAP: registered 2023-08-02, expires 2027-08-02, GoDaddy nameservers, `client*Prohibited`; `hammered.dev` also resolves to a live GCP site; `hammered.gg` has no A record but needs a registry check before assuming it is free. A new domain is a later branding move with `api.` → the same tunnel. |
| 2 | mesh route | **both, chosen per job**: `route: "local"` (TRELLIS/Hunyuan3D on our GPU) or `route: "provider"` (a hosted provider finish). The caller picks; the API never picks silently, and a job records which route it took. |
| 3 | which GPU serves tenants | **both boxes are candidates**, so the fleet gets a *device discovery* surface: a UI/API that scans the network for GPUs and ComfyUI servers (local or another box), shows what it found, and requires **HITL approval** before a device is allowed to take tenant work. Neither `desktop-pc` (3080, also the compile host) nor `pomps-pc` (5080, live machine) serves a tenant job until approved, and `pomps-pc` approval should be scoped (e.g. idle-only, paused on request). |
| 4 | billing | **Stripe test-mode through Phase 1–2**; live keys and the trading name go in when the asset pipeline produces an in-game frame. |

### Device discovery + HITL (Phase 1 requirement, not a nice-to-have)

- `GET /v1/s2/devices` — what the fleet can see: host, GPU + VRAM, driver, reachable ports, whether a
  ComfyUI (or other worker) answers, current load, and its approval state
  (`discovered → approved → serving → paused`). Discovery is a scan the platform runs, not a claim a
  client makes.
- `POST /v1/s2/devices/{id}/approve` / `.../pause` — the human gate. A discovered device is **listed
  but inert** until approved; approval names the workload classes it may take and the hours it may
  take them in. Every state change is an audit row.
- The desktop side gets the same view in the plugin (a devices page), so "which box is free" is one
  screen, not an SSH session.
- Reuses what exists: `/nodes`, `/pair-nodes`, `/pair-capacity` on the control plane already model
  "hardware we own that serves workloads"; this adds discovery + the approval workflow in front.

### Infrastructure already landed for this

- `~/.cloudflared/jackson-biz.yml`: `hammered.cpompa.com → http://localhost:8020` (dev control plane)
  added, config validated (`cloudflared tunnel ingress validate` → OK), tunnel restarted, and
  `cpompa.com` + `api.cpompa.com/health` re-probed 200 after the reload. Backup:
  `jackson-biz.yml.bak-20261022-233232`.
- **Still HITL (one dashboard action):** the DNS record — `CNAME hammered → 04232b6e-80c3-4a7e-81df-be06f6c12e30.cfargotunnel.com`, proxied. No `cert.pem` and no `CLOUDFLARE_API_TOKEN` on this machine, so `cloudflared tunnel route dns` cannot mint it. Until it exists the hostname does not resolve.
- Because :8020 is the **dev** control plane, put Cloudflare Access in front of `hammered.cpompa.com`
  for now, and flip the ingress target to `:8030` with the Phase-2 deploy.


## 9. What we will not do

- Put a secret in a portable `mcp.json` remote entry (it cannot expand one — and a literal key in a
  public repo is a leak, not a shortcut).
- Ship Valve-derived assets through the API, or make the index a paid endpoint.
- Run tenant jobs on the live 5080, or a compile on a host the user is using.
- Copy that vendor's models; we are not in the model-training business this quarter — the moat is the
  Source 2 half, where they have nothing.
