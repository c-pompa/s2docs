"""One-shot weight fetch into the bind-mounted /models (run with HF_HUB_OFFLINE=0).

  docker compose run --rm -e HF_HUB_OFFLINE=0 triposr python /app/fetch_weights.py

No login is used on purpose; stabilityai/TripoSR is public (gated=False). If this ever
starts answering 401/403 the repo became gated and needs a token decision -- do not
paste one here.
"""
import os
import time

from huggingface_hub import hf_hub_download

REPO = os.environ.get("TSR_MODEL_REPO", "stabilityai/TripoSR")
REV = os.environ.get("TSR_MODEL_REVISION", "5b521936b01fbe1890f6f9baed0254ab6351c04a")

t0 = time.time()
for name in ("config.yaml", "model.ckpt"):
    path = hf_hub_download(REPO, name, revision=REV)
    print(f"{REPO}@{REV[:7]} {name}: {path} ({os.path.getsize(path):,} bytes)")
path = hf_hub_download("facebook/dino-vitb16", "config.json")
print(f"facebook/dino-vitb16 config.json: {path} ({os.path.getsize(path):,} bytes)")

import rembg  # noqa: E402  (downloads u2net.onnx into $U2NET_HOME on first session)

rembg.new_session("u2net")
u2 = os.path.join(os.environ.get("U2NET_HOME", os.path.expanduser("~/.u2net")), "u2net.onnx")
print(f"rembg u2net: {u2} ({os.path.getsize(u2):,} bytes)")
print(f"done in {time.time() - t0:.1f}s")
