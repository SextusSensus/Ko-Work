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

## Phase 6G.1 + 6G.3 — substrate stood up + the pool VERIFIED live (DESKTOP RIG2, 2026-07-13)

**P6G.1 GATE PASSED** (as-built in `docs/WSL2_SUBSTRATE.md`): mirrored networking + 48GB cap, native
docker-ce 29.6.1 under systemd (Docker Desktop integration off), nvidia-container-toolkit, key-only
sshd:2222, `~/k1` on ext4 (787 MB/s). Laptop-initiated key-auth `docker run --gpus all nvidia-smi`
named the RTX 3080; forced-password leak check refused. Laptop identity file deviation:
`C:\Users\toddm\k1_desktop` (not `~/.ssh`).

**P6G.3 / CLUSTER VERIFIED** (as-built in `docs/CLUSTER_SETUP.md` section 6): every 'VERIFY ON
CLUSTER' item except cross-pyarrow content_hash is now proven on the real substrate — real gRPC loop
(coordinator on **:40077**; 50077 sits in a Windows excluded-port range under mirrored networking),
`detect_caps` reporting true hardware (cpu+cuda, 10GB, 47GB), k1recon:v1 rebuilt on the native daemon
from the committed lockfile, a real recon job end-to-end (submit → lease → container, all 5 stages ok
→ 19 artifacts pushed), and the **wipe-and-resubmit disposability gate: tolerance-identical**. Two
live-fire bugs fixed in `cluster/worker.py`: missing `RECON_IMAGE_DIGEST` injection (the worker is the
wrapper post-pivot) and root-owned bind-mount artifacts (jobs now run `--user uid:gid -e HOME=/tmp`).
Open: one ceremonial `Submit-Job.ps1` run from the laptop; cross-pyarrow content_hash.

## P7.6 + P6G.5 + P6G.4-features — the remaining desk work closed (DESKTOP RIG2, 2026-07-13)

- **P7.6 (DECISIONS.md, spec-only, BINDING):** graduation criteria for the shadow policy — per-state
  agreement bands/Wilson floors/sample floors (PARKED phantom-motion its own criterion),
  non-interference criteria, the offline failure taxonomy mapped 1:1 onto the runtime shield's graded
  rungs (Caution→Hold→Fallback-to-P→Safe-stop), structural preconditions, enable-by-reviewed-code only.
- **P6G.5 (docs/DOMAIN.md, new):** the domain artifact contract + Phases 9–12 handoff (collision =
  CoACD parts, visual = decimated mesh/splat, nav = named occupancy interface), randomization axes,
  portability + parked Cosmos track.
- **P6G.4 deferred features (desktop/recon/splat.py, all five):** adaptive densification (gsplat
  DefaultStrategy; `packed=False` API pin), joint pose refinement + per-frame appearance — both
  **gauge-anchored** (found live: unregularized deltas drifted to 4x injected noise, PSNR fell while
  SSIM rose; L2 priors pin poses near odom and appearance near identity; assertion doctrine: under a
  depth anchor refinement wins SSIM, PSNR must merely not regress), Laplacian sharpness pre-filter,
  and the real-bundle `--run` driver (bundle .rrd wall-time-paired + person-masked via geom, poses
  from trajectory.jsonl, init from mesh_visual.ply → point_cloud.ply + splat_metrics.json).
  `SPLAT-SELFTEST-OK` + driver plumbing proven on the synth bundle (quality numbers honest-low on
  noise-texture fixtures; real quality gate awaits a real garage bundle — P8.3-gated). Image deps:
  +rerun-sdk; Dockerfile ships eval/rrd_to_lerobot.py.

## Bridge build fix — `Move` (not `MoveCommand`) + probed SDK root (ROBOT-VERIFIED, 2026-09-02)

Symptom: every `--drive` launch died with `Follow process exited 3 = COMPILE FAILED`;
`k1_compile.err` showed `'class booster::robot::b1::B1LocoClient' has no member named 'MoveCommand'`
at `loco_follow_bridge.cpp:165` and `:181`. Two independent breakages, both from the robot's SDK
having moved out from under the hard-coded assumptions:

1. **Wrong API name.** The installed SDK (`b1_loco_client.hpp`, identical md5 at
   `~/Workspace/sdk_release/include/` and `/usr/local/include/`) declares
   `int32_t Move(float vx, float vy, float vyaw)`. There is no `MoveCommand` anywhere in the headers.
   Renamed both call sites (`loco_move`, `safe_shutdown`) + the protocol comments; added a note at
   `loco_move` so the name isn't "corrected" back. **No semantic change** — same fire-and-forget
   velocity call, same clamps, same watchdog tiers, same shutdown order (`Move(0,0,0)` → `kPrepare`).
2. **Wrong SDK root.** `/home/booster/Workspace/booster_robotics_sdk` no longer exists on the robot
   (the drop is now `~/Workspace/sdk_release`, and `install.sh` also puts it in `/usr/local`, whose
   `lib/` is flat with no arch subdir). Headers still resolved via the default `/usr/local/include`
   search path — which is why the failure surfaced as a *member* error and hid the second half; with
   the rename alone the link then failed `ld: cannot find .../lib/aarch64/libbooster_robotics_sdk.a`.
   `run_follow.sh`, `run_follow_demo.sh`, `run_follow_capture.sh` and `bridge_cpp/CMakeLists.txt` now
   **probe** `$BOOSTER_SDK` → `~/Workspace/booster_robotics_sdk` → `~/Workspace/sdk_release` →
   `/usr/local` for a root with both the header and the static lib (arch subdir or flat), and
   fail-closed with the reason in `k1_compile.err` + the `BRIDGE`-prefixed stderr marker (exit 3,
   the contract the app already tails) when none is found.

Verified on the robot (192.168.9.75, aarch64, g++ 11.4.0) in `/tmp` — probe resolves to
`~/Workspace/sdk_release`, `g++ -std=c++17 ... -o` exits 0, and `cmake -B build && cmake --build build`
(3.22.1) links `loco_follow_bridge` clean. The bridge binary was **not** run: `quit`/EOF ends in
`ChangeMode(kPrepare)`, which is robot motion. `VERIFY ON ROBOT`: deploy from the app, then one drive
session — expect `[run_follow] compiled OK.` and no exit 3.

Not touched (same stale-path root cause, does not block the follow): `run_loco.sh` (cds into
`~/Workspace/booster_robotics_sdk/build` for the SDK's own example client — that build dir doesn't
exist in the new drop), `tree_manifest.py`'s `SDK` root and `K1Finder.ps1`'s SDK tree-node/hint
(file-tree UI only).

## OSNet ReID staged on the robot — export, verify, TRT pre-build (ROBOT-VERIFIED, 2026-09-02)

`/home/booster/reid/` did not exist, so every `--appearance osnet` follow fell back to the colour
histogram and the node auto-refused armed re-lock (`ARM-REFUSED`, `_osnet_ok=False`). `models/` had
no OSNet blob either (README: "supplied out-of-band"), so nothing could be staged.

**Exported from official sources rather than grabbing a prebuilt ONNX.** `models/export_osnet.py`
(new) takes the official torchreid architecture (`KaiyangZhou/deep-person-reid`, self-contained,
torch-only — no pip install on the robot) and the official `osnet_x0_25` MSMT17 checkpoint
(`huggingface.co/kaiyangzhou/osnet`, the OSNet author's own repo), drops the dataset-specific 4101-id
classifier head (the follow reads the 512-d feature, never the logits), and exports with a **dynamic
batch axis** — a fixed-batch ONNX would silently collapse the node's pre-warmed `(1,2,4)` TRT set to
batch 1 (P4.5). It refuses to write the file unless the ONNX matches PyTorch at batches 1/2/4 and the
embedding is non-degenerate. Sizes + sha256s are in the script header; measured:
`max|torch-onnx|` 4.8e-05 / 5.3e-05 / 9.9e-05, `cos` 0.99999988 / 1.0 / 1.0, distinct-input cos 0.9733.

**Pre-built the TensorRT engines through the node's own `identity.ReidEngine`** (`robot/stage_reid.py`,
new — same options, same `trt_engine_cache_path` = the model dir, so the node reuses the cache rather
than building mid-follow). On the robot (Orin, sm87, ORT 1.22, GPU clocks UNPINNED at 306 MHz):

- `REID-ENGINE ok providers=TensorrtExecutionProvider,... in=256x128`, `dyn_batch=True`,
  `cpu_ep_degraded=False` → the arm gate's `_osnet_ok` and `_ep_ok` both pass.
- build+warm **355.4 s ONE TIME** (cached to `/home/booster/reid/*_fp16_sm87.engine`, 1.6 MB); this is
  exactly the stall that would otherwise land in the first field follow.
- embeddings behave: same-crop cos 1.000000, shifted 0.9213, recoloured 0.5796.
- `embed_batch(5)` 33.6 ms chunked `[4,1]` (no on-the-fly build), steady-state `embed_batch(2)`
  p50 10.6 ms / p90 21.1 ms / p99 31.4 ms — measured at the 306 MHz clock floor, so pin
  `jetson_clocks` before judging it.

Also staged a copy at `models/osnet_x0_25_msmt17.onnx` (gitignored) so `K1Finder.ps1`'s
`Ensure-ReidModel` re-stages it offline to a re-imaged robot. `models/fetch_models.sh` grew an
`osnet` branch and `models/README.md` documents the recipe + the two robot-side steps.

Note: the TRT cache is keyed to the ORT/TRT/driver versions, the GPU arch and the ONNX itself — after
an SDK/JetPack upgrade or a model change, re-run `robot/stage_reid.py` or the first follow eats the
~6-minute rebuild.

## P8.1 landed half-deployed — `calibration.py` + the `calibration` config key (FIELD-FOUND, 2026-09-02)

Two launch-blocking gaps from the P8.1 commit, both found the hard way in a field session (the P8.1
`VERIFY ON ROBOT` was never done). They surfaced one after the other, each masking the next:

1. **`ModuleNotFoundError: No module named 'calibration'`** — `follow_person_k1.py:103` imports
   `from calibration import load_calibration`, but `calibration.py` was never added to the app's
   `Deploy-FollowFiles` hard-required list, so every deploy shipped a node that cannot import. Added
   it to the list (the list's own comment is the contract: "the follow node imports every one of
   these"), so a missing local copy now fail-closes the launch instead of crashing on the robot.
2. **`CONFIG-ERROR: defaults.yaml key set mismatch -- missing=['calibration']`** — P8.1 added
   `--calibration` to argparse but not the matching key to `robot/config/defaults.yaml`, and the node
   fail-closes when the YAML key set doesn't exactly match the argparse surface (correctly — §2). Added
   `calibration: null`, which is exactly the argparse default, so no behavior changes.

Verified on the robot with the app's real drive argv (ROS sourced, `parse_args` only — no node, no
motion): `CONFIG-OK` for both `--preview` and the full `--drive --appearance osnet --arm-reacquire …`
line, `calibration=None`. Also evaluated the armed-relock ladder from the osnet-resolved floors:
`bank 0.4 >= anchor 0.35`, `reloc 0.55 > 0.35`, `view 0.55 >= 0.4`, `0 < backstop 0.28 < 0.35`,
`iso 0.55 > 0.35`, `arm_streak 8 >= streak 5`, `arm_margin 0.3 >= margin 0.2` → **LADDER-OK**.

Consequence worth stating plainly: with the OSNet engine now present and on the TRT EP, all three
arm-gate conditions (`_osnet_ok`, `_ep_ok`, `_ladder_ok`) pass for the first time, so `--arm-reacquire`
is no longer inert — the next `--drive` with that flag ARMS markerless re-lock. `follow_person_k1.py`'s
own help text says to arm only after validating audit-only. Treat the first post-OSNet session as the
audit-only validation run, not a demo.

## FIELD SESSION 2026-09-02 — first full follow: gesture lock → walk → armed re-lock → graceful stop

The follow ran end to end on the robot for the first time since the re-image. Five blockers
were found and fixed in one session, each masking the next:

1. **Bridge would not compile** — `B1LocoClient` has no `MoveCommand` (it is `Move`), and the
   hard-coded SDK root no longer exists (`booster_robotics_sdk` → `sdk_release`). See the
   "Bridge build fix" entry.
2. **Node could not import** — `calibration.py` was missing from the app's deploy list (P8.1
   landed half-deployed). See the "P8.1 landed half-deployed" entry.
3. **Node fail-closed on config** — `defaults.yaml` was missing the `calibration` key (same
   half-landed P8.1).
4. **Every drive aborted `ping 100`** — the 2026-05 firmware answers loco RPC ONLY via the ROS2
   service `/booster_rpc_service`; the raw SDK channel times out. See the "ROS-transport loco
   bridge" entry. This one cost the most time; the two red herrings are recorded there because
   they are extremely easy to fall for again:
   - `ss -ulpn` as `booster` cannot show pid info for the ROOT-owned `loco_rpc_bridge`, so it
     reads as "0 DDS sockets" when it actually holds 19. This produced a confident-but-wrong
     "boot race" diagnosis that cost a reboot and a daemon restart.
   - `ros2 topic hz/echo` never wakes the lazy camera publishers, so out-of-band probes read
     ~0 fps while a live session sees 30+. Only the node's own `DRIVE-WAIT` trace is truthful.
5. **Drive gate `depth=STALE`** — NOT a depth fault. Arrival-gap measurement during a live
   session: `depth median 0.055s p90 0.068s max 0.144s, gaps>0.5s: 0` vs
   `rgb median 0.032s p90 0.044s max 8.934s, gaps>0.5s: 1`. RGB stalls (worst 8.9s) starve the
   stereo depth downstream; the node reports the symptom as depth. Cleared by aiming at a lit,
   textured scene — `rgb_fps` went 8.7 → 35.7. The fps EMA never decays, so a frozen
   `depth_fps=7.4` alongside `depth=DOWN` means "stopped", not "slow": read the health state,
   not the fps.

**Verified in the 300s session** (`--standoff-m 0.9 --vx-max 0.1 --appearance osnet
--arm-reacquire --lock-trigger gesture --require-heartbeat`):

| signal | result |
|---|---|
| bridge (ROS transport) | `ping 0` / `prep 0` / `walk 0` |
| gesture seed | `LOCKED conf=0.92 -- gesture handoff complete` (repeatable: 2/2 sessions) |
| follow control law | closed 3.5m → **0.79m** vs 0.9m standoff, then `vx=+0.00` holding station |
| OSNet identity | `sim=0.89–0.93` frame-to-frame, `cost=0.01/2nd=0.86` under a bystander |
| armed re-lock | `AUTO-RELOCK id=44 via=anchor g=0.77 k=2 -> TRACK (anchor kept)` after 1 loss |
| **P4.4 tier retune** | **`WATCHDOG stale` == 0** for the whole run → **the P4.4 VERIFY ON ROBOT criterion PASSES** |
| graceful stop | `GRACEFUL STOP: decelerate in-gait -> settle -> kPrepare`; robot ended in mode 1 (standing) → **the 06000dc fall-fix VERIFY ON ROBOT PASSES** |
| operator deadman | `HB-LOST -> velocity gated to zero` then `HB-OK restored` (WiFi hiccup, self-recovered) |
| depth safety gate | `DEPTH-STARVED -> TURN-ONLY (forward vx suppressed)` then cleared — forward drive correctly suppressed during a hiccup |

**Open, in priority order:**
- **Loop cost.** `dt=137–237ms` against a 100ms target; `RERUN-DISABLED-SLOW` shed the recording
  in every session, so there is **no .rrd from any of them**. P4.4's margin assumed p99≈122ms;
  it held at 400ms, but a recorded run needs the loop cost back first (drop `--rerun`, or cut
  per-frame neural cost — P4.3's one-model idea is now more attractive).
- **`jetson-clocks.service` did not survive the re-image** (`Unit could not be found`). Clocks
  reset to 306MHz on every boot; pin manually until the boot unit is reinstalled.
- **Deadman auto-resumes on link return** — per runtime-safety doctrine a deadman should latch
  and require a deliberate operator clear. `UNTETHERED_FOLLOW.md` blocker 3, now observed live.
- `robot/ops/k1-loco-rpc-heal.service` was drafted against the WRONG (boot-race) diagnosis —
  it is not needed and should be deleted rather than installed.

## Obstacle brake wired to the app — the reflex existed but was OFF in every session (2026-09-03)

`--obstacle-brake` (OBSTACLE_LABELING_PLAN.md Phase 3, built 2026-07-04) was enabled in
`demo.yaml`/`field.yaml`/`capture.yaml` — but `K1Finder.ps1` passes **no `--profile` and never passed
`--obstacle-brake`**, so `defaults.yaml`'s `obstacle_brake: false` won every launch. Every drive to
date, including the first full follow on 2026-09-02, ran with **no obstacle braking at all**.

Added a `Obstacle brake` checkbox on the Tracker safety row (next to Range fence / Arm re-lock),
**default ON**, appending `--obstacle-brake`. No node changes: the reflex is unmodified.

Pre-flighted headlessly on the robot (`parse_args` only — no node, no motion):
- all 11 `obstacle_*` attributes the reflex reads are present — this is the exact failure class the
  code comments record (a missing argparse attr once raised AttributeError on every tracked frame and
  bricked the follow), so it is worth re-proving whenever the flag is turned on;
- grading verified against `_obstacle_vx_cap`: no cap ≥1.5 m, 0.062 m/s @1.2 m, 0.038 @1.0, 0.013 @0.8,
  **0.00 ≤0.7 m**;
- operator-ignore verified both ways: target 0.9 m + nearest return 0.9 m → no brake (that return IS
  the operator); obstacle 0.6 m + target 2.0 m → cap 0.00.

**STILL VERIFY ON ROBOT** — the live drive validation the plan lists as outstanding ("blocked so far
by gesture-lock flakiness = no TRACK to observe"). That blocker is gone: gesture lock seeded 2/2 on
2026-09-02. Procedure: follow at walking pace, step past a chair/box so it sits between robot and
operator, and expect throttled `CLEARANCE <m> -> vx-cap <v>` lines with vx grading to 0.00 before
contact, then recovery when the corridor clears. Do it at `--vx-max 0.1` with a hand on STOP first.

Scope note: this is **braking, not steering around**. The plan's safety spine forbids steer-around on
this hardware (one ~70° forward cone, no side sensing, no odometry — turning away loses the lock).
Class-aware braking (COCO modulates, geometry still triggers) remains Phase 3's other open TODO.

## Robot hit a chair with the brake ON — the band was blind below 0.53 m (FIELD, 2026-09-03)

First live drive with `--obstacle-brake` (standoff 0.7, vx-max 0.23). The reflex engaged and held
`vx-cap 0.00` — but only AFTER contact. The brake logic is correct; the SENSING was blind.

Evidence, from the session log:
```
CLEARANCE 1.38m -> vx-cap 0.20      <- corridor "clear", full authority
CLEARANCE 0.59m -> vx-cap 0.00      <- next sample: already inside the 0.7 m stop band
```
The obstacle did not approach through the grading zone; it MATERIALISED inside it. No graded braking
was possible, and a walking gait at 0.23 m/s cannot stop in the remaining 0.59 m.

Root cause, measured not assumed. A live per-row depth profile of the corridor (captured mid-session)
gives floor returns that fit `floor_range = cam_h / sin(theta_row)` with **cam_h = 0.86 m and a
horizontal optical axis, model vs measured within +/-0.05 m over four rows** (row 0.75: 1.98 vs 1.94;
0.80: 1.69 vs 1.74; 0.85: 1.50 vs 1.54; 0.90: 1.37 vs 1.32). With `obstacle_band_bot` at its 0.68
default the band therefore sees only ABOVE:

| range | 0.68 (was) | 0.75 (now) |
|---|---|---|
| 1.5 m | +0.37 m | +0.14 m |
| 1.0 m | **+0.53 m** | +0.38 m |
| 0.7 m | +0.63 m | +0.52 m |

A chair seat is ~0.45 m. At the old band it was invisible from ~1.2 m inward — visible far away, then
dropping BELOW the band exactly as the robot closed on it, reappearing only when the backrest filled
enough pixels at ~0.6 m. That is the 1.38 -> 0.59 jump.

Fix (app-side, visible in the echoed launch line): the Obstacle brake checkbox now emits
`--obstacle-brake --obstacle-band-bot 0.75`. At 0.75 the band sees above 0.38 m at 1 m range while the
FLOOR does not appear until 1.98 m — a 0.48 m margin before the 1.5 m trigger, so no ground
false-braking. Only ADDS obstacle sensitivity; the failure direction is a spurious stop (fail-safe).

**Explicitly rejected:** raising `--obstacle-brake-start` to 2.0 m (as `field.yaml` does). At any band
low enough to see a chair, the floor first returns at 1.79-1.98 m, which a 2.0 m trigger would put
inside the braking zone -> continuous braking on the ground. Band and trigger are coupled; tune the
band, keep the trigger at 1.5.

Still open:
- **Very low objects up close remain invisible** (coffee table ~0.40 m at 0.7 m range needs the band
  to see below 0.52 m). Inherent to a forward camera at 0.86 m with a fixed pixel band. The real fix
  is floor-plane REJECTION — extend the band far lower and discard returns consistent with the fitted
  ground plane, which the +/-0.05 m fit now makes practical. New logic in a safety reflex: audit-only
  first.
- **Speed vs stopping distance.** 0.8 m of grading zone is 3.5 s at vx 0.23 but 5.3 s at 0.15. For
  indoor clutter run `--vx-max 0.15`.
- `LOOP-MS n=600 p50=116 p90=146 p99=179 max=241 budget=100 rerun=off` — the loop is 79% over budget
  at p99 even without Rerun; 2.2x margin to the 400 ms watchdog tier (P4.4 holds).

## Compute manager Phase 1 — per-stage cost attribution (measurement only, 2026-09-03)

The loop runs `p50 116 / p90 146 / p99 179 ms` against a 100 ms budget (measured with Rerun OFF),
and the stack already carries THREE independent ad-hoc shed mechanisms — `_rr_overrun_streak`
(Rerun auto-disable), `_gesture_overrun_streak` (gesture auto-disable) and the ReID watchdog — each
with its own counter and threshold. What was missing is **attribution**: the loop reported a TOTAL
only, so there was no way to know which stage to shed first. A shed ladder built on that would be
guesswork (shedding an 8 ms Rerun to fix a 45 ms overrun is theatre).

`common.PERF` (`_StageTimer`) is a bounded rolling per-stage millisecond accountant. Five cost
centres in the control loop are instrumented: `detect` (YOLO), `pose` (gesture/lock trigger),
`reid` (OSNet `embed_batch`), `emit` (JPEG encode + stdout for `--stream`), `track` (tracker
update). `LOOP-MS` now carries `| stage p50/p90: ...` **ordered by p90 DESCENDING**, so it reads
left-to-right as "what is actually expensive" — the shed-priority question. Stats reset per 10 s
window, matching the existing LOOP-MS cadence.

**MEASUREMENT ONLY — nothing sheds, gates, or degrades on these numbers.** The diff is timing calls
plus one log line; no decision path is touched (verified: the only non-timing/non-log line in the
whole diff is the `common` import). Cost is one `perf_counter` pair + a list append per stage
(~1 us, ~0.005% of a 116 ms frame). Unit-checked on the robot: ordering and reset behave.

### The design Phase 2 will implement (NOT built yet — awaiting the measured breakdown)
A single accountant replacing the three counters, shedding in tiers, escalate-fast/restore-slow:

| tier | contents | shed order |
|---|---|---|
| **Safety — NEVER shed** | depth read, obstacle brake, control law, velocity clamps, bridge write, staleness watchdog, heartbeat | never |
| Comfort | Rerun logging, JPEG `--stream`, odometry recording | first |
| Trigger | pose/gesture model — only while LOCKED, never during SEARCH | second |
| Identity | ReID embed rate (`--reid-every-n`), detection input size | last |

The governing principle: **obstacle and wall avoidance are what the budget BUYS, never what gets
cut.** The manager may only DISABLE optional work — it can never enable anything and never touches
the safety path, the same discipline as the obstacle brake only ever REDUCING vx.

## Obstacle brake desensitised + the loop's real cost found to be CONTENTION (2026-09-03)

**Brake tuning (operator request: "less sensitive, more sentient").** In a cluttered room the reflex
held `vx-cap 0.00` continuously. It triggered on the **8th percentile** of corridor depth with only
**40 valid pixels**, so a handful of close returns (a glancing table edge, a depth speckle) could latch
a full stop. It now has to see a real object. App emits, alongside the band fix:

| knob | was | now | why |
|---|---|---|---|
| `--obstacle-pctile` | 8 | **20** | ignore the closest few % -- noise/thin edges stop dominating |
| `--obstacle-min-valid` | 40 | **150** | require a genuine footprint, not a speckle |
| `--obstacle-aged` | 3 | **5** | longer median; a transient cannot latch a stop |
| `--obstacle-brake-stop` | 0.7 | **0.6** | ~10 cm closer before forward is refused |
| `--obstacle-brake-start` | 1.5 | 1.5 | UNCHANGED -- coupled to the band (floor returns at 1.98 m) |

Verified on the robot via `parse_args` (no node, no motion): grading at `--vx-max 0.15` is full speed
>=1.5 m, 0.067 @1.0 m, 0.033 @0.8 m, **0.000 <=0.6 m**.

**This trades margin in the fail-DANGEROUS direction** (brakes later and less) -- recorded plainly
because it is the first change in this stack that does so. Pair with `--vx-max 0.15` indoors; a gait
cannot stop instantly inside 0.6 m. "More sentient" proper (mask YOLO person boxes out of the depth
corridor so bystanders do not read as furniture -- geometry triggers, class modulates) is Phase 3's
open TODO and deliberately still unbuilt.

### The compute finding that changes the manager's target
Both YOLO models were measured standalone on the robot:
```
detect  providers=['CUDAExecutionProvider','CPUExecutionProvider']  input=[1,3,640,640]  p50=19ms
pose    providers=['CUDAExecutionProvider','CPUExecutionProvider']  input=[1,3,640,640]  p50=19ms
```
**19 ms each in isolation, but 60 ms (detect) and 111 ms (pose) inside the control loop.** The models
are not slow -- they are 3-6x slower *in situ*. Neither is on TensorRT, but moving them there would
optimise the 19 ms while the 40-90 ms of CONTENTION is the actual cost: OSNet TRT, the camera pipeline
(`nv12_jpeg` ~24% CPU), `motion` ~28%, and `polkitd` at **55% CPU** driven by the robot's own
`service_status.log` loop running `sudo systemctl status` on a tight cadence -- on a box already at
load 9-12 with 6 cores.

Consequence for the compute manager: a shed ladder over the optional tier (Rerun ~0, `emit` 5 ms,
odom) recovers ~15 ms of a 118 ms loop. **The lever is contention, not features.** Reducing background
load (that 55% polkitd is a logging loop, not work) plausibly returns more than shedding everything
the follow owns. Phase 2 should target scheduling/contention, not feature shedding.

## Band fix VALIDATED + the loop came in UNDER budget (FIELD, 2026-09-03 10:17)

**Obstacle band (0.68 -> 0.75) validated on the robot.** The clearance sequence no longer cliffs:
```
band 0.75 (this session): 0.61 0.93 0.80 1.07 0.76 0.77 0.81 0.83 1.09 1.43 1.32
band 0.68 (yesterday):    1.38 -> 0.59        <- 0.79 m in ONE step, straight past the grading zone
```
Clearance now moves in smooth increments instead of materialising inside the stop band, and the
session produced exactly ONE `vx-cap 0.00` versus the continuous pinning before. Combined with the
desensitising (pctile 20 / min-valid 150 / aged 5 / stop 0.6), the reflex grades instead of slamming.
`WATCHDOG stale = 0` again, this time at `--vx-max 0.3`.

**The loop is under budget for the first time.**
```
BEFORE  p50=118 p90=189 p99=329 | detect=60/79 reid=19/31 track=14/30 emit=5/12
AFTER   p50=79  p90=119 p99=152 | detect=33/43 reid=10/20 track=10/16 emit=2/6  pose=40/65
```
detect 60->33 ms, pose 111->40 ms, reid 19->10 ms -- **every stage ~45% cheaper at once**, with NO
code, model or flag change between the two measurements. What changed was contention: GPU clocks
pinned (`jetson_clocks`, which does not survive a reboot) and the wedged camera daemon restarted.
Simultaneous across-the-board improvement is the signature of resource starvation lifting, not of any
single optimisation.

**This settles the compute-manager design.** A shed ladder over the optional tier (Rerun, `emit`
2-5 ms, odom) was worth ~15 ms; clock state plus a healthy camera daemon were worth ~40 ms. Phase 2
must manage CONTENTION and detect starvation, not shed features. Concretely it should:
1. detect and report the starvation signature (all stages inflating together) rather than blaming one;
2. assert clock state at startup (pinned or warn loudly -- the boot service the re-image deleted);
3. treat the `polkitd` 55% CPU (the robot's own `systemctl status` logging loop) as the largest single
   recoverable cost, ahead of anything the follow itself owns.

**Also observed:** `ARM-DISOWNED` x13 in one session -- with two people overlapping at iou 0.94-0.97
the gesture owner-guard refuses repeatedly (correctly; it will not risk seeding the wrong person), and
the same overlap produced a `LOST target (ambiguous)`. Single-person runs give far cleaner data.

## Head tracking stages 1-2: bridge `head` command + observed head yaw (DEFAULT OFF, 2026-09-03)

Goal (operator): head tracks the operator so the body can steer around obstacles without swinging the
person out of frame -- the prerequisite for automatic re-routing.

**The coupling that dictates the whole design.** `bearing_from_x()` derives target bearing purely from
PIXEL offset, so image-centre == body-forward is an ASSUMPTION, and nothing had ever commanded the
head, so it always held. Pan the head and three things break at once: (1) yaw control steers by the
pan angle, (2) the obstacle corridor watches head-forward instead of body-forward -- a safety
INVERSION, since it would report clear while the robot walks into something, and (3) range/target-point
inherit the same error. Therefore head yaw must be **observed, never assumed**.

Measured first: `/head_pose` reports `yaw +0.3 deg, pitch +1.0 deg` -- the head IS centred today, so
there is no latent steering offset, and the ~1 deg pitch independently corroborates the near-horizontal
optical axis the obstacle-band floor fit assumed.

**Stage 1 -- bridge (`loco_follow_bridge_ros.cpp`).** New `head <pitch_rad> <yaw_rad>` command ->
`RotateHead` api **2004**, body `{"pitch","yaw"}` (read from the SDK's own `RotateHeadParameter::ToJson`,
not guessed). Fire-and-forget like Move because it is streamed at tracking rate, clamped in
`loco_head()` as the last line of defence exactly like velocity: **yaw +/-0.60 rad (34 deg)**, chosen so
body-forward stays inside the 105.8 deg camera FOV with ~19 deg to spare; pitch +/-0.35 rad. NOT covered
by the velocity staleness watchdog -- a stale head command cannot run the robot away, and zeroing head
yaw mid-stride would be worse than leaving it. `safe_shutdown()` re-centres the head, deliberately
placed AFTER the stop+PREP sequence so the safety ordering is untouched. Compiles on the robot.

**Stage 2 -- observed head yaw (`perception.CamNode`).** Optional `/head_pose`
(`geometry_msgs/Pose`) subscription, guarded exactly like odom: absent topic or failed import ->
`head_yaw()` returns **None**, meaning UNKNOWN, and callers must fail closed. Freshness window 0.5 s
(same contract as depth): a pose older than that cannot be trusted mid-stride.

**Flags: `--head-track off|audit|on`, DEFAULT off.**
- `off` -- head never commanded, no `/head_pose` subscription at all, head yaw forced 0.0, every
  bearing/corridor expression literally the one that shipped (byte-identical; provable via
  `replay_eval compare`).
- `audit` -- compute and LOG what would be commanded and what the corrections would be; send nothing,
  apply nothing. This is how the numbers get checked before the head ever moves.
- `on` -- pan to keep the operator centred, correct bearing by the OBSERVED yaw, shift the obstacle
  corridor to keep watching body-forward. Fails closed: head pose stale/absent -> recentre + forward
  suppressed.

Verified on the robot: `preview/default` and `drive/default` both give `head_track=off` with
`pose_topic_subscribed=(none)`, and the node did not fail-closed -> `defaults.yaml` key set still
matches argparse.

**NOT YET DONE / NOT SAFE TO ENABLE:** the head command is built but **UNTESTED on hardware** (the
motion test was deliberately deferred), and the stage-3 tracking logic (bearing correction, corridor
shift, head command loop) is not written. `--head-track on` must not be used until api 2004 is proven
to actually move the head.

### Corrected premise worth recording
`OBSTACLE_LABELING_PLAN.md` forbids steering around because it "swings the operator out of the ~70 deg
FOV". The camera is **105.8 deg** (measured from `camera_info`), and head tracking removes the rest of
the objection. The reactive gap-following that becomes possible is still NOT route planning -- with one
forward cone and no SLAM it can steer around a chair, not route around a wall into another room.

## Stricter armed re-lock + clocks boot unit + a CORRECTED contention claim (2026-09-03)

**1. Armed re-lock gated harder (operator: "must not find another person on re-lock").** Armed
re-lock has DRIVE authority -- a wrong re-lock walks the robot at a stranger -- so the app now emits,
alongside `--arm-reacquire`:

| knob | was | now | why |
|---|---|---|---|
| `--reloc-arm-margin` | 0.30 | **0.35** | winner must beat the RUNNER-UP by this; the real anti-wrong-person guard |
| `--reloc-arm-streak` | 8 | **12** | consecutive confirming frames before re-lock is permitted |
| `--reloc-floor` | 0.55 (osnet-resolved) | **0.68** | raises the absolute bar; field relocks were seen at g=0.63/0.72/0.77 |

Verified on the robot (`parse_args`, no motion) that the floor ladder still holds -- **LADDER-OK**, so
armed re-lock stays ARMED rather than silently dropping to audit-only (which would LOOK like it
worked). Effect on realistic cases:
```
g=0.63 runner-up=0.10  weak field relock          -> refused: below floor
g=0.72 / 0.77          mid + strong field relocks -> re-locks
g=0.85 runner-up=0.50  decisive win (gap 0.35)    -> re-locks
g=0.90 runner-up=0.62  LOOK-ALIKE close (gap 0.28)-> REFUSED   <- the case that was asked for
```
`arm-margin` was first set to 0.45 and **corrected down to 0.35 after verification showed 0.45 also
refuses a decisive win** (g=0.85 vs 0.50). Too strict is not free: it converts every loss into manual
re-seeding, which is how a safety knob becomes an annoyance that gets switched off.

**2. `robot/ops/jetson-clocks.service` -- installed and enabled.** `jetson_clocks` does not survive a
reboot; the GPU drops to its 306 MHz floor every boot. That cost two debugging sessions -- once as a
loop-p99 regression, once as a `no camera frame within 25s` DRIVE-ABORT that looked like a dead camera
and was partly a starved one. The 2026-07-14 unit did not survive the re-image. Unit ordered
`After=nvpmodel.service` (clocks pinned BEFORE the power mode get re-scaled underneath them) with a
20 s settle -- the devfreq nodes are not reliably writable the instant multi-user is reached and
`jetson_clocks` silently no-ops if it runs too early. Verified `enabled` + `active`, clocks
`1173000000/1173000000`. Costs idle power/heat, which is why `run_follow.sh` still only WARNS and
never escalates privilege itself.

**3. CORRECTION: `polkitd` is NOT a 55% CPU runaway.** An earlier entry claimed it was "the largest
single recoverable cost", based on a single `top` snapshot taken during a busy moment. Measured
properly (mean of 5 samples over 20 s):
```
35.9% nv12_jpeg   28.7% motion   11.3% device_gateway   11.0% default   3.5% polkitd
```
polkitd averages **3.5%**. There is no runaway; the background load is legitimate services. The
largest is `nv12_jpeg` (the Auki camera NV12->JPEG converter, ~36%), which may be sheddable if nothing
consumes the video stream -- but that is a Booster/Auki service and wants understanding before being
touched. The compute-manager conclusion is unchanged (contention, not features) but the specific
target named earlier was wrong.

## Gap steering — the robot can now route AROUND an obstacle (stages 4-5, DEFAULT OFF, 2026-09-03)

Field report: "it runs into obstacles instead of finding an alternative route -- its head stays
straight and it runs into everything." Correct diagnosis of the shipped behaviour: **nothing in the
stack had steering authority over an obstacle.** The brake can only slow and stop.

**The sequencing was wrong and the field report exposed it.** Head tracking was treated as a
prerequisite for steering. It is not: the camera is **105.8 deg** wide and the obstacle reflex looks
at only the central 35% (49.7 deg), DISCARDING the rest of every frame. The free space beside an
obstacle is already visible -- it was simply never consulted. Gap steering therefore works on a FIXED
head; head tracking becomes the upgrade that allows LARGE detours without losing the operator.
```
 left sector [-53..-25 deg]   centre [-25..+25]   right sector [+25..+53]
                               ^ the only part the brake ever looked at
```

**Stage 4 -- `_sector_clearances()` + `--sector-audit`.** Per-sector (L/C/R) clearance over the full
frame, same robustness rules as the brake (percentile not min, min-valid footprint). A sector that
cannot be MEASURED returns None and every caller treats None as BLOCKED: "I cannot see" and "nothing
is there" must never be the same answer for a moving robot.

**Stage 5 -- `_gap_steer_bias()` + `--gap-steer off|audit|on`.** When the centre corridor is blocked
and a side is measurably clear, bias the yaw TARGET toward it (pre-slew, so the existing slew limiter
still bounds how fast yaw may change, and the hard vyaw clamp still applies).

INVARIANTS (deliberate):
- **Yaw only.** It never authorises forward vx. Speed stays under the brake + the `forbid_forward`
  keystone, so a steer can never authorise driving at something unseen.
- **Unmeasurable == blocked.**
- **Boxed in -> 0.0**, let the brake stop; never guess a direction.
- **Stops biasing once the operator nears the frame edge** (`--gap-steer-max-bearing-deg 35`): a
  detour that loses the lock has failed even if it misses the chair.

**How it drives.** The brake reads only the CENTRE corridor, so: centre blocked -> vx capped, robot
rotates toward the gap -> after ~1-2 s the gap has rotated INTO the centre corridor -> centre
clearance rises above brake-start -> the brake releases by itself -> forward resumes, now aimed down
the gap. It turns, then drives. It will never drive forward while the centre is blocked, because that
is driving at the obstacle.

Decision table verified on the robot (pure function of sectors + bearing, no motion):
```
centre blocked, LEFT open      -> +0.20 (LEFT)      BOXED IN               -> 0.00
centre blocked, RIGHT open     -> -0.20 (RIGHT)     side unmeasurable      -> 0.00
both open, operator LEFT/RIGHT -> toward operator   centre clear           -> 0.00
                                                    operator at -40deg     -> 0.00
```
Defaults verified `gap_steer=off`, `sector_audit=off`, and the config key set still matches argparse.

**NOT YET RUN ON THE ROBOT.** Enable `--sector-audit audit --gap-steer audit` FIRST: that logs
`SECTOR L=.. C=.. R=.. -> would-steer=..` and `GAP-AUDIT would-bias ..` while commanding nothing, so
the decisions can be read against the real room before anything steers.

## Gap steering FIELD FIX -- it oscillated and cancelled itself (2026-09-03, first live run)

First live `--gap-steer on` run. The BRAKE worked exactly as designed:
```
CLEARANCE 1.49 -> vx-cap 0.18   1.33 -> 0.15   0.99 -> 0.08   0.61 -> 0.00
TRACK ... range=1.99[depth] vx=+0.00        <- forward genuinely cut to zero
```
The STEERING did not. It flipped direction frame to frame on near-identical bearings, so the biases
cancelled and the robot wobbled straight on:
```
GAP-STEER bias -0.20 (bearing +3deg)   GAP-STEER bias +0.20 (bearing +8deg)
GAP-STEER bias +0.20 (bearing -5deg)   GAP-STEER bias -0.20 (bearing -8deg)
GAP-STEER bias +0.20 (bearing -6deg)   GAP-STEER bias -0.20 (bearing -7deg)
```
Two defects, both mine:

1. **No hysteresis.** The side was re-decided every frame and the per-sector clearances flicker, so
   the choice flipped. `_gap_dir` now COMMITS to a side and holds it while that side stays clear,
   releasing only when the centre is clear again or the robot is boxed in. A detour has to be
   committed to in order to be a detour.
2. **Trigger far too early.** It engaged whenever clearance fell below `--obstacle-brake-start`
   (1.5 m) -- the instant the brake merely began grading, with the path still essentially clear
   (`GAP-STEER` appears alongside `CLEARANCE 1.49m` above). New `--gap-steer-trigger-frac` (0.35)
   engages a fraction INTO the braking zone instead: with a 0.6..1.5 zone that is below ~0.92 m,
   i.e. when actually blocked rather than merely approaching.

This is precisely the class of defect the audit pass exists to catch, and it was found in one live
run instead of by reasoning -- worth remembering next time the temptation is to skip audit.

## Obstacle brake RE-sensitised -- min-valid 150 was hiding thin objects until close (2026-09-03)

Field: the robot still met obstacles late. The clearance trace shows why, and it is not a grading
problem:
```
CLEARANCE 1.41m -> 0.43m -> 0.35m     (1 Hz samples, travelling ~0.18 m/s)
```
0.98 m of clearance vanished between two samples while the robot covered ~0.18 m -- the obstacle
APPEARED rather than approached, the same cliff signature as the pre-band-fix chair.

Cause: `--obstacle-min-valid 150`, raised from 40 earlier the same day when the brake was
desensitised on request. A chair leg or table edge subtends very few depth pixels at 1.5 m and
plenty at 0.4 m, so demanding 150 valid pixels made thin/distant objects invisible until they were
close. That is precisely the "brakes later and less" trade recorded at the time, now observed.

Walked back toward the middle:

| knob | was | now | why |
|---|---|---|---|
| `--obstacle-min-valid` | 150 | **70** | thin/distant objects register again |
| `--obstacle-pctile` | 20 | **12** | react to nearer returns sooner, still robust vs a raw min |
| `--obstacle-brake-stop` | 0.6 | **0.7** | stop ~10 cm further out |
| `--obstacle-aged` | 5 | 5 | KEEP -- anti-glitch guard, not a sensitivity knob |
| `--obstacle-brake-start` | 1.5 | 1.5 | KEEP -- raising it puts the 1.98 m floor return inside the braking zone |

Note the shape of this: sensitivity was tuned DOWN on one report and back UP on the next. The two
requests are in genuine tension (fewer nuisance stops vs earlier detection) and the honest resolution
is not a single number but better SENSING -- floor-plane rejection would let the band see low objects
without the false-brake risk that forced the desensitisation in the first place.

## Gap steering now completes the detour -- the tracking term was cancelling it (2026-09-03)

Field: "obstacle detection works well for chairs, now I want it to automatically walk around and
continue follow mode." The previous run DID steer (18 `GAP-STEER` engagements, zero full stops, zero
losses) but never routed around, and the control law says why:
```
vyaw = -k_yaw * bearing + gap_bias      ->  equilibrium at bearing = gap/k_yaw
                                            0.20 / 0.9 = 0.22 rad = ~13 deg
```
The gap bias turns away from the obstacle; the operator-tracking term pulls straight back. They
cancel ~13 deg off the operator, so the robot NUDGES and then holds -- it can never commit to a route
around, because following fights the manoeuvre the whole way.

`--gap-steer-yaw-relax` (0.5) scales the tracking gain WHILE a detour is committed, moving that
equilibrium out to a useful angle, and restores full gain the instant the corridor clears and the
bias returns to 0 -- which is also what resumes normal following without any extra state.

| relax | detour settles at |
|---|---|
| 1.00 (before) | 13 deg -- a nudge |
| **0.50 (now)** | **25 deg -- a real diagonal past a chair** |
| 0.35 | 36 deg -- past the 35 deg lock-protection guard, would clip |

The operator stays inside the 105.8 deg FOV throughout, and `--gap-steer-max-bearing-deg 35` still
refuses to steer them toward the frame edge. Note how close 0.35 lands to that guard: relax and the
guard are coupled, so lowering relax further without raising the guard just makes the steer clip.

Previous-run evidence that the earlier two fixes hold: clearance stepped smoothly
(0.89 1.07 0.90 0.88 0.87 ... 0.80 0.78 0.75) rather than cliffing, and the steer sign HELD for 16
consecutive decisions instead of alternating.

## Wider berth around obstacles -- widened by turning EARLIER, not harder (2026-09-03)

Field: "needs to take a wider turn round the object." Two ways to widen, and they are not equally
good:
- **Turn harder** (bigger yaw bias): widens the arc but raises PEAK BEARING, and peak bearing is
  exactly what costs the lock -- all three losses today were at -27, -32 and +31 deg.
- **Turn earlier** (engage further out): same lateral clearance from a GENTLER arc at a SMALLER peak
  bearing. Strictly better on the axis that was failing.

So mostly the second, with a small rate bump:

| knob | was | now | effect |
|---|---|---|---|
| `--gap-steer-trigger-frac` | 0.35 | **0.65** | engage below **1.22 m** instead of 0.98 m (+0.24 m runway) |
| `--gap-steer-rate` | 0.20 | **0.24** | slightly firmer arc |
| `--gap-steer-max-bearing-deg` | 35 | **40** | headroom for the resulting 31 deg |

Verified on the robot: engages below 1.22 m, detour settles at **31 deg** (was 25), guard at 40 deg
has headroom, and 22 deg of FOV margin remains before the operator would leave frame.

**The honest limit.** 31 deg is close to where the lock has actually been breaking today (27-32 deg).
Turning earlier buys clearance more cheaply than turning harder, but it does not remove the underlying
constraint: on a FIXED head, every degree of detour is spent from the same budget that keeps the
operator in frame. Head tracking is the structural fix -- the head absorbs the detour and the operator
stays centred -- and it remains blocked only on the ~11 deg head-motion test.

## Pending human decisions (see DECISIONS.md)
- P0.2a / P0.2b — **resolved**.
- P1.2 heartbeat writer — **resolved** (Start-HbRelay ~25 Hz; soft-deadman caveat).
- P1.2b demo/field profile values — populated with STARTING values; awaiting final sign-off.
- P2.1 model/wheels location — **resolved** (models/ via fetch-script; wheels → models/wheels/).
- P3.x / P3.7 — **deferred** (dataclass grouping is a large attribute rewrite; fsm-injection is a
  behavior-change) — opt in deliberately if wanted.
