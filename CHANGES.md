# CHANGES — IMPLEMENTATION_README.md execution

Per-phase summary of what moved/changed. One task per commit; the tree stays green
(behavior-diff gate byte-identical on non-`BEHAVIOR-CHANGE` tasks) at every commit.

## Verification environment (this machine)

The eval gate does **not** run out-of-the-box here: `follow_person_k1.py` imports
`rclpy` / `sensor_msgs` at module top-level, which aren't installed on this Windows
box (ROS2-only), and `onnxruntime` is absent. Python (anaconda 3.13.9) with
ultralytics/torch/cv2/numpy/scipy is present.

To run the behavior-preserving gate locally, an **uncommitted, gitignored** test
harness lives in `_gate/` (persistent across sessions; see `_gate/README.md`):
minimal `rclpy` + `sensor_msgs` stub packages on `PYTHONPATH` (the harness never
instantiates a real node — `StubNode` replaces it) + the eval `yolo11n.pt` (torch
backend, no onnxruntime). `_gate/gate.ps1 {selftest|baseline|diff|run}` drives
`replay_eval` on a no-person synthetic clip; `-Flags` passes extra node args so the
before/after refactor gate can snapshot and diff any flag-set.

- **Coverage:** determinism + no-op-flag + import/config regressions on the
  SEARCH/config paths. **Not** covered locally: live person TRACK/LOCK/REACQUIRE
  logic (needs a person clip; real clips live at `/home/booster/clips`). A correct
  pure move/comment edit is byte-identical on *any* clip, so this catches Phase-0/3
  mechanical regressions; person-path behavior remains `VERIFY ON ROBOT`.

## Phase 0 — Dead-code purge  `SAFE`  ✅

- **P0.1** (`e622f88`) — removed proven-dead files: `files.zip`,
  `K1Finder/follow_marker_k1.py`, root `follow_marker.py`,
  `K1Finder/yolo11n-pose.pt`. Dropped the two `tree_manifest.py` entries
  (`follow_marker_py`/`follow_marker_png`). Trimmed 6 provenance comments in
  `follow_person_k1.py` that referenced the deleted `follow_marker_k1.py` (kept the
  still-valid `stream_cam.py` referent on the NV12→BGR comment). Retired stale
  `follow_marker_k1.py` references in `README.md` + `K1Finder/README.md`.
  - Verified: app (`K1Finder.ps1`) runs `follow_person_k1.py` via `run_follow.sh`,
    never the marker node; the `.pt` is only auto-downloaded by `stage_pose.py`.
  - Gates: `git grep follow_marker_k1` empty (tracked files); decision stream
    byte-identical vs pre-change.
- **P0.2** — added `DECISIONS.md` (`917369b`), then actioned both items once
  confirmed (2026-07-07):
  - **P0.2b** (`3e0b3ab`) — `git rm` the WPF app scaffold (`K1Finder.wpf.ps1`,
    `K1 Finder (WPF).bat`, `Launch K1 Finder WPF (no console).vbs`) + `_redesign/`;
    WinForms `K1Finder.ps1` is the shipping UI. Dropped the `_redesign/` doc ref.
  - **P0.2a** (`f817be4`) — `git rm follow_marker.png` (ArUco asset retired for the
    gesture trigger); de-referenced it in both READMEs. The ArUco *code path* is
    untouched (out of scope).
- **P0.3** (`d06c3f8`) — `.gitignore` for the generated manifests
  (`k1_tree.txt`, `k1_paths.json`, `k1_paths_list.txt`) + `.claude/settings.local.json`;
  `git rm --cached` those (kept on disk). (`__pycache__/`, `*.pyc`, `*.pt` already
  ignored.)

### Notes / deviations flagged during Phase 0
- The P0.1 gate (`grep -rn follow_marker_k1` empty) required editing the two README
  files, which the brief's P0.1 *Do*-list did not mention. Done, minimally.
- `loco_follow_bridge.cpp:60` still has a dangling `follow_marker.py` doc reference
  (a deleted file). **Left untouched** per the §2 invariant (do not edit the C++
  safety floor outside P4.4). Acceptable — it's a comment, and the grep gate is for
  `follow_marker_k1`, which it does not contain.
- An untracked, already-gitignored whole-repo backup (`_opt_backup_20260623_172956/`)
  exists locally; it holds old copies with the `follow_marker_k1` string but is not
  in version control and does not affect the gate.
- The generated manifests (`k1_tree.txt` etc.) still list `follow_marker.py/png` and
  are now stale until regenerated on the robot; they are untracked, so this is moot.

## Phase 1 — Config + fail-closed profiles  ✅

Plan + test design in `PHASE1_PLAN.md`; test harness (`_gate/config_parity.ps1`,
`dump_args.py`, `config_matrix`) built pre-refactor. `BEHAVIOR-CHANGE` in the config
**source** only for P1.1 (byte-identical decisions); P1.2 is a deliberate new refusal.

- **P1.1a** (`68059e2`) — `config/defaults.yaml` generated from the pristine `parse_args`
  (types preserved, the 7 appearance floors kept `null`) + empty dev/demo/field overlays.
- **P1.1b** (`388fc2e`) — YAML loader in `parse_args`: `defaults.yaml ← --profile ← CLI`
  (CLI wins via an `argparse.SUPPRESS` parse, so a CLI value == the node default still
  beats a profile). Fail-closed on missing/mismatched/baked-floor/unknown-key/bad-choice.
  App deploys `config/` (defaults hard-required). Committed `config_selftest.py`.
- **P1.1b-fix** (`a2ea198`) — hardened per an adversarial review (6 CONFIRMED latent
  footguns): symmetric `type(str(v))` coercion so an int-written float stays float and a
  non-int float for an int key raises like argparse; bool-flag enforcement; floor-null on
  profiles too; deploy checks the scp exit code on `defaults.yaml`.
- **P1.2a** (`d45aac5`) — fail-closed drive gate + `--allow-untethered-unsafe` (default
  flip): `--drive` without `--require-heartbeat` now REFUSES (exit 2) unless the explicit
  override; refusal fires in `parse_args` before any bridge/CamNode/YOLO spawn; loud WARN
  on the override path.
- **P1.2b** (`9280701`) — populated demo/field safety profiles (both `require_heartbeat:
  true`) + `run_follow_demo.sh` (pre-compiles the bridge, forces `--profile demo`).

**Gates (every commit):** config-parity byte-identical (20/20, then 21/21 after the +1 key
in P1.2); decision-stream `COMPARE-OK`; `config_selftest` OK; `--profile dev` == no-profile;
`K1Finder.ps1` parses clean. The heartbeat-writer question was RESOLVED (armed soft deadman
~25 Hz — see DECISIONS.md P1.2).

## Phase 2 — Restructure + build  ✅

Plan in `PHASES_2-3_PLAN.md`. `SAFE` (moves + build only); byte-identity held throughout.

- **P2.1** (`fef82e7`) — reorganized by deploy target: `git mv` (history preserved) into
  `robot/` (node, bridge, scripts, config), `desktop/` (app + launchers), `eval/`
  (replay harness), `docs/` (design docs), `models/` (blobs + wheels). The robot layout is
  unchanged — scp DEST stays flat `/home/booster/`, only the app's SOURCE paths moved
  (`$ROBOT_DIR`/`$MODELS_DIR`). Fixed the `_gate` harness, doc-path comments, `.gitignore`,
  and the README map. Model blobs untracked (fetch-script, `models/fetch_models.sh`).
  Gate: config-parity 21/21 + decision-stream + config_selftest all byte-identical after
  the move.
- **P2.2** (`18de845`) — `robot/bridge_cpp/CMakeLists.txt` mirrors the verified g++ recipe
  exactly (safety floor unweakened). `run_follow.sh` unchanged — its guard already consumes
  a pre-staged binary. `VERIFY ON ROBOT` for the aarch64 build.
- **P2.3** (`8fad451`) — `robot/pyproject.toml` (`pip install -e .`) + `requirements.txt`:
  numpy<2 ABI pin, rerun/eval optional extras, rclpy/sensor_msgs as system deps. TOML
  parses; `pip install -e .` is `VERIFY ON ROBOT` (heavy GPU deps).

## Phase 3 — Decompose the monolith  ✅ (seams) / deferred (dataclasses + fsm-injection)

One seam per commit, **decision-stream diff empty after every commit**. `follow_person_k1.py`
went from ~4,830 → 3,271 lines; 921+ lines now live in focused, importable modules.

- **P3.0** (`2f55895`) + **P3.0b** (`9c95c65`) — `common.py`: shared constants (`HARD_*`,
  `DEPTH_*`, `PERSON_CLS`, `LL_DARK_THRESH`, `KP_*`, `LockHint`, and the `S_*/TS_*` FSM states)
  + pure helpers (`clamp`/`to_bgr`/`depth_to_meters`/`iou_xyxy`) + the stream/logging helpers
  (`log`/`emit_frame`/`gdbg` with `_STREAM`/`_MAGIC`/`_GDBG_LAST`). Fixed the `S_*` forward-ref
  landmine; handled the runtime-mutated `_STREAM` via `common._STREAM`.
- **P3.1** (`ef85674`) `bridge.py`; **P3.2** (`b7ff70c`) `tracking.py`; **P3.3** (`d1747f6`)
  `identity.py`; **P3.4** (`43cf446`) `rerun_sink.py` (the `_RR` singleton — provably shared,
  `node.rerun_sink._RR is rerun_sink._RR`); **P3.5** (`2809699`) `perception.py`; **P3.6**
  (`de64886`) `triggers.py`.
- **P3.y** (`7198245`) — `stream_cam` now imports `common.to_bgr` (AST-verified identical); the
  Python↔C++ clamp duplication is intentionally left alone.

Harness wiring: `replay_eval.load_follow` / `config_selftest.load` / `_gate/dump_args` add the
node dir to `sys.path` so the modular imports resolve (mirrors the robot running it as a script);
`K1Finder.ps1` deploys each new module.

**Deferred (flagged, see DECISIONS.md):** P3.x dataclass grouping and the P3.7 fsm/control
**injected-interfaces** refactor — the latter is a BEHAVIOR-CHANGE (not a pure move), so `Follower`
stays as the thin orchestrator per the "don't improve while moving" invariant.

## Phase 4 — Runtime & perf  ✅ (P4.1/P4.2/P4.4 ROBOT-VERIFIED; P4.3 evaluated; P4.5/P4.6 done)
- **P4.1** loop baselines captured: unpinned p99 ~149 ms, pinned (jetson_clocks) ~122 ms
  (`docs/LOOP_BASELINE.md`, `eval/loop_stats.py`).
- **P4.2a** warm up `PersonDetector` at construction -> first-frame spike 826->316 ms. SAFE, byte-identical.
- **P4.2b** pose model on a TRT engine (`stage_pose_engine.py`, FP16): 21.3->9.3 ms; the app prefers
  `yolo11n-pose.engine` when built (`Resolve-GestureModel`), reverts to `.onnx`. BEHAVIOR-CHANGE (FP16).
  Detection-TRT deferred (Booster `.onnx`, no `.pt`).
- **P4.3** collapse detect+pose: EVALUATED (on-Orin detect 19.1 ms vs pose-engine 9.3 ms -> collapse
  would HELP, not regress); merge deferred pending P5.1 detection-quality parity (DECISIONS.md).
- **P4.4** command-staleness tiers `800/3000 -> 400/1000` (the ONLY permitted C++ floor edit); halves the
  tier-1 runaway window (14.4->7.2 cm @ 0.18 m/s). HB tiers untouched.
- **P4.5** clamp the ReID embed batch to pre-warmed sizes {1,2,4} (no on-the-fly TRT build mid-follow);
  decision-preserving. demo cost-gating confirmed (rerun + JPEG-emit off in the headless path).
- **P4.6** persistent-fault -> STAND escalator + headless `fault_selftest.py`.
- **ROBOT-VERIFIED 2026-07-08** (`k1_follow_1783541380.rrd`, clean drive follow): P4.2a spike gone
  (max 316 ms), gesture LOCK held under the FP16 engine, **P4.4 zero `WATCHDOG stale`**; loop p50
  61-67 / p99 103-109 ms.

## Phase 5 — Eval & observability  ✅ (mechanisms built + PC-verified; real numbers need labeled clips)
- **P5.1** objective task-success scorer (`replay_eval.score_outcome` + `expect`): standoff-in-band,
  forbidden-forward (fwd vx with no depth), geofence breach, id-switches, operator retention ->
  `TASK-SUCCESS k/n` + Wilson95. `clips.json` gains the `outcome` schema. Real numbers need labeled
  person clips (`operator_id`) -- the remaining manual input.
- **P5.2** depth-injecting replay `StubNode` (`--depth-range`/`--depth-glitch`, per-clip in the manifest)
  -> the depth paths (obstacle brake, relock range-admission gate) run offline. Default OFF = byte-identical.
- **P5.3** always-on JSONL event log (`common.EventLog`): per-tick cmd_vel/fsm/loop_ms/lock forensics
  alongside the text log (`--event-log`). SAFE, byte-identical.
- **Infra** (arose on-robot, not in the numbered brief): `cam_health.sh` camera-stall detect ->
  perception-restart -> escalate; DRIVE-ABORT operator guidance in the app; `.gitattributes` forcing LF
  on robot files (a CRLF deploy had crashed `run_follow.sh`); `robot/systemd/k1-jetson-clocks.service`
  (pin persistence -- enabling is a power/thermal decision).

## Phase 6 — Run-offload substrate  (in progress)
- **P6.1** audited what a run actually records (`docs/RUN_ARTIFACTS.md`): always-on `k1_events.jsonl`
  (per-tick decision/safety signals) vs opt-in `.rrd` (RGB + depth + scalars, `--rerun` only —
  `demo`/`field` keep it off for loop-cost). Key gaps found: **no camera intrinsics** and **no
  odometry** were recorded, and pixels/depth are not always-on. Closed the intrinsics gap — the node
  now emits a derived pinhole model **once per capture run**: `rr.Pinhole` into the `.rrd` **and** an
  `intrinsics.json` sidecar beside it (`perception.pinhole_intrinsics` from `--hfov-deg`, marked
  `approximate` for P8.1 to recalibrate). Gated on the default-off Rerun sink → **byte-identical when
  recording is off**. SAFE / recording-only; on-robot capture-bundle checks are the gate
  (`VERIFY ON ROBOT`, no Python off-robot). Odometry + a `capture.yaml` profile deferred to
  `DECISIONS.md` (P6.1a/P6.1b).
- **P6.2** post-run offload hook (on-demand pair; auto-on-session-end deferred to P6.2b).
  `robot/offload_run.sh` (Jetson) packages a finished run into `runs/<run_id>/` (`.rrd` +
  `intrinsics.json`, `events.jsonl`, `k1_follow.err`, `config/`, `manifest.json` with per-file
  size+SHA-256, profile, duration) — atomic staging + **retry-safe** (sources rotated only after a
  clean publish; interrupted offload leaves sources intact) + `--keep N` retention. `desktop/Pull-Run.ps1`
  (this Windows PC, OpenSSH — no rsync/Python) pulls a bundle, fetching each file only on
  missing/hash-mismatch so an interrupted pull re-runs to convergence. Staging + fail-safe verified
  locally with a fake tree; the `python3` manifest step + the SSH pull are `VERIFY ON ROBOT`. Channel
  decided with the user: workstation-pull over scp; `runs/` lives on this PC.
- **P6.3** workstation run index. `desktop/Runs.ps1 list|show|reindex` derives `runs/index.json`
  from the pulled `manifest.json`s (run_id, created_utc, profile, duration, sizes, + an `outcome`
  label slot for the P5.1 scorer that is PRESERVED across reindex). Filesystem + JSON only.
  **PowerShell, not the plan's `runs.py`** — this workstation has no Python and the store lives here;
  `index.json` is language-neutral so a future Python `runs.py` on a GPU box reads it unchanged.
  Fully tested locally (list shows 3 bundles newest-first; outcome survives reindex; show detail).
- **P6.1a** optional planar odometry recorder (`--odom-topic`, default `''` = off = byte-identical).
  `booster_interface/msg/Odometer {x,y,theta}` via a GUARDED `CamNode` subscription (missing type/
  workspace self-disables) -> `/odom/{x,y,theta}` in the `.rrd` + `odom_x/y/theta` in the JSONL, for
  P8 trajectory prior. `run_follow*.sh` source `BoosterRos2Interface`. Recording-only; live sub
  `VERIFY ON ROBOT`.
- **P6.1b** `config/capture.yaml` (demo-grade safety + `rerun: true`, `rerun_image_every_n: 2`,
  `odom_topic`) + `run_follow_capture.sh` (`--profile capture`, pre-compiles bridge) so P7/P8 runs
  actually record pixels+depth+intrinsics+odom. App deploys both. Loop-cost of `--rerun`+`--drive`
  at N=2 is `VERIFY ON ROBOT`.
- **P6.2a** app stamps `/home/booster/DEPLOY_VERSION` (repo short SHA) on deploy so run manifests carry
  a real `git_version` (else `nogit`). Best-effort.
- **P6.2b** `desktop/Offload-Run.ps1` + `Invoke-Offload` in `Stop-Tracker`: a capture session's end
  fires the bundle+pull DETACHED, after the robot is safed (never blocks/throws into teardown). End-to-
  end `VERIFY ON ROBOT`.
- **Design pass** (multi-agent, adversarially verified): P7/P8 compute placement (desktop 3080/64GB =
  P7/P8 workstation; laptop = robot-facing landing store; Jetson = TRT build + shadow node + geofence
  only) + the P7.1 dataset design; captured for the P7 kickoff. Flagged an FSM-id freeze
  (`fsm_states.json`) and the retention footgun (sync capture runs off the laptop before `--keep`).
- **Design pass** (web-verified): Windows-native desktop toolchain kit (superseded 2026-07-09 by the
  updated plan's WSL2+Docker desktop substrate, but the pins/findings still hold as reference).

## Phase 6 (updated plan Rev 2026-07-08) additions
- **P6.2 enhancement** (updated spec): (a) `offload_run.sh --reconcile` runs at session START from
  every `run_follow*.sh` launcher, so a crashed/killed/power-cut session's leftover data still gets
  bundled on the next boot (no-op after a clean session; runs before the node writes, so nothing mixes).
  (b) Retention now prunes **only workstation-VERIFIED** bundles (a `.verified` marker `Pull-Run.ps1`
  writes back after a hash-checked pull), oldest-first beyond `--keep`; an un-offloaded bundle is
  **never** pruned (data loss impossible) and a pile-up emits `OFFLOAD-DISK-ALERT`. Both verified
  locally; live sweep + writeback `VERIFY ON ROBOT`.
- **P6.4** auto-label on ingest. `eval/label_run.py` runs the EXACT P5.1 `score_outcome` over a bundle's
  own decision log (`k1_follow.err` -- range/rsrc/vx/track_id), writing an `outcome` label (pass /
  review / incomplete) + metrics + `scorer_version` into `manifest.json`; idempotent, `--force`/`--all`,
  flags artifact-incomplete / never-locked runs for review rather than passing them. Thresholds default
  + refined from the bundle's own config. `Runs.ps1 label` invokes it (robust real-Python detection --
  Windows ships `py`/`python` alias STUBS that fool `Get-Command`) and surfaces the label into
  `index.json` (index now reads `outcome` from the manifest). Runs where Python lives (the desktop; this
  laptop has none) -- PS index-surfacing + no-Python path tested here, the Python labeler is
  `VERIFY ON WORKSTATION`. Same stub-detection fix applied to `Setup-Workstation.ps1`.

## Phase 8 (laptop-authorable groundwork)
- **P8.1** `docs/FRAMES.md`: coordinate-frame conventions (REP-103; map / anchor_i / odom / base /
  camera + the camera-in-world compose chain, with the `/tf`-is-body-only and flat-floor caveats from
  on-robot notes) and the `models/calibration.json` format that upgrades P6.1's approximate
  `intrinsics.json` to a real (checkerboard/factory) intrinsic + distortion + **depth-scale
  validated against a known distance** (kills the mm-vs-m bug). The doc + format are the laptop
  deliverable; the OpenCV checkerboard solve + depth-scale check are `VERIFY ON WORKSTATION`.

## Phase 7 (laptop-authorable groundwork)
- **P7.1 prep** `eval/fsm_states.json`: FROZEN categorical FSM state->id map for the dataset's
  `fsm_state_id` feature, mirroring `robot/k1_rerun.py::_STATE_CODE` exactly (validated). Zero-risk
  (no recording change) -- it just version-locks the encoding so the feature is stable across dataset
  versions; the P7.1 ingest will assert every recorded state maps here before minting a dataset. Fixes
  the design pass's "unfrozen ad-hoc FSM enum" blocker. k1_rerun.py gains a sync cross-ref comment.

## Phase 6G/7 prep -- charter scaffolds + the LAN-pool pivot (laptop-authored, VERIFY ON CLUSTER)

Per `docs/SCAFFOLD_CHARTER.md` (the adjudicated GO/DEFER record for blind-authoring on this no-Python
laptop) and `docs/CLUSTER_PLAN.md` (the 2026-07-10 architecture pivot: single central desktop -> a LAN
heterogeneous job-pool; DECISIONS.md P6G.0). Every artifact ships UNEXECUTED -- **`VERIFY ON CLUSTER`**:
presumed broken until its self-test passes where Python exists (`docs/HANDOFF_DESKTOP.md` step 0). The
charter-scaffolding workflow hit the account rate limit mid-run; this records **what actually landed**.

**LANDED + self-test-gated (pivot-agnostic -- a dataset/contract/fixture is a *job* the pool runs):**
- **S6** `eval/synth_fixtures.py` -- deterministic-under-`--seed` synthetic run bundles/episodes
  (`offload_run.sh`-shaped, planted edge cases: unlabeled / no-rrd / unmapped-FSM / under-floor); the
  shared fixture source. `python eval/synth_fixtures.py --out %TEMP%\fixtures --seed 0`.
- **B1** `eval/fsm_groups.py` (the ONE fsm id->canonical-name helper; VERIFIED BY READ against
  `fsm_states.json`) + the `eval/label_run.py` occupancy patch (VERIFIED BY READ). Dual-ownership rule
  in DECISIONS.md P6.4a.
- **S5 (contracts only, train DEFERRED)** `docs/TRAIN_CONTRACT.md` + `eval/checkpoint_contract.py`
  (checkpoint/`stats_hash` handshake; `python eval/checkpoint_contract.py selftest`). The desktop
  authors `train_act.py` from scratch against these.
- **S3 contract** `docs/RECON_CONTRACT.md` -- frozen stage enum + `recon_manifest.json` schema. Under
  the pivot this describes one job *type* run by a worker; bystander-masking gap in DECISIONS.md P8.2a.

**NOT reached before the rate limit (still to author):**
- **S1** `eval/batch_ingest.py` -- P7.1 dataset mint (RAW LeRobot-v3, imports `rrd_to_lerobot`'s
  decoder, deterministic `content_hash`/`stats_hash`, fail-closed FSM assert, loud exclusions). **Next
  up.** Pivot-agnostic. Target self-test `INGEST-SELFTEST-OK`.
- **S3 image** `desktop/recon/` Dockerfile + stage CLI + synthetic-TSDF smoke. Author-agent failed.
- **S4** `docs/WSL2_SUBSTRATE.md` -- LANDED, but **re-scoped by the pivot** to *per-CUDA-worker-node*
  provisioning (banner added); `docs/CLUSTER_SETUP.md` will generalize it across worker types.

**SUPERSEDED by the pivot (audit -- removed/not committed):**
- **S2** `desktop/Recon.ps1` (the direct laptop->one-desktop recon round-trip) is **not committed** --
  recon is now a pool job submitted to the scheduler (`cluster/submit.py` + `desktop/Submit-Job.ps1`);
  its manifest-verify scp idiom moves into `cluster/worker.py`. `recon_job.sh` / `Runs.ps1`-recon were
  never authored and are replaced by the worker + scheduler. (DECISIONS.md P6G.3a, superseded note.)
- The single-desktop framing in `docs/HANDOFF_DESKTOP.md` + `docs/COMPUTE_PLACEMENT.md` gained pivot
  banners; `PHASE_6-8_PLAN.md` §2/§6 + P6G.1/P6G.3 overrides recorded in DECISIONS.md P6G.0.

## Phase 6G -- LAN job-pool substrate (`cluster/`, laptop-authored + LOCALLY VERIFIED)

The pivot (`docs/CLUSTER_PLAN.md`): a heterogeneous job-pool -- Docker workers pull independent jobs
from one scheduler, matched by capability tag. **Correction to the standing "no Python on the laptop"
claim:** anaconda python 3.13.9 (+ numpy 2.3.5, rerun 0.33.1, pyarrow 21) IS present (just not `python`
on PATH). So all the stdlib/numpy/rerun scaffolds were **run green locally** -- NOT "presumed broken."

- `cluster/jobspec.py` (schema + pure fail-closed capability match), `cluster/jobqueue.py` (filesystem
  queue: atomic transitions, bounded retry, stale-lease reaping, crash-reconcile), `cluster/scheduler.py`
  (`SchedulerCore` pure + lazy-gRPC `serve()`), `cluster/worker.py` (`WorkerCore` lease->run->report; the
  self-test wires a real worker to a real scheduler in-process), `cluster/submit.py` + `desktop/Submit-Job.ps1`
  (enqueue; laptop ssh's to the coordinator). `cluster/proto/cluster.proto` = the pull-based control plane.
  ALL self-tests pass locally: JOBSPEC/QUEUE/SCHEDULER/WORKER/SUBMIT-SELFTEST-OK.
- **S1** `eval/batch_ingest.py` -- the P7.1 dataset mint (was rate-limited out earlier), now authored +
  `INGEST-SELFTEST-OK` across BOTH tiers (core determinism + real-`.rrd` bundle w/ integrity gate +
  FSM-abort blocker).
- `docs/CLUSTER_PLAN.md` + `docs/CLUSTER_SETUP.md`. Coordinator-host design corrected: the scheduler is
  Python so it runs on a Python node (desktop/WSL), NOT the laptop; laptop stays data source of truth +
  submits over ssh.
- **Two real bugs caught by running it:** `synth_fixtures` emitted `bearing=+05.5` (parser needs
  `+05.5deg`); and it hashed the `.rrd` before rerun's background writer finalized it (footer lands on
  recording-drop) -> every fixture bundle failed the integrity gate; forced a `rerun_shutdown` finalize.
- **Still `VERIFY ON CLUSTER`:** the gRPC network loop, Docker/GPU execution + `detect_caps` on real
  hardware, image builds, cross-pyarrow `content_hash` stability, the P6G.3 wipe-and-resubmit gate.

## Phase 6G.2 — recon image built + geometry stages implemented (DESKTOP, 2026-07-13)

First real work on the RTX 3080 desktop (Docker Desktop for now; native-engine substrate P6G.1 +
sshd deferred until the laptop is present for its gate). Supersedes the stale "S3 image ... Author-agent
failed" note above — the image built and all stages landed.

- **Image built + locked.** `k1recon:v1` built via Docker Desktop; `RECON-IMAGE-SELFTEST-OK`.
  `requirements-recon.lock` minted from the first build (`pip freeze`, 82 pins, open3d 0.19.0 /
  rerun-sdk 0.33.1 / opencv-contrib-headless 5.0 / coacd 1.0.11); `recon.Dockerfile` pinned to the lock
  (rebuild-from-lock reproduces the same set + green selftest).
- **All geometry stages implemented** (`desktop/recon/geom.py`, wired into `cli.py`): odom = Open3D RGBD
  odometry + pose-graph loop closure + global optimization (NO COLMAP); tsdf = masked-RGBD TSDF along the
  trajectory; simexport = quadric-decimated visual + CoACD watertight collision parts; align = AprilTag
  detect + solvePnP per-tag observations. Metric intrinsics gate fail-closed (odom/tsdf refuse approximate
  intrinsics unless `--allow-approximate-intrinsics`); artifacts frame-tagged per the contract.
- **New `.rrd` reader** `rrd_to_lerobot.read_rrd_frames` (non-breaking): per-frame WALL timestamps +
  `/camera/rgb/target` Boxes2D, feeding wall-time RGBD pairing (doctrine 4) + person-masking (doctrine 5).
- **Verified in-container:** `geom.py --selftest` (`GEOM-SELFTEST-OK`; synthetic RaycastingScene motion,
  odom traj err ~0.003 m, tsdf/ simexport/align all pass) + a full-pipeline run over a real
  `synth_fixtures` `.rrd` bundle (all stages `ok`; refusal path -> odom failed, tsdf skipped, exit 1).
  `synth_fixtures.py` also runs green on this box (`SYNTH-FIXTURES-OK`).
- **Still `VERIFY ON DESKTOP`:** metric accuracy on a real garage bundle (data-blocked); the target-box
  mask decode (synth logs no box); align cross-run merge into `map` (P8.3, two-runs-blocked). Splat
  (P6G.4) is the v2 image bump. Git: tracked locally on branch `desktop/p6g2-stages` (this box is not the
  laptop checkout — reconcile there).

## Pending human decisions (see DECISIONS.md)
- P0.2a / P0.2b — **resolved**.
- P1.2 heartbeat writer — **resolved** (Start-HbRelay ~25 Hz; soft-deadman caveat).
- P1.2b demo/field profile values — populated with STARTING values; awaiting final sign-off.
- P2.1 model/wheels location — **resolved** (models/ via fetch-script; wheels → models/wheels/).
- P3.x / P3.7 — **deferred** (dataclass grouping is a large attribute rewrite; fsm-injection is a
  behavior-change) — opt in deliberately if wanted.
