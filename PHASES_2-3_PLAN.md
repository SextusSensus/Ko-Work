# Phases 2-3 — Restructure/build + monolith decomposition: plan

`IMPLEMENTATION_README.md` Phases 2 (`SAFE` moves+build) and 3 (`SAFE` seam extraction). Both run
under the **behavior-preserving gate** (byte-identical decision stream + config-parity + `selftest` after
**every commit**). Derived from a fanned-out reference/deploy/build + seam-dependency investigation.

## Roadmap 0→3

| Phase | Tag | Gate | Status |
|---|---|---|---|
| P0 dead-code purge | SAFE | grep + byte-identical | ✅ done (CHANGES.md) |
| P1.1 argparse→YAML | BEHAVIOR-CHANGE (source only) | config-parity 20/20 + decision-stream | ⏳ P1.1a done (config gen); loader pending — PHASE1_PLAN.md |
| P1.2 fail-closed gate + profiles | BEHAVIOR-CHANGE (by design) | argparse-level refusal tests; dev byte-identical | ⏳ planned — PHASE1_PLAN.md |
| P2.1 reorg by deploy target | SAFE (moves) | byte-identical + selftest after path fixes | ⏳ planned (below) |
| P2.2 CMake for the bridge | SAFE | cmake build; VERIFY ON ROBOT | ⏳ planned |
| P2.3 packaging + pinned deps | SAFE | pip install -e . + selftest | ⏳ planned |
| P3.0 common.py (prereq) | SAFE | diff empty | ⏳ planned |
| P3.1-7 seam extraction | SAFE | diff empty **each commit** | ⏳ planned |
| P3.x state dataclasses | SAFE | diff empty | ⏳ planned |
| P3.y NV12→BGR dedup | SAFE | diff empty | ⏳ planned |

**Ordering:** P1 → P2 → P3 (brief order). P2.1 moves `follow_person_k1.py` into `robot/`, then P3 extracts
modules *within* `robot/`. Do P1 fully first (P2.1's path churn shouldn't collide with the config work).

---

# Phase 2 — Reorganize by deploy target + build

## The load-bearing principle (P2.1)
The robot runs a **flat `/home/booster/` layout** — the app scp's every helper to `/home/booster/<basename>`
and `run_follow.sh` does `cd /home/booster`. So a **local** repo reorg changes only the scp **SOURCE**
paths in the app; the **DEST stays flat**. Get this backwards and deploy silently breaks.

Target layout (`git mv`, preserve history):
```
robot/    follow_person_k1.py, loco_follow_bridge.cpp, enable_camera.cpp, k1_rerun.py,
          stream_cam.py, stage_pose.py, tree_manifest.py, run_*.sh, check_ifaces.sh, config/
desktop/  K1Finder.ps1 + launchers (+ last_target.txt runtime state)
eval/     _follow_autonomy/eval/* (promote)
docs/     SCOPE_*.md, _follow_autonomy/*.md, _wf4/, PHASE*_PLAN.md, DECISIONS/CHANGES
models/   yolo11n-pose.onnx (+ OSNet if staged); wheels/ (or keep at root) — via git-lfs/fetch, not raw
```

## P2.1 reference-fix checklist (verified inventory)
Every reference classified as **breaks_on_local_reorg** (fix) vs robot-side DEST (keep):

**App (`K1Finder.ps1`) — repoint SOURCE, keep DEST:**
- L39: add `$ROBOT_DIR = Join-Path $REPO_ROOT 'robot'` (with `$REPO_ROOT = Split-Path $SCRIPT_DIR -Parent`
  once the .ps1 moves to `desktop/`). Do **not** change `$SCRIPT_DIR` semantics blindly.
- L86-91 `Deploy-FollowFiles` loop, L96 `k1_rerun.py`, L69-73 `Deploy-RobotFiles` loop
  (stream_cam/run_stream/run_loco): `$src = Join-Path $ROBOT_DIR $f`; DEST `/home/booster/{f}` unchanged.
- L125/153 `Ensure-GestureModel`/`Ensure-ReidModel` local source, L209 `Ensure-RerunWheels` `$wdir`,
  L1787/1826 tree_manifest scp source, L40 `last_target.txt`: repoint per where each asset lands.
- **Keep unchanged (robot-side):** every `/home/booster/...` scp DEST, `bash /home/booster/run_*.sh`,
  `pkill -f <basename>`, `/opt/...` SDK/ROS paths, the `enable_camera` binary run (`enable_camera.cpp` has
  *no* scp — moving it is a pure repo move, zero app edits).

**Scripts / node / eval:**
- `run_*.sh`: `cd /home/booster`, `SDK=/home/booster/...`, `BIN/SRC` — all robot-side, **unchanged**.
- `follow_person_k1.py`: `from k1_rerun import RerunSink` resolves as a `/home/booster/` sibling (deploy
  keeps both flat) — unchanged. `DEF_YOLO_PATH`/`--gesture-model`/`--hb-file`/`--cmd-file` are robot
  runtime paths — unchanged.
- `replay_eval.py`: `NODE_PATH_DEFAULT = "/home/booster/follow_person_k1.py"` (robot path, unchanged); it
  adds `dirname(node_path)` to `sys.path` so `k1_rerun` imports — preserved by flat deploy.

**Two gaps P2.1 must not miss:**
1. **The `_gate` test harness** (`gate.ps1`, `config_parity.ps1`, `dump_args.py`) hardcodes
   `K1Finder\follow_person_k1.py` and `K1Finder\_follow_autonomy\eval\...`. When the node moves to `robot/`
   and eval to `eval/`, **the gate that proves the reorg is byte-identical stops running** — update these
   paths as part of the same commit (they're gitignored, so not in the tracked diff, but essential).
2. **Config deploy** (also a P1.1b item): the app deploys a *hardcoded file list*; `config/*.yaml` and
   `run_follow_demo.sh` are **not** in it. Add a `mkdir -p /home/booster/config` + per-YAML scp block to
   `Deploy-FollowFiles` (`defaults.yaml` hard-required — the fail-closed loader aborts without it; profiles
   best-effort). Loader must resolve `defaults.yaml` `__file__`-relative (`os.path.join(dirname(__file__),
   'config','defaults.yaml')` = `/home/booster/config` on the robot).

**Gate:** after all path fixes, `config_parity.ps1 diff` 20/20 + `gate.ps1 diff` empty + `selftest`; app
deploys+launches (`VERIFY ON ROBOT`). Pure `git mv` + reference edits, no logic change.

## P2.2 — CMake for the bridge (`SAFE`)
The launcher **already supports** a pre-built binary: `run_follow.sh` compiles only when
`[ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]`, and always passes `--bridge "$BIN"`. So ship a binary newer than
the `.cpp` → the compile is skipped, no launcher change needed.
- **`robot/bridge_cpp/CMakeLists.txt`:** `set(CMAKE_CXX_STANDARD 17)`; find the Booster SDK
  (`/home/booster/Workspace/booster_robotics_sdk`), include `${SDK}/include`, link the static
  `${SDK}/lib/aarch64/libbooster_robotics_sdk.a` + `fastrtps fastcdr pthread`; output `loco_follow_bridge`.
  (Exact recipe: `g++ -std=c++17 loco_follow_bridge.cpp -I <sdk>/include <sdk>/lib/aarch64/libbooster_robotics_sdk.a -lfastrtps -lfastcdr -lpthread -o loco_follow_bridge`.)
- **Deploy:** stage the built binary via scp (deploy-time build), so first-drive never blocks on g++. Keep
  `run_follow.sh`'s guard as a fallback.
- **Gate:** `cmake -B build && cmake --build build` produces a working binary; `VERIFY ON ROBOT` the drive
  path uses it. Do **not** touch the bridge source (invariant §2).

## P2.3 — Packaging + pinned deps (`SAFE`)
`pyproject.toml` so `robot/` installs with `pip install -e .`. Dependency rules (verified):
- **System, NOT pip** (document, exclude): `rclpy`, `sensor_msgs` (ROS2 Humble, `/opt/ros`).
- **Runtime deps:** `numpy` **pinned `<2`** (robot 1.26.3; onnxruntime-TRT/cv2/rclpy are ABI-bound to
  numpy 1.x — critical), `opencv-python`, `ultralytics`, `onnxruntime-gpu`, `scipy`, `pyyaml`.
- **Optional extra `[rerun]`:** `rerun-sdk==0.23.1` (pinned — newest allowing numpy 1.x; 0.23.2+ need
  numpy≥2) with `numpy>=1.23,<2`. Never stage the numpy wheel to the robot.
- **Optional extra `[eval]`:** `torch`, `transformers`, `lerobot`, `imageio`, `onnx` (eval-only heavy deps,
  kept out of the robot runtime).
- **Wheels flow is REAL** (`wheels/README.md`): offline aarch64/cp310 closure for the optional Rerun
  feature, staged by `Ensure-RerunWheels`, `.whl` gitignored (~115 MB), regenerated on demand; the numpy
  wheel is deliberately deleted. Keep as the offline recipe.
- **Gate:** `pip install -e .` succeeds; `selftest` passes against the installed package.

---

# Phase 3 — Decompose the `Follower` monolith

One seam per commit, **decision-stream diff must stay empty after every extraction** (§3). Snapshot
baselines first. Pure move + import wiring — **zero logic change**; if a seam can't cut without a logic
change, stop and flag.

## P3.0 — `common.py` FIRST (prerequisite, not one of the 7 seams)
Shared helpers + constants used across 2-5 units; extracting any seam before these have a home forces a
duplicate or a back-import. Move into `common.py`:
- **Helpers:** `log`, `gdbg`, `clamp`, `emit_frame`, `to_bgr`, `depth_to_meters`, `iou_xyxy`
  (`iou_xyxy` alone is used by tracking + triggers + fsm).
- **Constants:** `HARD_VX_LIMIT`/`HARD_VYAW_LIMIT`, `DEPTH_WARMUP_S`/`FRESH_S`/`DOWN_S`, `PERSON_CLS`,
  `KP_*`, `LockHint`, and the **`S_*`/`TS_*` state constants**.
- **Stays put:** the `DEF_*` block — consumed **only** by `parse_args` (verified: no `DEF_*` reads between
  L210-4157), so it stays with `parse_args`, not `common`.

## Extraction order (safest-first — re-ordered from the brief's middle)
`common → bridge → rerun_sink → tracking → identity → perception → triggers → fsm_control`
(the brief had perception before tracking/identity; **perception is riskier** — it drags the full ROS import
surface and the `_RR` sink — so do the pure-numpy leaves first while the diff is small).

| # | Seam → module | Classes | Risk | Key deps to carry |
|---|---|---|---|---|
| 1 | `bridge.py` | `Bridge` | **low** | none — zero globals/helpers/sibling classes (stdlib only). Ideal first cut to prove mechanics. |
| 2 | `rerun_sink.py` | `_RerunSink` alias, `_NullRR`, `_RR`, `init_rerun` | high | the `_RR` singleton (see hazard); `log` |
| 3 | `tracking.py` | `Track`, `MultiTracker` + iou/box helpers | **low** | `_HAVE_LSA`/scipy optional import; `iou_xyxy` (common) |
| 4 | `identity.py` | `TargetGallery`, `ReidEngine`, `color_hist`, `striped_*` | med | `STRIPE_*`/`REID_*` consts, `clamp`; onnxruntime lazy import |
| 5 | `perception.py` | `PersonDetector`, `CamNode`, `to_bgr`, pinhole helpers | high | ROS imports (Node/QoS/Image); `_RR`; `PERSON_CLS`/`DEPTH_*` |
| 6 | `triggers.py` | `LockTrigger`,`ArucoTrigger`,`GestureTrigger`,`CompositeTrigger` | high | `ARUCO_*`/`KP_*`/`S_*`, `marker_center`/`detect_markers`, ultralytics lazy |
| 7 | `fsm.py`+`control.py` | `Follower`(+`Seed`,`CommandChannel`) | high | **everything** — the sink node; extract last, leaves a thin orchestrator in `follow_person_k1.py` |

## Two byte-identity HAZARDS (must not miss)
1. **`GestureTrigger` reads `S_SEARCH`/`S_REACQUIRE`/`S_PARKED`** (line ~560) that are defined ~1,200 lines
   later (L1788-1792). In one file this is a call-time global lookup; the moment `triggers.py` moves, those
   names are unresolved **unless `S_*` already lives in `common.py`** (both triggers and fsm import it).
   Putting `S_*` in `fsm.py` would break triggers → this is why P3.0 owns `S_*`.
2. **The `_RR` rerun singleton** is a module global touched by the sink glue, `CamNode` (perception), and
   `Follower` (~40 sites). After extraction every consumer must import the **same** `_RR` instance, and
   `init_rerun()` rebinds it via `global _RR` — so expose a rebind/holder the importers see, or `--rerun`
   logging silently splits across sinks (a subtle diff-breaker; the no-person gate won't catch it since it
   runs `--rerun` off — **`VERIFY` with a `--rerun` run** for the perception/fsm commits).

## P3.x — group `self._` state into dataclasses (`SAFE`)
77 `Follower.__init__` fields → dataclasses, field-for-field, no semantic change:

| Group | Count | What |
|---|---|---|
| `SafetyState` | 32 | clamps/slew, geofence latches, heartbeat, loop/depth/obstacle health, command-hold |
| `RelockState` | 15 | `reloc_*`, relock-armed, reacquire vote streaks |
| `TrackState` | 17 | seed, lost_count, marker_streak, track binding, centroid/EMA, coast, feat_fn/sim_fn |
| `VizState` | 5 | `_viz_*` annotated-stream + `_rr_overrun_streak` |
| `Other` | 8 | `a` (args), bridge/node/det handles, timers, cleanup lock |

Do this **after** the seam extraction (or interleaved carefully) — it touches `Follower` broadly.

## P3.y — DRY the one safe duplication (`SAFE`)
Extract the NV12→BGR conversion (`to_bgr`, duplicated `follow_person`/`stream_cam`, per its own "verbatim
from…" comment) into `camera.py` (folds into the perception seam). **Leave the Python↔C++ clamp
duplication alone** (§2 invariant — intentional defense-in-depth).

## Payoff test (after P3.7)
Add a small unit test that drives `fsm.py` headless (no YOLO) through state transitions — the whole point of
the extraction (a testable FSM core). Complements the byte-identical gate.

---

## Cross-cutting notes
- **Gate continuity:** the config-parity + decision-stream gates run through all of P2/P3 (all `SAFE`/
  byte-identical). P2.1 must update the `_gate` harness paths in the same commit or the gate goes dark.
- **`VERIFY ON ROBOT` residue:** bridge build/deploy (P2.2), the `--rerun` singleton (P3.2/5/7), and the
  P1.2 HB-relay stand remain robot-gated — the local no-person gate can't cover them.
- **Human-review items** (DECISIONS.md): the demo/field profile values (P1.2); whether models/wheels go
  under `models/` vs stay at root (P2.1); git-lfs vs fetch-script for model blobs (P2.1/2.3).
