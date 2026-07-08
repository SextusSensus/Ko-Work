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

## Pending human decisions (see DECISIONS.md)
- P0.2a / P0.2b — **resolved**.
- P1.2 heartbeat writer — **resolved** (Start-HbRelay ~25 Hz; soft-deadman caveat).
- P1.2b demo/field profile values — populated with STARTING values; awaiting final sign-off.
- P2.1 model/wheels location — **resolved** (models/ via fetch-script; wheels → models/wheels/).
- P3.x / P3.7 — **deferred** (dataclass grouping is a large attribute rewrite; fsm-injection is a
  behavior-change) — opt in deliberately if wanted.
