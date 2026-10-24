# syntax=docker/dockerfile:1
# TripoSR (VAST-AI-Research/TripoSR, MIT) image -> mesh worker for the Hammered
# `route: "local"` mesh stage. No supervisor, no tunnel, no UI: one uvicorn process.
# CUDA = torch cu124 wheels + the NVIDIA runtime injected at `docker run` (runtime: nvidia).
#
# Everything that decides the result is pinned: base image by digest, torch by exact local
# version, TripoSR + torchmcubes by commit, every Python package by constraints.txt (a
# `pip freeze` of the first verified build). /opt/build-info.json and /opt/pip-freeze.txt
# record what actually went in; GET /health echoes build-info.
ARG PY_IMAGE=python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
ARG TORCH_VERSION=2.5.1
ARG TRIPOSR_SHA=107cefdc244c39106fa830359024f6a2f1c78871
ARG TORCHMCUBES_SHA=a9ac95aa329957ba646e7bb057f825f5dfa77f38

# ---- stage 1: torchmcubes wheel (CPU marching-cubes kernels) built against the same torch minor,
# plus the TripoSR source at the pinned commit. No nvcc here, so torchmcubes runs MC on the CPU;
# TripoSR's isosurface.py handles that. Encoder, transformer and triplane queries stay on the GPU.
FROM ${PY_IMAGE} AS mcubes
ARG TORCH_VERSION
ARG TORCHMCUBES_SHA
ARG TRIPOSR_SHA
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN pip install "torch==${TORCH_VERSION}+cpu" --index-url https://download.pytorch.org/whl/cpu \
 && pip install "cmake>=3.24,<4" ninja "scikit-build-core>=0.10" "pybind11>=2.10" numpy==1.26.4
RUN CMAKE_PREFIX_PATH="$(python -c 'import torch; print(torch.utils.cmake_prefix_path)')" \
    pip wheel --no-build-isolation --no-deps -w /wheels \
      "git+https://github.com/tatsy/torchmcubes.git@${TORCHMCUBES_SHA}"
RUN git clone --filter=blob:none https://github.com/VAST-AI-Research/TripoSR.git /src/TripoSR \
 && git -C /src/TripoSR checkout --detach "${TRIPOSR_SHA}" \
 && test "$(git -C /src/TripoSR rev-parse HEAD)" = "${TRIPOSR_SHA}" \
 && git -C /src/TripoSR rev-parse HEAD > /src/TripoSR/COMMIT \
 && git -C /src/TripoSR log -1 --format='%H %cI %s' > /src/TripoSR/COMMIT_INFO \
 && rm -rf /src/TripoSR/.git

# ---- stage 2: runtime
FROM ${PY_IMAGE}
ARG PY_IMAGE
ARG TORCH_VERSION
ARG TRIPOSR_SHA
ARG TORCHMCUBES_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install "torch==${TORCH_VERSION}+cu124" --index-url https://download.pytorch.org/whl/cu124
COPY requirements.txt constraints.txt /tmp/
RUN pip install -r /tmp/requirements.txt -c /tmp/constraints.txt
COPY --from=mcubes /wheels /tmp/wheels
RUN pip install --no-deps /tmp/wheels/*.whl && rm -rf /tmp/wheels
COPY --from=mcubes /src/TripoSR /opt/TripoSR
RUN PYTHONPATH=/opt/TripoSR python -c "import torch, torchmcubes, tsr.system; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'torchmcubes HAS_CUDA', torchmcubes.HAS_CUDA)" \
 && pip freeze --all > /opt/pip-freeze.txt \
 && python -c "import json, os, platform, torch; json.dump({'base_image': os.environ['PY_IMAGE'], 'python': platform.python_version(), 'torch': torch.__version__, 'torch_cuda': torch.version.cuda, 'triposr_sha': open('/opt/TripoSR/COMMIT').read().strip(), 'triposr_commit': open('/opt/TripoSR/COMMIT_INFO').read().strip(), 'torchmcubes_sha': os.environ['TORCHMCUBES_SHA']}, open('/opt/build-info.json', 'w'), indent=2)" \
 && cat /opt/build-info.json
ENV PYTHONPATH=/opt/TripoSR \
    HF_HOME=/models/hf \
    U2NET_HOME=/models/u2net \
    TSR_OUTPUT_DIR=/outputs \
    TSR_BUILD_INFO=/opt/build-info.json \
    OMP_NUM_THREADS=4
COPY app/ /app/
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)" || exit 1
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
