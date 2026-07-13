# recon image v2 (P6G.4 splat) -- depth-supervised 3DGS (gsplat). CUDA DEVEL base because gsplat compiles
# a CUDA extension at install (needs nvcc); torch cu126 = Ampere sm_86 (the 3080). NO COLMAP (doctrine):
# gaussians initialize from P6G.2's TSDF cloud + odom/align poses, refined during training.
#   docker build -f desktop/recon/splat.Dockerfile -t k1splat:v2 .           # build context = repo root
#   docker run --rm --gpus all k1splat:v2 --selftest                          # -> SPLAT-SELFTEST-OK
# GPU-stochastic: gate on held-out PSNR/SSIM with recorded seed + image digest, NOT byte-identical.
FROM nvidia/cuda:12.6.3-devel-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev git build-essential ninja-build \
        libgl1 libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# venv sidesteps PEP-668 (externally-managed) on 24.04's system python3.12
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --upgrade pip wheel

# torch/torchvision FIRST (separate layer; Ampere cu126), then gsplat from source targeting sm_86.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu126
ENV TORCH_CUDA_ARCH_LIST="8.6"
RUN pip install --no-cache-dir ninja && pip install --no-cache-dir gsplat

WORKDIR /app
COPY desktop/recon/requirements-splat.in /app/requirements-splat.in
RUN pip install --no-cache-dir -r /app/requirements-splat.in

COPY desktop/recon/splat.py /app/recon/splat.py
COPY desktop/recon/geom.py /app/recon/geom.py
COPY eval/rrd_to_lerobot.py /app/eval/rrd_to_lerobot.py
ENTRYPOINT ["python", "/app/recon/splat.py"]
