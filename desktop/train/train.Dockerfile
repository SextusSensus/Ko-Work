# CANDIDATE -- pins verified at the FIRST build; then `pip freeze` -> requirements-train.lock (commit).
# k1train: the P7.1 ingest + P7.2 ACT-train stack. TWO variants via a build-arg (cu126 wheels don't
# target Blackwell sm_120; cu128 does -- both VERIFIED: 3080 cu126, 5060 cu128):
#   docker build -f desktop/train/train.Dockerfile --build-arg TORCH_INDEX=cu126 -t k1train:cu126 .  # Ampere / 3080
#   docker build -f desktop/train/train.Dockerfile --build-arg TORCH_INDEX=cu128 -t k1train:cu128 .  # Blackwell / 5060
# Build context = REPO ROOT (so eval/ + robot/k1_rerun.py copy in). Run with `--gpus all`
# (nvidia-container-toolkit); torch wheels bundle CUDA, the host driver provides the rest.
#   docker run --rm --gpus all k1train:cu128 selftest            # -> TRAIN-ACT-SELFTEST-OK
#   docker run --rm --gpus all -v <ds>:/ds -v <out>:/out k1train:cu128 train --dataset /ds --out /out
# This image ALSO serves the `ingest` job type (it carries rerun + pyarrow):
#   docker run --rm -v <runs>:/runs -v <out>:/out k1train:cu128 python /app/eval/batch_ingest.py mint ...
#
# NO lerobot (torch pin conflict). NO COLMAP. NO torch.compile at runtime (Blackwell).
FROM python:3.12-slim
ARG TORCH_INDEX=cu126

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libgomp1 libglib2.0-0 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# torch/torchvision FIRST, from the CUDA index (separate layer so a deps change doesn't re-pull torch).
RUN pip install --no-cache-dir torch torchvision \
        --index-url https://download.pytorch.org/whl/${TORCH_INDEX}
COPY desktop/train/requirements-train.in /app/requirements-train.in
RUN pip install --no-cache-dir -r /app/requirements-train.in

# The P7.1/P7.2 code + the modules it imports (train_act -> checkpoint_contract/fsm_groups; the selftest
# -> synth_fixtures -> robot/k1_rerun.py + label_run/replay_eval, all under eval/ except k1_rerun).
COPY eval/ /app/eval/
COPY robot/k1_rerun.py /app/robot/k1_rerun.py

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/eval:/app/robot
ENTRYPOINT ["python", "/app/eval/train_act.py"]
