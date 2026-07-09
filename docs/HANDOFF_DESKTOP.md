# HANDOFF — desktop (GPU box) session start here

**Audience:** a fresh Claude Code session on the DESKTOP (Ryzen 9 5900X / 64 GB / RTX 3080, Windows 11).
This doc is the entry point; it exists so you don't need the laptop session's transcript. Read in order:

1. `PHASE_6-8_PLAN.md` (repo root on the user's Desktop; ask the user for it if not in-repo) — the
   governing plan, **Rev 2026-07-08** (has Phase 6G + P6.4; §2 invariants are non-negotiable).
2. `CHANGES.md` — what's done, per phase, with honest VERIFY-tags.
3. `docs/COMPUTE_PLACEMENT.md` — which machine owns which task + the P7.1 dataset design.
4. `DECISIONS.md` — resolved + open human decisions. Append here; never fork a second decisions file.

## Machine roles (locked with the user)

| Machine | Role |
|---|---|
| **Laptop** (RTX 5060, 16 GB, NO Python) | Repo home + robot-facing control. `runs/` landing store (`desktop/Pull-Run.ps1` pulls from the Jetson; `desktop/Runs.ps1` indexes). Source of truth. |
| **Desktop** (this box) | Stateless GPU compute: P6G substrate → P7 ingest/train + P8 reconstruction. Everything here must be regenerable from `runs/` + the pinned recon image — wiping this box loses nothing. |
| **Jetson** (robot) | Exactly three things ever: TRT engine build (P7.3), shadow node (P7.4), replay-gated geofence (P8.4b). Control loop is sacred. **No route/credentials from this desktop to the robot — by construction.** |

## State as of 2026-07-09 (branch `claude/suspicious-stonebraker-d6372b`, pushed)

- **Phase 6 complete at the updated spec** (code-side): P6.1 artifact audit + intrinsics recording;
  P6.2 offload with crash-reconcile + verified-before-prune retention; P6.3 run index; P6.4 auto-label
  (`eval/label_run.py` — Python, so it runs HERE, not on the laptop); P6.1a odometry recorder,
  P6.1b `capture.yaml`, P6.2a DEPLOY_VERSION stamp, P6.2b auto-offload. On-robot verification of the
  live paths is still pending (tagged in CHANGES.md).
- **P7/P8 prep done:** `eval/fsm_states.json` (frozen FSM enum — ingest must assert against it),
  `docs/FRAMES.md` (frame conventions + `models/calibration.json` format), `docs/RUN_ARTIFACTS.md`
  (what a run bundle carries).
- **No capture runs exist yet.** P7.1/P8.2 are data-blocked until the user does a `capture.yaml` run
  and pulls it to the laptop. Don't fake data; build the substrate first.
- `docs/WORKSTATION_SETUP.md` (Windows-native toolchain) is **superseded** by the plan's Phase 6G
  (WSL2 + Docker) but its verified findings still hold: **rerun-sdk ≥ 0.33 is a hard ingest dep**
  (`rrd_to_lerobot.py` reads the .rrd via `rerun.experimental.RrdReader` before any fallback);
  **do NOT install lerobot** (RAW-layout path needs it not; it pins torch<2.12); torch for the 3080 =
  cu126 index, never bare `pip install torch`.

## What THIS box does first (in order)

1. **[P6G.1] WSL2 + Docker GPU substrate** — Ubuntu under WSL2 (systemd on), Docker Engine **inside
   the distro** (not Docker Desktop) + nvidia-container-toolkit. NVIDIA driver on **Windows only** —
   never a Linux GPU driver inside WSL. Cap WSL2 RAM in `.wslconfig`. `sshd` in the distro + mirrored
   networking (or netsh portproxy) so the laptop reaches it. **Run data lives on ext4** (`~/k1/runs`),
   never `/mnt/c/...` (9P murders frame I/O).
   *Gate:* laptop-initiated SSH → `docker run --rm --gpus all <cuda-base> nvidia-smi` shows the 3080;
   a test bundle reads at native speed from ext4.
2. **[P6G.2] The recon image** — one versioned container carrying P8.2/P8.3 (+ later splat) as stages
   (`recon --run <id> --stages odom,tsdf,align[,splat]`); pinned CUDA base, lockfile, image digest =
   `recon_version` in every artifact. **No COLMAP** — poses come from RGBD odometry + loop closure;
   splats initialize from the TSDF cloud. Include the sim-export stage (watertight collision mesh).
3. **[P6G.3] Job round-trip** — `recon.py submit|status|fetch <run_id>`: rsync bundle → SSH-invoke
   container (lock file, one job) → artifacts back to laptop `runs/<id>/recon/` + `recon_manifest.json`.
   No daemons/queues/cloud. Disable Windows sleep before submitting.
   *Gate:* full round trip; then wipe the desktop copies, resubmit, tolerance-identical → disposable.
4. Then, **once capture data exists:** P7.1 batch ingest (respect `fsm_states.json` + the P6.4 outcome
   labels; episode=run, episode-level split, deterministic hash — design in COMPUTE_PLACEMENT.md §3),
   P7.2 ACT training, P8.2/P8.3 recon in the container. P6G.4 splats wait on P8.3.

## Invariants you must not break (from the plan §2 — read the full list)

Shadow policy NEVER actuates (no bridge code path — absent, not flagged off). Jetson control loop is
sacred. TRT engines build on the Jetson only. Datasets versioned; episode-level splits; norm stats in
card + checkpoint. Laptop = source of truth; this desktop = disposable. YAGNI. Never claim an unrun
gate passed — tag `VERIFY ON ...` honestly.
