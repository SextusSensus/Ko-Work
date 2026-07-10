# HANDOFF — desktop (GPU box) session start here

> **⚠ ARCHITECTURE PIVOT (2026-07-10) — read `docs/CLUSTER_PLAN.md` FIRST.** The "single central
> desktop" framing throughout this file is **superseded**: compute is now a **LAN job-pool** (multiple
> boxes each run a Docker worker that pulls independent jobs from one scheduler, matched by hardware
> tag). This desktop is the **first CUDA worker** in that pool, not the sole compute box; the laptop
> hosts the coordinator/scheduler + stays source of truth. `CLUSTER_PLAN.md` is authoritative on the
> substrate; where this file says "the desktop does X" read "a capable worker does X." The
> dataset/contract/fixture scaffolds below are unchanged (they are *jobs* the pool runs). See
> DECISIONS.md P6G.0. **Substrate setup + the current step-0 self-test list live in
> `docs/CLUSTER_SETUP.md`** (use that, not the single-desktop step 0 below). **Also: the "no Python on
> the laptop" premise was wrong** -- anaconda python 3.13.9 (+ numpy/rerun/pyarrow) is present, and the
> `cluster/` + `eval/` scaffolds already PASS their self-tests locally, so they are verified logic, not
> "presumed broken." Only Docker/GPU/gRPC-network/cross-pyarrow-hash remain VERIFY ON CLUSTER.

**Audience:** a fresh Claude Code session on the DESKTOP (Ryzen 9 5900X / 64 GB / RTX 3080, Windows 11)
— the pool's first CUDA worker (and a fine place to develop the recon/train images).
This doc is the entry point; it exists so you don't need the laptop session's transcript. Read in order:

1. `PHASE_6-8_PLAN.md` (repo root on the user's Desktop; ask the user for it if not in-repo) — the
   governing plan, **Rev 2026-07-08** (has Phase 6G + P6.4; §2 invariants are non-negotiable).
2. `CHANGES.md` — what's done, per phase, with honest VERIFY-tags.
3. `docs/COMPUTE_PLACEMENT.md` — which machine owns which task + the P7.1 dataset design.
4. `DECISIONS.md` — resolved + open human decisions. Append here; never fork a second decisions file.
5. `docs/SCAFFOLD_CHARTER.md` -- which Phase 6G-8 scaffolds were blind-authored on the laptop vs
   DEFERRED to this box, plus the binding contracts each one carries. On any conflict with prose in
   this file, the charter's contracts win.

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

0. **[step 0] Run the shipped self-tests** -- the moment a Python env exists here (numpy +
   rerun-sdk >= 0.33; see the WORKSTATION_SETUP.md findings above) and BEFORE any real data work:

   ```
   python eval/batch_ingest.py selftest
   python eval/checkpoint_contract.py selftest
   powershell -File desktop/Recon.ps1 -SelfTest
   python eval/synth_fixtures.py --out %TEMP%\fixtures --seed 0
   ```

   All four were blind-authored on the laptop (which has NO Python) -- none of this code has ever
   executed. **Blind-authored code is presumed broken until its self-test passes on this box.**
   Fix-forward and record as-built deviations; do NOT start P7.1/P8.2 data work on unproven
   scaffolds. None of these needs WSL2, Docker, the GPU, or robot data -- they run on synthetic
   fixtures by design. The full scaffold -> self-test map is the table below.
1. **[P6G.1] WSL2 + Docker GPU substrate** -- runbook: **`docs/WSL2_SUBSTRATE.md`** (decisions-first,
   self-gating; edit it in place as commands actually run). Ubuntu under WSL2 (systemd on), Docker
   Engine **inside the distro** (not Docker Desktop) + nvidia-container-toolkit. NVIDIA driver on
   **Windows only** -- never a Linux GPU driver inside WSL. Cap WSL2 RAM in `.wslconfig`. `sshd` in
   the distro (port 2222, key auth) + mirrored networking (or netsh portproxy) so the laptop reaches
   it. **Run data lives on ext4** (`~/k1/{inbox,outbox,runs,bin}`), never `/mnt/c/...` (9P murders
   frame I/O).
   *Gate:* laptop-initiated SSH -> `docker run --rm --gpus all <cuda-base> nvidia-smi` shows the
   3080; a test bundle reads at native speed from ext4.
2. **[P6G.2] Build the recon image** -- from the CANDIDATE `desktop/recon.Dockerfile` against the
   frozen `docs/RECON_CONTRACT.md` (stage CLI `recon --run <id> --stages odom,tsdf,align[,splat]`;
   `recon_manifest.json` schema; image digest = `recon_version` in every artifact). v1 is CPU-lean
   (Open3D CPU wheel + OpenCV + rerun-sdk 0.33.x + CoACD + AprilTag; no torch/CUDA -- splat arrives
   as a v2 image bump at P6G.4). **No COLMAP** -- poses come from RGBD odometry + loop closure;
   splats initialize from the TSDF cloud. Sim-export stage (watertight collision mesh) is in the
   stage enum, stubbed. Mint + commit the real lockfile from the first successful build here (a
   blind lockfile is fiction -- charter S3).
   *Gate:* `docker run --rm k1recon:dev recon --selftest` prints `RECON-IMAGE-SELFTEST-OK`
   (synthetic-TSDF smoke stage; geometry stages exit `not_implemented` LOUDLY until implemented on
   this box).
3. **[P6G.3] Job round-trip** -- `desktop/Recon.ps1 -Submit|-Status|-Fetch <run_id>` driven from the
   LAPTOP (PowerShell over Windows-OpenSSH scp -- NOT the plan's `recon.py`/rsync; the laptop has
   neither Python nor rsync, see DECISIONS.md P6G.3a) -> `recon_job.sh` in the distro validates the
   inbox bundle against its `manifest.json` and `docker run`s the container (container name = the
   lock, one job) -> artifacts back to laptop `runs/<id>/recon/` + `recon_manifest.json`. No
   daemons/queues/cloud. Disable DESKTOP Windows sleep before submitting
   (`powercfg /change standby-timeout-ac 0`); laptop sleep is harmless (detached container).
   *Gate (charter S2 Tier 2):* first the STUB round-trip (`recon_job.sh --stub` -- busybox copies
   inbox -> outbox + fake per-stage status + `recon_manifest.json`), then the real image; then wipe
   the desktop copies, resubmit, tolerance-identical -> disposable. The Tier-1 `-SelfTest` from
   step 0 is NOT this gate.
4. Then, **once capture data exists:** P7.1 batch ingest (`eval/batch_ingest.py mint` -- respects
   `fsm_states.json` + the P6.4 outcome labels; episode=run, episode-level split, deterministic hash
   -- design in COMPUTE_PLACEMENT.md section 3 + the binding S1 contract in SCAFFOLD_CHARTER.md),
   P7.2 ACT training (author `train_act.py` on THIS box against `docs/TRAIN_CONTRACT.md` -- see the
   S5 deferral note below), P8.2/P8.3 recon in the container. P6G.4 splats wait on P8.3.

## Blind-authored scaffolds -> their one-command self-tests

Everything in this table was authored blind on the no-Python laptop per `docs/SCAFFOLD_CHARTER.md`
(the adjudicated GO/DEFER record; its binding contracts win over any stale prose). None of it has
ever run -- **ALL of it is `VERIFY ON DESKTOP`.** Run the self-tests (step 0) before trusting any of
it with real data.

| Scaffold (charter item) | What it is | Self-test |
|---|---|---|
| `eval/synth_fixtures.py` (S6) | Deterministic synthetic run-bundle/episode fixtures (`offload_run.sh`-shaped, planted edge cases: unlabeled / no-rrd / unmapped-FSM / under-floor) that every other self-test consumes | `python eval/synth_fixtures.py --out %TEMP%\fixtures --seed 0`; determinism asserted by S1's double-mint |
| `eval/fsm_groups.py` (B1 helper) | THE fsm id->canonical-name map (`SEARCH_MARKER` canonical with `SEARCH` as alias; `SEARCHING=1.5` distinct; anything else = `UNMAPPED` = data bug), shared by the S1 card, the `label_run` occupancy patch, and the future P7.5 grader | exercised via `batch_ingest selftest` (no standalone CLI) |
| `eval/batch_ingest.py` (S1) | P7.1 dataset mint: RAW LeRobot-v3 layout, deterministic `content_hash`/`stats_hash`, fail-closed FSM assert, loud exclusions, per-state counts in the card | `python eval/batch_ingest.py selftest` -> `INGEST-SELFTEST-OK` |
| `eval/label_run.py` occupancy patch (B1) | Per-FSM-state occupancy from `events.jsonl` into label metrics + `index.json` -- ADVISORY for run scheduling; the card is authoritative for training (DECISIONS.md P6.4a) | via its labeler flow over S6 fixtures |
| `desktop/Recon.ps1` + `desktop/recon_job.sh` (S2) | Laptop-side recon submit/status/fetch over Windows-OpenSSH scp (B2 deviation, DECISIONS.md P6G.3a); container-name lock; manifest verify-or-fetch idiom | `powershell -File desktop/Recon.ps1 -SelfTest` -> `RECON-SELFTEST-OK` (Tier-1 mock, NOT the P6G.3 gate) |
| `desktop/recon.Dockerfile` + `docs/RECON_CONTRACT.md` (S3) | CANDIDATE CPU-lean recon image + the frozen stage enum / `recon_manifest.json` schema; lockfile + real geometry stages DEFERRED to this box | after first build: `docker run --rm k1recon:dev recon --selftest` -> `RECON-IMAGE-SELFTEST-OK` |
| `docs/WSL2_SUBSTRATE.md` (S4) | The P6G.1 runbook (decisions-first, self-gating; edit as-built) | N/A (doc) -- each stage embeds its own PROVE-IT check; terminal acceptance = the P6G.1 gate |
| `docs/TRAIN_CONTRACT.md` + `eval/checkpoint_contract.py` (S5 deferral) | Frozen checkpoint payload + `stats_hash` handshake; save/load helpers the future trainer MUST import | `python eval/checkpoint_contract.py selftest` |

**The S5 deferral, explicitly:** the ACT train skeleton (`train_act.py`) was deliberately NOT
blind-authored (charter S5: the churny CVAE/chunking/dataloader parts need an interpreter to smoke;
blind authoring converts 5-minute REPL fixes into desktop debug-of-untested-code). THIS box authors
`train_act.py` from scratch against `docs/TRAIN_CONTRACT.md`, importing
`eval/checkpoint_contract.py`'s helpers -- never re-typing the payload, which is how the #1
norm-stats-mismatch bug stays structurally impossible -- and ships its own selftest (CPU overfit on
S6 fixtures: loss-decrease + checkpoint/stats-hash round-trip + per-state val report via
`eval/fsm_groups.py`).

## Invariants you must not break (from the plan §2 — read the full list)

Shadow policy NEVER actuates (no bridge code path — absent, not flagged off). Jetson control loop is
sacred. TRT engines build on the Jetson only. Datasets versioned; episode-level splits; norm stats in
card + checkpoint. Laptop = source of truth; this desktop = disposable. YAGNI. Never claim an unrun
gate passed — tag `VERIFY ON ...` honestly.
