# triposr-worker — local image → mesh (Hammered `route: "local"`), build-pc RTX 3080

> Addresses and host names in this copy are placeholders (RFC 5737): `build-pc` = the Windows GPU
> build host, `192.0.2.10` = its LAN IP, `192.0.2.20` = the control-plane host ("the mini"), the only
> allowed client, `192.0.2.30` = any other LAN source, `198.51.100.10` = the build host's tailnet IP.
> Substitute the real values when deploying; the design (loopback publish + one portproxy door +
> a single-source firewall rule) is what matters.

TripoSR (VAST-AI-Research/TripoSR, MIT) behind a small async HTTP API in one container.
Weights `stabilityai/TripoSR` are public (HF API: `gated=False`, licence MIT) — no token needed.
Proven 2026-10-23 (numbers below); evidence in `proof\`.

## Run / stop

```powershell
cd D:\Docker\triposr
docker compose up -d --no-build     # uses the already-built image triposr-worker:local
docker compose down                 # stop + remove (outputs/weights are bind-mounted, nothing lost)
```

* Rebuild only when `Dockerfile`, `requirements.txt`, `constraints.txt` or `app\` change:
  `docker compose build --progress plain > build-N.log 2>&1` (BuildKit reuses the torch/CUDA layers).
* Weights (already present): `docker compose run --rm -e HF_HUB_OFFLINE=0 triposr python /app/fetch_weights.py`.
* `.env` (not in git, not in the image) holds `TSR_API_TOKEN=<64 hex>`. Compose refuses to start
  without it (`required variable TSR_API_TOKEN is missing a value`), and so does the server process itself. Rotate: new value in `.env`,
  then `docker compose up -d --no-build` (recreates the container).
* Idle behaviour: model loads on the first job (~20 s), unloads after `TSR_IDLE_UNLOAD_S=600` idle
  seconds and the process exits (`TSR_IDLE_EXIT=1`); `restart: unless-stopped` brings a fresh one
  up holding 0 MiB VRAM. Free the card now: `POST /v1/unload?recycle=true` (bearer).

## Reachability — loopback + one LAN door for the mini, nothing public

| layer | bind | who can reach it |
|---|---|---|
| Docker publish | `127.0.0.1:18191 -> 8000` | the box itself only (LAN/tailnet to :18191 get no answer) |
| Windows portproxy | `192.0.2.10:8191 -> 127.0.0.1:18191` | LAN IP only; nothing on the tailnet IP `198.51.100.10` |
| Firewall rule `TripoSR worker 8191 (mini only)` | TCP 8191, LocalAddress 192.0.2.10, RemoteAddress **192.0.2.20**, Private | the mini only (another source IP, `192.0.2.30`, times out while its :22 connects) |

No tunnel, no cloudflared (host service is Stopped/Disabled), no supervisor: the only process in
the container is `python -m uvicorn server:app`. Never publish `8191:8000` / `0.0.0.0`.
Remove the door: `netsh interface portproxy delete v4tov4 listenaddress=192.0.2.10 listenport=8191`
and `Remove-NetFirewallRule -DisplayName "TripoSR worker 8191 (mini only)"`.

## API (base `http://192.0.2.10:8191` from the mini, `http://127.0.0.1:18191` on the box)

`/v1/*` requires `Authorization: Bearer <TSR_API_TOKEN>` (401 without; compared with `hmac.compare_digest`).
The worker never fetches a URL itself — there is no URL-input code path: a request carrying `image_url` (or `url`) → 422, so send the image bytes.

| call | notes |
|---|---|
| `GET /` | service card (`kind: mesh`, `route: local`), no auth — the healthcheck/device probe |
| `GET /health` | torch/CUDA device, model loaded?, queue, whole-card `nvidia-smi`, `auth`, build info |
| `POST /v1/mesh` | multipart `image=@file` (+ fields) or JSON `{image_b64, ...}` → `202 {task_id}` |
| `GET /v1/jobs`, `GET /v1/jobs/{id}` | status, phase, progress, artifacts[], stats (timings, VRAM peaks), error |
| `GET /v1/jobs/{id}/files/{name}` | `mesh.glb`, `mesh.obj`, `input_processed.png` |
| `POST /v1/unload[?recycle=true]` | drop the model from VRAM (409 while busy) |

Options: `formats` `glb,obj` (default `glb`), `target_tris` (quadric decimation, colours re-queried),
`mc_resolution` 64–384 (256), `remove_bg` (true; skipped when the input already has alpha),
`foreground_ratio` (0.85), `pivot` `none|center|bottom`, `chunk_size` (8192), `threshold` (25).
Frames: `mesh.glb` is glTF (+Y up, front +Z); `mesh.obj` is TripoSR's raw frame (+Z up, front +X).
Output is geometry + per-vertex colour only: **no UVs, no materials/textures** (no texture bake).
Outputs: `triposr_data\outputs\<task_id>\` (last 200 jobs kept).

```bash
TOK=...   # from D:\Docker\triposr\.env
curl -H "Authorization: Bearer $TOK" -F image=@thing.png -F formats=glb,obj http://192.0.2.10:8191/v1/mesh
curl -H "Authorization: Bearer $TOK" http://192.0.2.10:8191/v1/jobs/<task_id>
```

## Pins (what decides the result)

| item | value |
|---|---|
| image | `triposr-worker:local` = `sha256:6ca78d4d4d57178d271796593cb960f8bda768f070e40e07f5b0568424b2870b` (6.31 GB, built 2026-10-24 01:54Z, `build-3-hardened.log`; every layer up to `COPY app/` reused from build 2, `sha256:2b9a2855…`) |
| base | `python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70` (Python 3.12.15) |
| torch | `2.5.1+cu124` (CUDA 12.4 runtime wheels, cuDNN 9.1.0.70); host driver 616.92 via `runtime: nvidia` |
| TripoSR | `107cefdc244c39106fa830359024f6a2f1c78871` (2026-06-04 "Update README.md"; = upstream main HEAD on 2026-10-23) |
| torchmcubes | `a9ac95aa329957ba646e7bb057f825f5dfa77f38` (CPU build: marching cubes on CPU, everything else on GPU) |
| Python deps | `constraints.txt` (pip freeze of build 1); what went in: `pip-freeze-build2.txt`; `build-info.json` (also in `/health`) |
| weights | `stabilityai/TripoSR@5b521936b01fbe1890f6f9baed0254ab6351c04a`: `model.ckpt` 1,677,246,742 B, sha256 `429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee`; `config.yaml` 987 B, sha256 `74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62` |
| | `facebook/dino-vitb16@f205d5d8e640a89a2b8ef0369670dfc37cc07fc2` `config.json` 454 B (architecture only; encoder weights are inside model.ckpt) |
| | rembg `u2net.onnx` 175,997,641 B, sha256 `8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491` |
| app | `app\server.py` sha256 `e48a43b14dd317d808d5144b0253056776397548867463758c5c45a5016a8ab9`, `app\security.py` sha256 `d320fea95a657848f7ff3655b431b8cc445da20ea49c54928e63b876561129dd` (identical in the image's `/app`) |

## Measured 2026-10-23 (container recreated from the image above)

* GPU inside the container: `docker exec triposr-worker python -c 'import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))'`
  → `2.5.1+cu124 12.4 True NVIDIA GeForce RTX 3080` (`proof\gpu-proof.txt`).
* Input: `C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\content\csgo\items\assets\sprays\workshop\example_spray.tga`
  (1024², RGBA, 4,194,335 B, sha256 `cc8d084b…81b5bd`).
* Cold job `eefa8331d9cf4056` (first after start): **28.4 s** submit→succeeded = 20.1 s model load +
  0.2 preprocess + 1.9 inference + 3.5 marching cubes + 0.2 export. Warm job `e06c7d7cbb704ccd`: **4.2 s**
  (byte-identical mesh).
* VRAM, host `nvidia-smi` sampled throughout the cold job (182 samples): baseline 890 MiB → **peak
  4,199 MiB of 10,240 → headroom 6,041 MiB**; the worker adds 3,309 MiB at peak, holds 2,861 MiB total
  while warm. In-container sampler agrees (peak 4,199 MiB, in marching cubes); torch peak reserved 3,042 MiB.
  That headroom assumes ComfyUI has nothing loaded and LM Studio has no model on the GPU — check
  `nvidia-smi` before co-scheduling with a VMap compile or a ComfyUI graph.
* Mesh: `triposr_data\outputs\eefa8331d9cf4056\mesh.glb` 1,175,796 B — trimesh 4.0.5: 29,401 vertices,
  58,696 triangles, vertex colours (COLOR_0), no UV, no material/texture/image, not watertight;
  `mesh.obj` 3,128,463 B (v 29,401 with rgb, f 58,696, vt 0, no mtllib).

Reproduce: `powershell -NoProfile -ExecutionPolicy Bypass -File D:\Docker\triposr\tools\run_proof.ps1`
(host VRAM sampler + submit + bounded poll + `app\validate_mesh.py`; writes `proof\run_*.json`,
`proof\gpu_trace_*.csv`, `proof\job_*.json`, `proof\validate_*.json`).

## Logs kept

`build.log` (build 1, unpinned; its image was deleted 2026-10-23), `build-2-pinned.log` (first pinned image), `build-3-hardened.log` (the image in use), `fetch.log` (weights),
`gpu_sample.csv` (build-1 VRAM samples), `pip-freeze-build1.txt`, `pip-freeze-build2.txt`, `build-info.json`,
`proof\compose-up-20261023.log`, `proof\container-log-20261023.log`, `proof\*` from the runs above.
`docker-compose.yml.bak-20261023-build1` = the compose before auth/URL-fetch hardening (rollback).
`*.bak-<timestamp>` next to each file = the version before the latest deploy (`.dockerignore` keeps them out of the image).

## Hardening (checklist: auth required, no server-side URL fetch, loopback + one door)

* **Refuses to start without a token.** `app/security.py:require_token()` runs at import of `server.py`
  and raises `SystemExit` when `TSR_API_TOKEN` is empty or unset, so the server itself refuses, not only
  compose (`${TSR_API_TOKEN:?…}`). `/health` therefore always reports `"auth": true`.
* **Constant-time bearer check.** `_auth` → `security.bearer_ok()` → `hmac.compare_digest` on bytes
  (a non-ASCII header is simply unequal). Every `/v1/*` route calls it; `GET /` and `GET /health` are open.
* **No URL inputs.** The `image_url` branch and `_fetch_url` are deleted (no `urllib` in the server); there is
  no setting to turn them back on. A multipart field or JSON key `image_url` / `url` → `422 {"detail": "URL
  inputs are not accepted: …"}`, checked after auth and before anything else.
* Tests (no torch needed): `pytest tests/test_triposr_security.py` — the helpers, plus static checks that
  `server.py` uses them and has no URL-fetch path, and that compose requires the token.

Deploy a change: copy `app\*.py` / `docker-compose.yml` / `.dockerignore` over with a `.bak-<timestamp>`
of each, `docker compose build --progress plain > build-N.log 2>&1` (only the `COPY app/` layer is rebuilt),
then `docker compose up -d --force-recreate` and check `/health` (`build`, `auth`) — a rebuild alone does not
replace the running container. The copy in the s2docs repo uses placeholder addresses in comments only; the
deployed `docker-compose.yml` carries the real ones and is otherwise identical.

## Readbacks (from the control-plane host; expected result after `->`)

```bash
B=http://192.0.2.10:8191
TOK=$(ssh build-pc '(Select-String -Path D:\Docker\triposr\.env -Pattern "^TSR_API_TOKEN=").Line.Substring(14)' | tr -d '\r\n')   # never echo it
curl -s -o /dev/null -w '%{http_code}\n' $B/v1/jobs                                              # -> 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer wrong-token" $B/v1/jobs       # -> 401  # gitleaks:allow (deliberately wrong token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" $B/v1/jobs              # -> 200
curl -s -w '\n%{http_code}\n' -H "Authorization: Bearer $TOK" -F image_url=http://192.0.2.20:8812/ $B/v1/mesh
                                                     # -> {"detail":"URL inputs are not accepted: …"} 422
curl -s $B/health | jq -c '{ok, auth, build}'        # -> auth true, build.triposr_sha 107cefdc…
# one real job: 202 {task_id,status,route,poll} -> poll GET /v1/jobs/{task_id} until status succeeded|failed
ID=$(curl -s -H "Authorization: Bearer $TOK" -F image=@thing.png -F formats=glb,obj $B/v1/mesh | jq -r .task_id)
curl -s -H "Authorization: Bearer $TOK" $B/v1/jobs/$ID | jq -c '{status, phase, error, artifacts: [.artifacts[].name]}'
curl -s -o mesh.glb -H "Authorization: Bearer $TOK" $B/v1/jobs/$ID/files/mesh.glb && head -c4 mesh.glb   # -> glTF
```

On the build host (PowerShell; exits 1 with `triposr-worker: TSR_API_TOKEN is empty or unset; refusing to start …`):

```powershell
docker run --rm -e TSR_API_TOKEN= --entrypoint python triposr-worker:local -c "import sys; sys.path.insert(0, '/app'); import server"
```

Recorded 2026-10-23 20:55 CDT on image `6ca78d4d…`: 401 / 401 / 200 / 422 (multipart and JSON) / `auth: true`,
`triposr_sha 107cefdc244c39106fa830359024f6a2f1c78871`; refusal exit 1 (empty and unset); a 512² PNG
→ job `succeeded` in 30.0 s cold (21.1 s model load), 77,176 triangles, `mesh.glb` 1,545,748 B starting
`glTF`, sha256 equal to the job's artifact record; the file route without a bearer → 401.

### Response shapes (what a client adapter reads)

* `POST /v1/mesh` → **202** `{"task_id", "status": "queued", "route": "local", "poll": "/v1/jobs/<task_id>"}`;
  errors: 401, 413 (> 32 MB), 422 (`detail`), 429 (queue full).
* `GET /v1/jobs/{task_id}` → `{"task_id", "status", "phase", "progress", "route", "model", "artifacts",
  "stats", "error", "options", "created_at", "started_at", "finished_at", "input"}`.
  `status` ∈ `queued | running | succeeded | failed`; `phase` ∈ `queued | loading_model | preprocess |
  inference | extract_mesh | decimate | export | done`; `progress` 0–1; `error` null or a string;
  timestamps are epoch seconds.
  Each artifact: `{"name", "url", "bytes", "sha256", "content_type", "host_path"}` (`url` is relative,
  e.g. `/v1/jobs/<id>/files/mesh.glb`).
* `GET /v1/jobs` → `{"jobs": [{"task_id", "status", "phase", "progress", "created_at", "finished_at"}]}`.
