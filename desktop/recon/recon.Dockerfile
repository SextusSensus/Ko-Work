# CANDIDATE -- pins verified at the FIRST desktop build; then `pip freeze` in-container ->
# requirements-recon.lock (commit it, per RECON_CONTRACT.md section 7). A blind lockfile is fiction.
#
# recon image v1: CPU-lean (charter S3). NO torch/gsplat/CUDA (arrive as v2 at P6G.4). NO COLMAP
# (doctrine 1). Build with the REPO ROOT as the context so eval/ is available:
#   docker build -f desktop/recon/recon.Dockerfile -t k1recon:v1 .
#   docker run --rm k1recon:v1 recon --selftest        # -> RECON-IMAGE-SELFTEST-OK
FROM python:3.12-slim

# Runtime libs for open3d + opencv (headless): libgomp (open3d OpenMP), libgl/libglib (image I/O).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY desktop/recon/requirements-recon.in /app/requirements-recon.in
RUN pip install --no-cache-dir -r /app/requirements-recon.in

# The recon code + the .rrd decoder it imports (eval/rrd_to_lerobot.read_rrd). cli.py finds eval/ at
# /app/eval via its ../eval search path.
COPY desktop/recon/cli.py /app/recon/cli.py
COPY eval/rrd_to_lerobot.py /app/eval/rrd_to_lerobot.py

ENV PYTHONUNBUFFERED=1
# recon_version comes from RECON_IMAGE_DIGEST injected at `docker run` (RECON_CONTRACT.md section 2);
# an unset digest self-reports 'unset' + a loud RECON-VERSION-UNPINNED warning.
ENTRYPOINT ["python", "/app/recon/cli.py"]
