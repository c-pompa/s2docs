"""TripoSR image -> mesh worker: the Hammered `route: "local"` mesh stage.

LAN-only HTTP API (loopback publish + one firewalled portproxy door; no tunnels).
`Authorization: Bearer <TSR_API_TOKEN>` is required on /v1/*, and the process refuses to
start without TSR_API_TOKEN. The worker never fetches URLs: a request with `image_url` gets 422.

  GET  /                          service card (the Hammered device probe hits this)
  GET  /health                    torch/CUDA device, model state, queue, nvidia-smi
  POST /v1/mesh                   multipart `image=@file` (+ form fields) or JSON
                                  {image_b64, target_tris, mc_resolution,
                                   remove_bg, foreground_ratio, formats, pivot}
                                  -> 202 {task_id, status, route}
  GET  /v1/jobs                   recent jobs
  GET  /v1/jobs/{id}              {status, phase, progress, route, artifacts[], stats, error}
  GET  /v1/jobs/{id}/files/{name} artifact download
  POST /v1/unload                 drop the model from VRAM now

One GPU job at a time. The model loads on first use and is unloaded after
TSR_IDLE_UNLOAD_S idle seconds so the shared 10 GB card is free for ComfyUI.
"""
from __future__ import annotations

import base64
import gc
import hashlib
import io
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from security import URL_INPUT_REFUSAL, bearer_ok, require_token, url_input_keys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("triposr-worker")

MODEL_REPO = os.environ.get("TSR_MODEL_REPO", "stabilityai/TripoSR")
MODEL_REV = os.environ.get("TSR_MODEL_REVISION", "5b521936b01fbe1890f6f9baed0254ab6351c04a")
OUTPUT_DIR = Path(os.environ.get("TSR_OUTPUT_DIR", "/outputs"))
HOST_OUTPUT_DIR = os.environ.get("TSR_HOST_OUTPUT_DIR", "")  # informational: where OUTPUT_DIR lives on the host
IDLE_UNLOAD_S = float(os.environ.get("TSR_IDLE_UNLOAD_S", "600"))
CHUNK_SIZE = int(os.environ.get("TSR_CHUNK_SIZE", "8192"))
MAX_QUEUE = int(os.environ.get("TSR_MAX_QUEUE", "8"))
MAX_UPLOAD_BYTES = int(os.environ.get("TSR_MAX_UPLOAD_MB", "32")) * 1024 * 1024
MAX_SIDE = int(os.environ.get("TSR_MAX_INPUT_SIDE", "2048"))
KEEP_JOBS = int(os.environ.get("TSR_KEEP_JOBS", "200"))
API_TOKEN = require_token()  # SystemExit when TSR_API_TOKEN is empty/unset: never serve unauthenticated
DEVICE_REQ = os.environ.get("TSR_DEVICE", "cuda:0")
REQUIRE_CUDA = os.environ.get("TSR_REQUIRE_CUDA", "1") == "1"
PRELOAD = os.environ.get("TSR_PRELOAD", "0") == "1"
# After an idle unload, exit the process so Docker (restart: unless-stopped) brings up a fresh
# one: that also releases the ~300 MB CUDA context and ~1 GB of host RAM the CUDA libs hold.
IDLE_EXIT = os.environ.get("TSR_IDLE_EXIT", "1") == "1"
MAX_ATTEMPTS = 2
BUILD_INFO_PATH = os.environ.get("TSR_BUILD_INFO", "/opt/build-info.json")


def build_info() -> dict | None:
    """What the image was built from (written at build time; see Dockerfile)."""
    try:
        return json.loads(Path(BUILD_INFO_PATH).read_text())
    except (OSError, ValueError):
        return None
Image.MAX_IMAGE_PIXELS = 64_000_000

FORMATS = {"glb": "model/gltf-binary", "obj": "text/plain"}
PIVOTS = {"none", "center", "bottom"}

# --------------------------------------------------------------------------- GPU facts


def _nvsmi() -> str | None:
    for p in (shutil.which("nvidia-smi"), "/usr/lib/wsl/lib/nvidia-smi", "/usr/bin/nvidia-smi"):
        if p and os.path.exists(p):
            return p
    return None


def gpu_mem() -> dict | None:
    """Whole-card memory as nvidia-smi sees it (torch under-reports under WDDM)."""
    exe = _nvsmi()
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip().splitlines()[0]
        name, used, total, util = [x.strip() for x in out.split(",")]
        return {"name": name, "memory_used_mib": int(used), "memory_total_mib": int(total),
                "utilization_pct": int(util) if util.isdigit() else None}
    except Exception:  # noqa: BLE001 - a missing/odd nvidia-smi is data, not an error
        return None


class VramSampler(threading.Thread):
    """Polls nvidia-smi (whole card) during a job; keeps the peak overall and per job phase.

    The phase is read right after each nvidia-smi call returns (the query happens at the
    end of the call, after NVML init), so a sample is tagged with the phase it measured.
    """

    def __init__(self, job: dict | None = None, interval: float = 0.05):
        super().__init__(daemon=True)
        self.job, self.interval = job, interval
        self.peak, self.first, self.samples = None, None, 0
        self.by_phase: dict[str, dict] = {}
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            g = gpu_mem()
            phase = (self.job or {}).get("phase", "?")
            if g:
                used = g["memory_used_mib"]
                self.first = used if self.first is None else self.first
                self.peak = used if self.peak is None else max(self.peak, used)
                self.samples += 1
                slot = self.by_phase.setdefault(phase, {"peak_mib": used, "samples": 0})
                slot["peak_mib"] = max(slot["peak_mib"], used)
                slot["samples"] += 1
            self._stop_evt.wait(self.interval)

    def stop(self) -> dict:
        self._stop_evt.set()
        self.join(timeout=10)
        return {"nvsmi_first_mib": self.first, "nvsmi_peak_mib": self.peak, "nvsmi_samples": self.samples,
                "nvsmi_by_phase": self.by_phase}


def device_info() -> dict:
    info: dict[str, Any] = {"torch": torch.__version__, "torch_cuda_build": torch.version.cuda,
                            "cuda_available": torch.cuda.is_available()}
    if info["cuda_available"]:
        idx = torch.device(DEVICE_REQ).index or 0
        info.update(device=f"cuda:{idx}", name=torch.cuda.get_device_name(idx),
                    capability=list(torch.cuda.get_device_capability(idx)))
    else:
        info["device"] = "cpu"
    return info


# --------------------------------------------------------------------------- model


class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.model = None
        self.rembg = None
        self.device: str | None = None
        self.loaded_at: float | None = None
        self.load_seconds: float | None = None
        self.last_used = time.time()
        self.busy = False

    def ensure_loaded(self) -> bool:
        """Load TripoSR (+ the rembg session). Returns True if this call loaded it."""
        with self.lock:
            if self.model is not None:
                return False
            if not torch.cuda.is_available() and REQUIRE_CUDA:
                raise RuntimeError("CUDA is not available in this container (NVIDIA runtime missing?) "
                                   "and TSR_REQUIRE_CUDA=1 - refusing to run TripoSR on CPU")
            device = DEVICE_REQ if torch.cuda.is_available() else "cpu"
            t0 = time.time()
            from huggingface_hub import hf_hub_download
            from omegaconf import OmegaConf
            from tsr.system import TSR

            cfg_path = hf_hub_download(MODEL_REPO, "config.yaml", revision=MODEL_REV)
            ckpt_path = hf_hub_download(MODEL_REPO, "model.ckpt", revision=MODEL_REV)
            cfg = OmegaConf.load(cfg_path)
            OmegaConf.resolve(cfg)
            # Allocate the parameters straight on the GPU (no 1.7 GB CPU copy), then
            # stream the checkpoint in from an mmap: keeps host RAM low in the shared VM.
            try:
                with torch.device(device):
                    model = TSR(cfg)
            except Exception as exc:  # noqa: BLE001 - fall back to the upstream CPU-then-move path
                LOG.warning("on-device construction failed (%s); building on CPU and moving", exc)
                model = TSR(cfg).to(device)
            state = None
            for kwargs in ({"weights_only": True, "mmap": True}, {"weights_only": True}, {"weights_only": False}):
                try:
                    state = torch.load(ckpt_path, map_location="cpu", **kwargs)
                    break
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("torch.load(%s) failed: %s", kwargs, exc)
            if state is None:
                raise RuntimeError("could not read model.ckpt")
            model.load_state_dict(state)
            del state
            model.renderer.set_chunk_size(CHUNK_SIZE)
            model.eval()
            import rembg

            self.rembg = rembg.new_session("u2net")
            self.model, self.device = model, device
            self.load_seconds = round(time.time() - t0, 2)
            self.loaded_at = time.time()
            gc.collect()
            LOG.info("TripoSR %s@%s loaded on device=%s in %.1fs (%s)", MODEL_REPO, MODEL_REV[:7], device,
                     self.load_seconds, device_info().get("name", "cpu"))
            return True

    def unload(self, reason: str) -> bool:
        with self.lock:
            if self.model is None or self.busy:
                return False
            self.model, self.rembg = None, None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            self.loaded_at = None
            LOG.info("model unloaded (%s)", reason)
            return True


ENGINE = Engine()

# --------------------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBQ: queue.Queue[str] = queue.Queue(maxsize=MAX_QUEUE)


def _now() -> float:
    return round(time.time(), 3)


def _save(job: dict) -> None:
    d = OUTPUT_DIR / job["task_id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(d / "job.json")


def _phase(job: dict, phase: str, progress: float) -> None:
    job["phase"], job["progress"] = phase, progress
    job.setdefault("phase_log", []).append({"phase": phase, "t": _now()})
    _save(job)


def _public(job: dict) -> dict:
    keys = ("task_id", "status", "phase", "progress", "route", "model", "artifacts", "stats", "error",
            "options", "created_at", "started_at", "finished_at", "input")
    return {k: job.get(k) for k in keys}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _artifact(job: dict, path: Path, ctype: str) -> dict:
    art = {"name": path.name, "url": f"/v1/jobs/{job['task_id']}/files/{path.name}",
           "bytes": path.stat().st_size, "sha256": _sha256(path), "content_type": ctype}
    if HOST_OUTPUT_DIR:
        art["host_path"] = HOST_OUTPUT_DIR.rstrip("\\/") + "\\" + job["task_id"] + "\\" + path.name
    return art


def _limit_side(img: Image.Image) -> Image.Image:
    if max(img.size) > MAX_SIDE:
        img = img.copy()
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return img


def _decimate_and_recolor(mesh, target: int, scene_code, device: str):
    """Quadric-decimate to ~target triangles, then re-query vertex colours from the triplane."""
    import fast_simplification
    import trimesh

    n = len(mesh.faces)
    if not target or n <= target:
        return mesh, False
    reduction = 1.0 - float(target) / n
    v_in = np.ascontiguousarray(mesh.vertices, dtype=np.float32)
    f_in = np.ascontiguousarray(mesh.faces)
    v_out, f_out = fast_simplification.simplify(v_in, f_in, target_reduction=reduction)
    with torch.no_grad():
        col = ENGINE.model.renderer.query_triplane(
            ENGINE.model.decoder, torch.from_numpy(np.ascontiguousarray(v_out, dtype=np.float32)).to(device),
            scene_code)["color"]
    out = trimesh.Trimesh(vertices=v_out, faces=f_out, vertex_colors=col.cpu().numpy(), process=False)
    return out, True


def _apply_pivot(mesh, pivot: str) -> None:
    """In TripoSR's own frame (+Z up, front +X)."""
    if pivot == "none":
        return
    lo, hi = mesh.bounds
    centre = (lo + hi) / 2.0
    if pivot == "center":
        mesh.apply_translation(-centre)
    elif pivot == "bottom":
        mesh.apply_translation([-centre[0], -centre[1], -lo[2]])


def _to_gltf_frame(mesh):
    """TripoSR (+Z up, faces +X, image-right +Y) -> glTF (+Y up, faces +Z): (x,y,z) -> (y,z,x)."""
    m = np.array([[0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
    g = mesh.copy()
    g.apply_transform(m)
    return g


def process(job: dict) -> None:
    from tsr.utils import remove_background, resize_foreground

    d = OUTPUT_DIR / job["task_id"]
    opts = job["options"]
    job["status"], job["started_at"] = "running", _now()
    stats: dict[str, Any] = {"queue_wait_s": round(job["started_at"] - job["created_at"], 2)}
    t_job = time.time()
    sampler = VramSampler(job)
    sampler.start()
    scene_codes = None
    torch_peaks: dict[str, dict] = {}

    def torch_mark(phase: str) -> None:
        """Record torch's own peak for the phase just finished, then reset for the next one."""
        if torch.cuda.is_available() and ENGINE.device and ENGINE.device.startswith("cuda"):
            torch.cuda.synchronize()
            torch_peaks[phase] = {"allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20),
                                  "reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20)}
            torch.cuda.reset_peak_memory_stats()

    # Mark busy BEFORE loading so the idle unloader can never drop a model a job is about to use.
    ENGINE.busy, ENGINE.last_used = True, time.time()
    try:
        _phase(job, "loading_model", 0.05)
        t = time.time()
        loaded_now = ENGINE.ensure_loaded()
        stats["model_load_s"] = round(time.time() - t, 2) if loaded_now else 0.0
        stats["model_was_warm"] = not loaded_now
        with ENGINE.lock:
            device = ENGINE.device
            stats["device"] = device
            if device.startswith("cuda"):
                torch.cuda.reset_peak_memory_stats()
                stats["gpu_name"] = torch.cuda.get_device_name(torch.device(device))
            LOG.info("job %s running on device=%s", job["task_id"], device)

            _phase(job, "preprocess", 0.15)
            t = time.time()
            img = Image.open(d / job["input"]["stored_as"])
            img.load()
            stats["input_size"] = list(img.size)
            stats["input_mode"] = img.mode
            img = _limit_side(img)
            if opts["remove_bg"]:
                img = remove_background(img, ENGINE.rembg)
                img = resize_foreground(img, opts["foreground_ratio"])
                arr = np.array(img).astype(np.float32) / 255.0
                arr = arr[:, :, :3] * arr[:, :, 3:4] + (1 - arr[:, :, 3:4]) * 0.5
                img = Image.fromarray((arr * 255.0).astype(np.uint8))
            else:
                img = img.convert("RGB")
            img.save(d / "input_processed.png")
            stats["preprocess_s"] = round(time.time() - t, 2)
            torch_mark("preprocess")

            _phase(job, "inference", 0.35)
            t = time.time()
            with torch.no_grad():
                scene_codes = ENGINE.model([img], device=device)
            if device.startswith("cuda"):
                torch.cuda.synchronize()
            stats["inference_s"] = round(time.time() - t, 2)
            torch_mark("inference")

            _phase(job, "extract_mesh", 0.6)
            t = time.time()
            ENGINE.model.renderer.set_chunk_size(opts["chunk_size"])
            try:
                mesh = ENGINE.model.extract_mesh(scene_codes, True, resolution=opts["mc_resolution"],
                                                 threshold=opts["threshold"])[0]
            finally:
                ENGINE.model.renderer.set_chunk_size(CHUNK_SIZE)
            stats["extract_mesh_s"] = round(time.time() - t, 2)
            torch_mark("extract_mesh")
            stats["raw_triangles"] = int(len(mesh.faces))
            stats["raw_vertices"] = int(len(mesh.vertices))
            if len(mesh.faces) == 0:
                raise RuntimeError("marching cubes produced an empty mesh (no foreground above threshold)")

            if opts["target_tris"]:
                _phase(job, "decimate", 0.75)
                t = time.time()
                mesh, did = _decimate_and_recolor(mesh, opts["target_tris"], scene_codes[0], device)
                stats["decimated"] = did
                stats["decimate_s"] = round(time.time() - t, 2)
                torch_mark("decimate")

            if torch_peaks:
                stats["torch_peaks_by_phase"] = torch_peaks
                stats["torch_peak_allocated_mib"] = max(p["allocated_mib"] for p in torch_peaks.values())
                stats["torch_peak_reserved_mib"] = max(p["reserved_mib"] for p in torch_peaks.values())

        _phase(job, "export", 0.9)
        t = time.time()
        _apply_pivot(mesh, opts["pivot"])
        artifacts = []
        for fmt in opts["formats"]:
            path = d / f"mesh.{fmt}"
            (_to_gltf_frame(mesh) if fmt == "glb" else mesh).export(str(path))
            artifacts.append(_artifact(job, path, FORMATS[fmt]))
        artifacts.append(_artifact(job, d / "input_processed.png", "image/png"))
        stats["export_s"] = round(time.time() - t, 2)
        stats["triangles"] = int(len(mesh.faces))
        stats["vertices"] = int(len(mesh.vertices))
        stats["watertight"] = bool(mesh.is_watertight)
        stats["bounds_triposr_frame"] = [[round(float(x), 4) for x in row] for row in mesh.bounds]
        stats["frames"] = {"glb": "glTF: +Y up, front +Z", "obj": "TripoSR raw: +Z up, front +X (Source-style)"}
        job["artifacts"] = artifacts
        job["status"], job["error"] = "succeeded", None
        _phase(job, "done", 1.0)
    except torch.cuda.OutOfMemoryError as exc:
        job["status"], job["error"] = "failed", f"cuda_oom: {exc}".strip()[:2000]
        LOG.error("job %s CUDA OOM: %s", job["task_id"], exc)
    except Exception as exc:  # noqa: BLE001
        job["status"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        LOG.error("job %s failed:\n%s", job["task_id"], traceback.format_exc())
    finally:
        del scene_codes
        stats.update(sampler.stop())
        stats["job_s"] = round(time.time() - t_job, 2)
        job["stats"] = stats
        job["finished_at"] = _now()
        stats["submit_to_done_s"] = round(job["finished_at"] - job["created_at"], 2)
        ENGINE.busy = False
        ENGINE.last_used = time.time()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        _save(job)
        LOG.info("job %s %s in %.2fs (tris=%s, nvsmi_peak=%s MiB)", job["task_id"], job["status"],
                 stats["job_s"], stats.get("triangles"), stats.get("nvsmi_peak_mib"))
        _prune()


def _prune() -> None:
    with JOBS_LOCK:
        done = sorted((j for j in JOBS.values() if j["status"] in ("succeeded", "failed")),
                      key=lambda j: j["created_at"])
        for j in done[: max(0, len(JOBS) - KEEP_JOBS)]:
            shutil.rmtree(OUTPUT_DIR / j["task_id"], ignore_errors=True)
            JOBS.pop(j["task_id"], None)


def worker_loop() -> None:
    while True:
        jid = JOBQ.get()
        try:
            job = JOBS.get(jid)
            if job is not None:
                process(job)
        except Exception:  # noqa: BLE001
            LOG.error("worker loop error:\n%s", traceback.format_exc())
        finally:
            JOBQ.task_done()


def _recycle(reason: str) -> None:
    """Exit so Docker restarts a fresh process (no CUDA context, ~250 MB RAM)."""
    if ENGINE.busy or not JOBQ.empty():
        LOG.info("recycle (%s) skipped: work arrived", reason)
        return
    LOG.info("recycling worker process (%s) to release the CUDA context; Docker restarts it", reason)
    logging.shutdown()
    os._exit(0)


def idle_loop() -> None:
    while True:
        time.sleep(10)
        if (IDLE_UNLOAD_S > 0 and ENGINE.model is not None and not ENGINE.busy and JOBQ.empty()
                and time.time() - ENGINE.last_used > IDLE_UNLOAD_S
                and ENGINE.unload(f"idle > {IDLE_UNLOAD_S:.0f}s") and IDLE_EXIT):
            _recycle("idle")


def _restore_jobs() -> None:
    """Reload job records; re-queue work a previous process accepted but did not finish."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pending = []
    for jf in OUTPUT_DIR.glob("*/job.json"):
        try:
            job = json.loads(jf.read_text())
        except (OSError, ValueError):
            continue
        if job.get("status") in ("queued", "running"):
            if job["status"] == "running":
                job["attempts"] = job.get("attempts", 0) + 1
            if job.get("attempts", 0) >= MAX_ATTEMPTS:
                job["status"], job["error"] = "failed", "worker died twice while running this job"
            else:
                job.update(status="queued", phase="queued", progress=0.0, started_at=None)
                pending.append(job)
            _save(job)
        JOBS[job["task_id"]] = job
    for job in sorted(pending, key=lambda j: j["created_at"]):
        try:
            JOBQ.put_nowait(job["task_id"])
            LOG.info("re-queued unfinished job %s", job["task_id"])
        except queue.Full:
            job["status"], job["error"] = "failed", "queue full after restart"
            _save(job)


# --------------------------------------------------------------------------- HTTP


@asynccontextmanager
async def lifespan(_: FastAPI):
    _restore_jobs()
    info = device_info()
    LOG.info("startup: torch=%s cuda_build=%s cuda_available=%s device=%s name=%s capability=%s",
             info["torch"], info["torch_cuda_build"], info["cuda_available"], info["device"],
             info.get("name"), info.get("capability"))
    LOG.info("build: %s", json.dumps(build_info()))
    threading.Thread(target=worker_loop, daemon=True, name="gpu-worker").start()
    threading.Thread(target=idle_loop, daemon=True, name="idle-unload").start()
    if PRELOAD:
        threading.Thread(target=ENGINE.ensure_loaded, daemon=True, name="preload").start()
    yield


app = FastAPI(title="triposr-worker", version="1.0", lifespan=lifespan)


def _auth(request: Request) -> None:
    """Constant-time bearer check (security.bearer_ok -> hmac.compare_digest). Always enforced."""
    if not bearer_ok(request.headers.get("authorization", ""), API_TOKEN):
        raise HTTPException(401, "missing or bad bearer token")


def _refuse_url_inputs(params: dict) -> None:
    """The worker never downloads on a client's behalf (SSRF into the LAN/tailnet)."""
    if url_input_keys(params):
        raise HTTPException(422, URL_INPUT_REFUSAL)


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _opts(p: dict) -> dict:
    def num(key, cast, default, lo, hi):
        raw = p.get(key)
        if raw in (None, ""):
            return default
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{key} must be a {cast.__name__}") from None
        if not lo <= val <= hi:
            raise HTTPException(422, f"{key} must be in [{lo}, {hi}]")
        return val

    fmts = p.get("formats") or p.get("format") or ["glb"]
    if isinstance(fmts, str):
        fmts = [f.strip() for f in fmts.replace(";", ",").split(",") if f.strip()]
    fmts = list(dict.fromkeys(f.lower() for f in fmts))
    if not fmts or any(f not in FORMATS for f in fmts):
        raise HTTPException(422, f"formats must be a subset of {sorted(FORMATS)}")
    pivot = str(p.get("pivot") or "none").lower()
    if pivot not in PIVOTS:
        raise HTTPException(422, f"pivot must be one of {sorted(PIVOTS)}")
    route = str(p.get("route") or "local").lower()
    if route != "local":
        raise HTTPException(422, "this worker only serves route=local")
    target = p.get("target_tris")
    return {
        "mc_resolution": num("mc_resolution", int, 256, 64, 384),
        "remove_bg": _bool(p.get("remove_bg"), True),
        "foreground_ratio": num("foreground_ratio", float, 0.85, 0.5, 1.0),
        "target_tris": None if target in (None, "", 0, "0") else num("target_tris", int, None, 200, 1_000_000),
        "formats": fmts,
        "pivot": pivot,
        "chunk_size": num("chunk_size", int, CHUNK_SIZE, 1024, 131072),
        "threshold": num("threshold", float, 25.0, 0.1, 200.0),
    }


@app.get("/")
def root():
    return {"service": "triposr-worker", "kind": "mesh", "route": "local",
            "model": f"{MODEL_REPO}@{MODEL_REV[:7]}", "ok": True,
            "api": ["GET /health", "POST /v1/mesh", "GET /v1/jobs", "GET /v1/jobs/{id}",
                    "GET /v1/jobs/{id}/files/{name}", "POST /v1/unload"]}


@app.get("/health")
def health():
    info = device_info()
    ok = info["cuda_available"] or not REQUIRE_CUDA
    return {"ok": ok, **info, "model": f"{MODEL_REPO}@{MODEL_REV}", "model_loaded": ENGINE.model is not None,
            "model_load_s": ENGINE.load_seconds, "busy": ENGINE.busy, "queue_depth": JOBQ.qsize(),
            "max_queue": MAX_QUEUE, "idle_unload_s": IDLE_UNLOAD_S, "chunk_size": CHUNK_SIZE,
            "auth": bool(API_TOKEN), "idle_exit": IDLE_EXIT, "gpu": gpu_mem(), "build": build_info()}


@app.post("/v1/mesh", status_code=202)
async def submit(request: Request):
    _auth(request)
    ctype = request.headers.get("content-type", "")
    params: dict[str, Any] = {}
    filename = "upload"
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        _refuse_url_inputs(form)
        up = form.get("image")
        if up is None or not hasattr(up, "read"):
            raise HTTPException(422, "multipart field 'image' (a file) is required")
        data = await up.read(MAX_UPLOAD_BYTES + 1)
        filename = up.filename or filename
        params = {k: v for k, v in form.items() if k != "image"}
        if isinstance(params.get("formats"), str) and params["formats"].startswith("["):
            params["formats"] = json.loads(params["formats"])
    else:
        try:
            params = await request.json()
        except ValueError:
            raise HTTPException(422, "send multipart (image=@file) or a JSON body") from None
        if not isinstance(params, dict):
            raise HTTPException(422, "JSON body must be an object")
        _refuse_url_inputs(params)
        if params.get("image_b64"):
            try:
                data = base64.b64decode(params.pop("image_b64"), validate=False)
            except ValueError:
                raise HTTPException(422, "image_b64 is not valid base64") from None
        else:
            raise HTTPException(422, "one of image (multipart) or image_b64 is required")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"image larger than {MAX_UPLOAD_BYTES // 2**20} MB")
    opts = _opts(params)
    try:
        probe = Image.open(io.BytesIO(data))
        fmt, size = (probe.format or "img").lower(), probe.size
        probe.verify()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"not a readable image: {exc}") from None
    if fmt in ("mpo",):
        fmt = "jpeg"
    if JOBQ.full():
        raise HTTPException(429, f"queue full ({MAX_QUEUE} jobs); retry later")

    jid = uuid.uuid4().hex[:16]
    d = OUTPUT_DIR / jid
    d.mkdir(parents=True, exist_ok=True)
    stored = f"input.{fmt}"
    (d / stored).write_bytes(data)
    job = {"task_id": jid, "status": "queued", "phase": "queued", "progress": 0.0, "route": "local",
           "model": f"{MODEL_REPO}@{MODEL_REV}", "options": opts, "artifacts": [], "stats": {}, "error": None,
           "created_at": _now(), "started_at": None, "finished_at": None,
           "input": {"filename": filename, "stored_as": stored, "format": fmt, "size": list(size),
                     "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    with JOBS_LOCK:
        JOBS[jid] = job
    _save(job)
    try:
        JOBQ.put_nowait(jid)
    except queue.Full:
        job["status"], job["error"] = "failed", "queue full"
        _save(job)
        raise HTTPException(429, "queue full; retry later") from None
    return {"task_id": jid, "status": "queued", "route": "local", "poll": f"/v1/jobs/{jid}"}


@app.get("/v1/jobs")
def list_jobs(request: Request, limit: int = 20):
    _auth(request)
    jobs = sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)[: max(1, min(limit, 200))]
    return {"jobs": [{k: j.get(k) for k in ("task_id", "status", "phase", "progress", "created_at",
                                             "finished_at")} for j in jobs]}


@app.get("/v1/jobs/{task_id}")
def get_job(task_id: str, request: Request):
    _auth(request)
    job = JOBS.get(task_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return _public(job)


@app.get("/v1/jobs/{task_id}/files/{name}")
def get_file(task_id: str, name: str, request: Request):
    _auth(request)
    job = JOBS.get(task_id)
    if job is None:
        raise HTTPException(404, "no such job")
    art = next((a for a in job.get("artifacts") or [] if a["name"] == name), None)
    if art is None:
        raise HTTPException(404, "no such artifact")
    return FileResponse(OUTPUT_DIR / task_id / name, media_type=art["content_type"], filename=name)


@app.post("/v1/unload")
def unload(request: Request, recycle: bool = False):
    """Drop the model from VRAM; `?recycle=true` also restarts the process (frees the CUDA context)."""
    _auth(request)
    if ENGINE.busy or not JOBQ.empty():
        raise HTTPException(409, "busy; try again when the queue is empty")
    dropped = ENGINE.unload("requested")
    if recycle:
        threading.Timer(0.5, _recycle, args=("requested",)).start()
    return {"unloaded": dropped, "recycling": recycle, "gpu": gpu_mem()}
