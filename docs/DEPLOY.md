# DEPLOY.md — one runbook, top to bottom

Get the whole system deployed and running: the robot producing capture data, and the compute
(ingest → train → recon → NPU inference) consuming it. Follow this in order. Detailed per-piece runbooks
are cross-referenced; this is the orchestration + the gates.

**Honesty legend:** ✅ verified this session (self-test passed on real hardware here) · 🧪 `VERIFY ON …`
(authored + self-tested, never run on the real robot/cluster/NPU — the first run is where reality checks it).

---

## 0. The mental model

**Two tracks meet at `runs/`.** The robot *produces* capture bundles into `runs/<run_id>/` (on the
laptop); the compute *consumes* them into datasets / checkpoints / meshes / masks. **Nothing downstream
runs on real data until one capture bundle exists.**

| Machine | Role | Envs / tools |
|---|---|---|
| **Laptop** (this PC) | Data source of truth (`runs/`), robot-facing app, coordinator, 5060 CUDA worker, XDNA2 NPU worker | anaconda `base` (py3.13.9); `train` conda env (py3.12, cu128); `rerun023` (compat only) |
| **Desktop** | 3080 CUDA worker + Open3D recon (needs 64 GB) | WSL2 + Docker; a Python env |
| **Jetson** (robot) | Runs the follow/capture. **Never a pool worker.** | deployed by the app via scp |

Laptop Python paths (used below):
`PY=C:\Users\toddm\anaconda3\python.exe` · `PYT=C:\Users\toddm\anaconda3\envs\train\python.exe`

---

## 1. Prerequisite — clone + prove the build (self-tests; no robot/GPU/Docker needed)

On any Python node (the laptop already has this checkout). **Blind-authored code is presumed broken
until its self-test passes** — run them first:

```
%PY% -m cluster.jobspec selftest      && %PY% -m cluster.jobqueue selftest
%PY% -m cluster.scheduler selftest    && %PY% -m cluster.worker selftest
%PY% -m cluster.submit selftest
%PY% eval/fsm_groups.py selftest      && %PY% eval/checkpoint_contract.py selftest
%PY% eval/batch_ingest.py selftest    && %PY% npu/infer.py selftest
set KMP_DUPLICATE_LIB_OK=TRUE & %PYT% eval/train_act.py selftest      # GPU overfit on the 5060
```
✅ All of these pass on this laptop today (`*-SELFTEST-OK`). If one fails on another machine, it's an
env skew — fix before trusting that piece with real data.

---

## 2. Track A — get one capture bundle from the robot  🧪 `VERIFY ON ROBOT`

Detailed safety/session plan: `docs/HANDOFF_DESKTOP.md`, `docs/RUN_ARTIFACTS.md`.

1. **Sensors + safety first (preview, no drive):** in the K1Finder app, deploy the follow files, confirm
   RGB **and** depth publishing (~10–15 s warmup), confirm odometry (`ros2 topic hz /odometer_state`),
   confirm the deadman relay (`/tmp/k1_hb`). Do a preview follow (no motion).
2. **Loop-cost check:** launch with `--rerun` + `--drive` at `capture.yaml`'s `rerun_image_every_n=2`
   and grep `k1_follow.err` for `SLOW-LOOP` / `WATCHDOG stale` — raise N if tight (`RERUN_PLAN.md`).
3. **Capture drive (tethered):** a short **there-and-back** `capture.yaml` drive (round trip = P8
   loop-closure value). On stop, the app auto-offloads (`Offload-Run.ps1` → `offload_run.sh` bundles →
   `Pull-Run.ps1` pulls) and `label_run.py` labels it.
   - **Gate:** `runs/<run_id>/` on the laptop has `manifest.json` (+ `.rrd`, `intrinsics.json`,
     `events.jsonl`), and `desktop/Runs.ps1` shows it labeled `pass`.
4. **P8.1 calibration (one session):** a checkerboard held in front of the head cam → a real
   `models/calibration.json` (P6.1 intrinsics are `approximate`; recon needs the real one — `FRAMES.md`).

**No robot yet?** Dry-run the compute path on synthetic bundles:
`%PYT% eval/synth_fixtures.py --out runs --seed 0` (real-shaped, but not real data — don't ship results).

---

## 3. Track B, fast path — run the pipeline NATIVE on the laptop (no Docker)

Everything runs in the conda envs already set up. This is the fastest way to a real end-to-end run.

**Ingest → dataset** (the `train` env has rerun + pyarrow + ffmpeg):
```
%PYT% eval/batch_ingest.py mint --runs runs --out datasets/k1_follow_v1 --version k1_follow_v1
```
Gate: `datasets/k1_follow_v1/meta/info.json` lists N episodes = N `pass` runs; a re-mint reproduces
`content_hash`. 🧪 cross-pyarrow hash stability is `VERIFY ON CLUSTER`.

**Train → checkpoint** (the 5060 GPU):
```
set KMP_DUPLICATE_LIB_OK=TRUE
%PYT% eval/train_act.py train --dataset datasets/k1_follow_v1 --out ckpts/v1
```
Gate: `ckpts/v1/best.pt` + `best.pt.val_report.json` with per-FSM-state MSE. ✅ the training loop runs
on the 5060; 🧪 the real multi-run val numbers + the honest `min_vram_gb` are `VERIFY ON CLUSTER`.

**NPU inference → masks/depth/semantics** (CPU today; NPU after §5):
```
%PY% npu/infer.py detect  --model models/yolo11n.onnx --bundle runs/<run_id> --out runs/<run_id>
%PY% npu/infer.py depth   --model models/depth_anything_v2_s.onnx --bundle runs/<run_id> --out runs/<run_id>
%PY% npu/infer.py segment --model models/segformer_b0.onnx --bundle runs/<run_id> --out runs/<run_id>
```
Gate: `runs/<run_id>/masks|depth_mono|semseg/` + a `npu_<task>_manifest.json` recording the provider
used. 🧪 the per-model parse + NPU acceleration are `VERIFY ON NPU`.

**Reconstruction (P8):** the geometry stages are `not_implemented` in recon image v1 and are implemented
on the desktop (64 GB) — see Track B pool below; native recon on the laptop's 16 GB is not recommended.

---

## 4. Track B, scalable — the LAN pool (Docker + gRPC, both GPUs in parallel)

Do this once the native path works and you have enough runs/sweeps that parallelism pays for the setup.
Detailed: `docs/CLUSTER_SETUP.md`, `docs/WSL2_SUBSTRATE.md`.

1. **Provision each GPU worker** (`WSL2_SUBSTRATE.md`): WSL2 + Docker-Engine-in-distro +
   nvidia-container-toolkit + sshd:2222. Once on the desktop, once on the laptop. Data on ext4, never
   `/mnt/c`. **Gate:** `docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi` shows the GPU.
2. **Build the images** (context = REPO ROOT):
   ```
   docker build -f desktop/train/train.Dockerfile --build-arg TORCH_INDEX=cu128 -t k1train:cu128 .   # laptop/5060
   docker build -f desktop/train/train.Dockerfile --build-arg TORCH_INDEX=cu126 -t k1train:cu126 .   # desktop/3080
   docker build -f desktop/recon/recon.Dockerfile -t k1recon:v1 .                                     # desktop
   docker run --rm --gpus all k1train:cu128 selftest      # -> TRAIN-ACT-SELFTEST-OK   (image gate)
   docker run --rm k1recon:v1 recon --selftest            # -> RECON-IMAGE-SELFTEST-OK (image gate)
   ```
   After the first build, `pip freeze` in-container → commit `requirements-{train,recon}.lock`. 🧪
3. **Stand up the coordinator** (a Python node — the laptop works) (`CLUSTER_SETUP.md §2`):
   ```
   pip install grpcio grpcio-tools
   python -m grpc_tools.protoc -I cluster/proto --python_out=cluster/proto \
          --grpc_python_out=cluster/proto cluster/proto/cluster.proto     # generate the gitignored stubs
   python cluster/scheduler.py serve --queue ~/k1/queue                   # -> SCHEDULER serving on :50077
   ```
4. **Start a worker per box** (`CLUSTER_SETUP.md §3`):
   ```
   python cluster/worker.py --scheduler <coord-host>:50077 --laptop-host <laptop-host>
   ```
   ⚠ **`detect_caps` is POSIX-only** (`os.sysconf`) — a Windows-native worker advertises `ram_gb=0.0`
   and fail-closes off every ram-floored job. Run the worker **inside WSL2** (like the images), or the
   pool won't schedule to it. 🧪
5. **Submit jobs from the laptop** (`desktop/Submit-Job.ps1`):
   ```
   .\desktop\Submit-Job.ps1 -HostName <coord> -Type ingest -Image k1train:cu128 -Inputs <run_id> -Requires 'backend=cpu'
   .\desktop\Submit-Job.ps1 -HostName <coord> -Type train  -Image k1train:cu128 -Inputs <ds>     -Requires 'backend=cuda','min_vram_gb=8'
   .\desktop\Submit-Job.ps1 -HostName <coord> -Type recon  -Image k1recon:v1    -Inputs <run_id> -Requires 'backend=cpu'
   ```
   **Gate (P6G.3 disposability):** run the STUB round-trip first (`cluster/worker.py … --stub`), then the
   real image; then wipe a worker's scratch + the coordinator queue copy, resubmit → tolerance-identical
   output lands in the laptop `runs/`. 🧪
   **NPU jobs (`detect`/`depth`/`segment`) run NATIVELY, not via the pool's Docker path** (§5) — the
   VitisAI EP can't pass through Docker.

⚠ **Operational rule:** do NOT co-schedule recon + train (or capture-offload + train) on the laptop — its
16 GB RAM is shared with the OS and would OOM (`COMPUTE_PLACEMENT.md §4`).

---

## 5. NPU acceleration — AMD Ryzen AI Software  🧪 `VERIFY ON NPU`

`docs/NPU_UTILIZATION.md`. The XDNA2 NPU + driver are ✅ present; the runtime is a one-time install:

1. Install **AMD Ryzen AI Software 1.7.1** (the Unified Installer from ryzenai.docs.amd.com) → adds ONNX
   Runtime's **VitisAIExecutionProvider** + the NPU runtime. Confirm: `python -c "import onnxruntime;
   print(onnxruntime.get_available_providers())"` now lists `VitisAIExecutionProvider`.
2. **INT8-quantize** each model with **AMD Quark** (XINT8, MinMSE) calibrated on ~100–300 real captured
   frames from `runs/`.
3. Re-run the §3 `npu/infer.py` commands — the runner now selects `VitisAIExecutionProvider` (records it
   in the manifest). NPU jobs run native (a subprocess on the laptop worker), not in Docker.

---

## 6. Gate checklist (one glance)

| # | Step | Command | Expect | Tag |
|---|---|---|---|---|
| 1 | build proof | the §1 self-test block | all `*-SELFTEST-OK` | ✅ here |
| 2 | capture | a `capture.yaml` drive → offload | `runs/<id>/` labeled `pass` | 🧪 robot |
| 3 | ingest | `batch_ingest mint` | N episodes == N pass runs; hash reproduces | ✅ logic / 🧪 cross-pyarrow |
| 4 | train | `train_act.py train` | `best.pt` + per-state val report | ✅ runs / 🧪 real numbers |
| 5 | recon image | `docker run k1recon:v1 recon --selftest` | `RECON-IMAGE-SELFTEST-OK` | 🧪 cluster |
| 6 | train image | `docker run --gpus all k1train:cu128 selftest` | `TRAIN-ACT-SELFTEST-OK` | 🧪 cluster |
| 7 | pool round-trip | stub → real → wipe → resubmit | tolerance-identical | 🧪 cluster |
| 8 | NPU | `npu/infer.py detect` after Ryzen AI | provider=`VitisAI…` in manifest | 🧪 npu |

---

## 7. Gotchas (things that bite — all hit + solved this session)

- **`python` on PATH is the Windows Store stub** (prints "Python was not found"). Use the full anaconda
  path (`%PY%` / `%PYT%` above), never bare `python`.
- **`OMP: Error #15` (libiomp5md double-init)** when torch runs under anaconda → prefix with
  `set KMP_DUPLICATE_LIB_OK=TRUE`.
- **torch CUDA index is per-GPU:** the 5060 is Blackwell (sm_120) → **cu128**; the 3080 is Ampere →
  **cu126**. A bare `pip install torch` gives the wrong build (`+cpu` or the wrong arch). `WORKSTATION_SETUP.md`.
- **rerun pin:** the workstation/recon/train envs pin `rerun-sdk>=0.33,<0.34` — 0.33.1 is VERIFIED to read
  the robot's 0.23.1 `.rrd` (`RERUN_COMPAT.md`); do not bump past it without re-running `eval/compat/`.
- **`detect_caps` is POSIX-only** → run pool workers in WSL2, not Windows-native (§4 step 4).
- **PowerShell execution policy:** launch `.ps1` via a `.bat`/`.vbs` with `-ExecutionPolicy Bypass`, never
  a bare `.\foo.ps1` (Restricted policy blocks it and looks like a crash).

---

**Recommended order:** §1 (prove the build) → §2 (one capture) → §3 (native run — real result today) →
§4 (pool, for scale) → §5 (NPU accel). Do §3 before §4: it needs zero new infrastructure and proves the
pipeline on real data before you invest in the Docker/WSL substrate.
