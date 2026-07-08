# P7/P8 Compute Placement & P7.1 Design (verified design pass)

Output of a multi-agent design pass (4 expert lenses + 2 adversarial verifiers + synthesis),
grounded in the repo + the three confirmed machines. Reference for the P7/P8 execution phases.

**Machines:** LAPTOP (this PC — Ryzen AI 9 270 + XDNA2 NPU, RTX 5060 Laptop/Blackwell ~8GB, 16GB
LPDDR5X, no Python) · DESKTOP (Ryzen 9 5900X, 64GB DDR4, RTX 3080/Ampere 10GB) · JETSON (Orin, robot).

## 1. Machine-assignment table

| Task | Machine | Why |
|---|---|---|
| P7.1 batch ingest → versioned LeRobot dataset | **Desktop** | CPU/IO (parquet+ffmpeg); 64GB; one host = stable hash. Laptop has no Python. |
| P7.2 train small ACT (vx,vyaw), val on held-out episodes | **Desktop** | Mature Ampere CUDA/PyTorch; ACT is tiny, 10GB VRAM ample. 5060 Blackwell needs unpinnable CUDA 12.8+. |
| P7.3a ONNX export (fixed obs contract) | **Desktop** | `torch.onnx.export` + ORT-vs-torch parity check. Stops at ONNX. |
| P7.3b TRT engine (FP16) + p99 w/ YOLO+ReID live | **Jetson** | **Invariant: engines built on the Orin, never copied.** Measure pinned, deploy power, all resident. |
| P7.4 shadow inference node (logs shadow_vx/vyaw/latency/hash) | **Jetson** | Consumes live obs; **no bridge handle**; fails isolated like `_reid_watchdog`; shadow-off diff EMPTY. |
| P7.5 agreement grading per FSM state (Wilson) | **Desktop** | Post-hoc numpy/scipy over JSONL; co-located with the P5.1 scorer. |
| P7.6 graduation criteria + shield spec → DECISIONS.md | **Written-only** | Do NOT build the shield or actuate. Criteria as sim-eval-measurable failure modes, not just an agreement number. |
| P8.1 real intrinsics + docs/FRAMES.md | **Desktop** | OpenCV `calibrateCamera` on checkerboard frames. **Hard prereq gate for all P8.2/8.3** (P6.1 intrinsics are `approximate`, `fy:=fx`). |
| P8.2 per-run RGBD odometry + loop closure + TSDF | **Desktop** | Most RAM-hungry task; 64GB absorbs it, 16GB can't. Open3D-class. **Don't integrate commanded velocity for pose.** |
| P8.3 cross-run alignment (VPR markerless, AprilTag backstop) | **Desktop** | Multi-run pose-graph + mesh fusion + descriptor index > 16GB. AprilTags gate/verify VPR, never dropped. |
| P8.4a map-frame metric eval → scorecard `[SAFE]` | **Desktop** | Offline CPU analysis. |
| P8.4b polygon geofence wired to robot `[BEHAVIOR-CHANGE]` | **Jetson** (authored/replay-gated on Desktop) | Only P8 item back in the loop. Replay byte-identical gate; **fail-closed on stale/bad map pose**; scalar geofence fallback intact. |
| P8.5 domain export → MuJoCo/Isaac scene + docs/DOMAIN.md | **Desktop** | Mesh decimation/convex-decomp + scene write; full mesh resident. Confirm sim target (MuJoCo lighter than Isaac). |

**Jetson does exactly three things:** build the TRT engine (P7.3b), run the shadow node (P7.4), run
the replay-gated geofence check (P8.4b). No ingest/train/reconstruction ever.

## 2. Data flow

```
JETSON (live loop)              LAPTOP (this box, Jetson-facing)        DESKTOP (workstation)
run ends → offload_run.sh:      Pull-Run.ps1 (SSH, SHA-verified)        one-hop sync (NEW leg):
  runs/<id>/ + manifest.json    → lands in runs/ (source of truth)      robocopy / rsync-over-WSL
  (sha256, git_version,         Runs.ps1 → index.json                   runs/ → canonical ingest host
   profile, duration, outcome)  NO Python here                          = dataset/checkpoint/map HOME
```

- Jetson→Laptop leg **exists** (offload_run.sh + Pull-Run.ps1). Desktop should NOT pull directly from
  the Jetson — keep the laptop as the single landing store.
- Laptop→Desktop is a **new second hop** (one-line robocopy/rsync, per session). Auto-sync = open.
- **⚠ Retention footgun:** offload moves the `.rrd` out of source + truncates the JSONL after publish,
  and `--keep N` prunes old bundles — so after offload a run lives in ONE place until synced onward.
  Sync capture runs to the desktop before `--keep` prunes them, or raise `--keep` for capture runs.
- **git_version:** carried in each manifest; the P7.1 card copies it per run. Land P6.2a's
  DEPLOY_VERSION before minting `k1_follow_v1`, else record `nogit` honestly (never fabricate a SHA).

## 3. P7.1 design (the immediate next task)

Graduate `eval/rrd_to_lerobot.py` (single-episode, `episode_index` hardcoded 0, stats over one episode)
into a batched, versioned, deterministic dataset from `runs/`.

**Feature contract (freeze it):**
- `observation.state` = `[range_m, bearing_deg, rsrc_is_depth, anchor_sim, conf, depth_fps,
  fsm_state_id]` float32 `[7]` — keep as-is (the honest proxy `control.py` consumes).
- `action` = `[vx, vyaw]` `[2]` — **do NOT re-add vy** (identically 0; a dead channel inflates
  action-norm stats). Document "vy ≡ 0, omitted" in the card.
- `observation.images.head_rgb` ← `/camera/rgb` (decimated, nearest-earlier pairing), mp4.
- **`fsm_state_id` is a BLOCKER, not a nicety:** the only map today is the ad-hoc dict in `k1_rerun.py`
  (`TRACK:3, REACQUIRE:2, SEARCHING:1.5, SEARCH_MARKER:1, SEARCH:1, PARKED:0`) — a non-integer 1.5 that
  already had a real unmapped-state bug. **Freeze a committed `fsm_states.json` (string→id) and assert
  every run maps to it before minting v1.**

**Episode = run + episode-level split:** one `runs/<run_id>/` → one episode; `episode_index` by
`sorted(run_id)`. Split at the **episode (run) level, never frame level** (adjacent ~10Hz frames are
near-duplicates → frame split leaks). Commit `splits.json`; P7.2 reads it, never re-splits.

**Norm stats:** `meta/stats.json` computed on the **TRAIN split only** (z-score continuous channels;
leave `rsrc_is_depth`/`fsm_state_id` passthrough). Must travel **inside the checkpoint** (P7.2) — the
#1 "great in training, dead on the robot" bug is a norm mismatch; enforce with a stats-hash in both.

**Determinism (re-run == same hash):** (1) sorted run iteration; (2) explicit frame ordering; (3)
float32-before-write + pinned parquet writer opts + `sort_keys` JSON; (4) **don't chase bit-identical
mp4** — pin encode params (libx264/crf/yuv420p/`-threads 1`/faststart) and exclude video bytes from the
identity hash (hash the frame→timestamp map instead); (5) write a `content_hash` over sorted parquet+
meta sha256 into `meta/info.json`.

**Versioning:** `k1_follow_v{N}` (never overwrite); card carries dataset_version, feature contract,
content_hash, fps, source run_ids with each run's git_version+profile+outcome (from `index.json`),
config snapshot, ingest-tool SHA, and a **trust block** naming what's approximate (depth loosely
aligned, intrinsics `approximate`, mono RGB only, vy omitted).

**Trust boundary — include NOW vs defer:** include the 7-dim state + `[vx,vyaw]` + mono head_rgb (none
depend on calibrated geometry — an ACT imitating `control.py` needs exactly these). **Defer:** depth
export (loose alignment; keep `--with-depth` failing loudly), any metric/intrinsic channel (until
P8.1), odometry (until P6.1a-recorded runs exist → bumps to v{N+1}). This constrains **P8 far more than
P7** — P7.1 can proceed now without waiting on P8.1.

**P7.1 gate:** N episodes == N offloaded runs; committed splits.json + fsm_states.json; card lists
version/contract/stats/source-runs; a re-run over the same runs reproduces `content_hash`.

## 4. Environment bootstrap (Desktop = the workstation)

1. Install Python + toolchain on the **desktop**: mature CUDA 11.8/12.x + PyTorch (Ampere), Open3D,
   pinned `lerobot`, `imageio-ffmpeg`/ffmpeg, OpenCV, AprilTag. **Prefer WSL2 or dual-boot Linux** —
   lerobot/Open3D/Isaac match their supported env and ffmpeg is native.
2. Stand up laptop `runs/` → desktop sync (one-line rsync-over-WSL or robocopy, post-offload).
3. Make `jetson_clocks` pinning durable (boot service) — every unpinned reboot regresses loop p99
   ~20-28%; any P7.3/P7.4 latency taken unpinned is invalid.
4. **Laptop needs no Python** (only SSH offload + one sync). The XDNA2 NPU has **no role** (INT8 ONNX,
   not training/geometry; shadow runs on the Jetson).

## 5. Open questions for the user

1. Desktop OS: Windows-only, or WSL2 / dual-boot Linux? (Determines native vs WSL for P7.1/P7.2/P8.)
2. P8.5 sim target: MuJoCo/MJX (portable, light) or Isaac (Linux + heavy 3080)? sim-eval owns this.
3. Desktop disk plan for the growing runs corpus + datasets + per-run meshes + global mesh (~500 MB/
   capture-run compounds).
4. Laptop→desktop sync: periodic (scheduled) or manual per session?
