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

## Pending human decisions (see DECISIONS.md)
- P0.2a / P0.2b — **resolved** (see above).
- P1.2 heartbeat writer — open, to be resolved when Phase 1 is executed.
