# DECISIONS — human-confirmation items

Items from `IMPLEMENTATION_README.md` that require a human decision before the
associated deletion/change is safe. Each has the evidence gathered and a
recommendation; **the call is yours.** Tick the box and I'll action it.

---

## P0.2a — Keep or remove `follow_marker.png`?

**What:** `follow_marker.png` (repo root, DICT_4X4_50 ArUco marker, printable).

**Evidence:**
- Not loaded by any code and not referenced by the app (`K1Finder.ps1` has 0
  references to `follow_marker.png`). It is a *printable* asset, not a runtime one.
- The ArUco marker is still a supported **lock trigger** for `follow_person_k1.py`.
  Per project notes, a raised-hand **gesture** became the *default* lock trigger
  (2026-07-05); ArUco is the fallback (untick "Gesture lock" in the Tracker tab).
- So the marker workflow is still reachable → the printable source still has a use.

**Recommendation was KEEP**, but the user chose REMOVE (ArUco lock trigger is being
retired in favor of the raised-hand gesture).

- [x] **Remove** it — **RESOLVED 2026-07-07:** `git rm follow_marker.png`; the file
  references in `README.md` and `K1Finder/README.md` were de-referenced. The ArUco
  code path in `follow_person_k1.py` is untouched (out of scope here); only the
  printable asset was removed.

---

## P0.2b — Is the WPF app stack abandoned? (remove it, or is it the final UI?)

**What (the WPF stack):** `K1Finder.wpf.ps1`, `K1 Finder (WPF).bat`,
`Launch K1 Finder WPF (no console).vbs`, and `K1Finder/_redesign/`
(`BASELINE.md`, `PLAN.md`, `VITALS.md`, phase screenshots).

**Evidence that it's an abandoned scaffold:**
- Its own launcher says so: `K1 Finder (WPF).bat` → *"Launch the K1 Finder (WPF)
  **scaffold** (visible console for debug output)."*
- `K1Finder.wpf.ps1` (637 lines) does **not** wire the follow pipeline — no
  `run_follow` / bridge invocation. It's a UI shell only.
- The active, documented app is `K1Finder.ps1` (PowerShell + WinForms); the
  top-level README names it the main entry point and documents its six tabs.

**Recommendation: REMOVE the WPF stack + `_redesign/`** — the WinForms
`K1Finder.ps1` is the shipping UI.

- [x] **Remove** the WPF stack + `_redesign/` (WinForms `K1Finder.ps1` is final)
  — **RESOLVED 2026-07-07:** `git rm`'d `K1Finder.wpf.ps1`, `K1 Finder (WPF).bat`,
  `Launch K1 Finder WPF (no console).vbs`, and `K1Finder/_redesign/`.

---

## P1.2 — Does a heartbeat *writer* exist? (RESOLVED — yes, ~25 Hz)

**What:** the untethered operator-deadman (`--require-heartbeat`) reads a heartbeat
file/signal; the deadman is inert unless something *writes* it at ~10 Hz.

**Status: RESOLVED (static verification 2026-07-07).** The deadman is **armed and functional
as a soft/software deadman** — NOT inert. `Start-HbRelay` (`K1Finder.ps1:1267`) runs a remote
`while read -r _; do touch /tmp/k1_hb; done`, pumped by the 40 ms `$mediaTimer` `WriteLine('h')`
at **~25 Hz** (faster than the ~10 Hz assumed). Node zeroes velocity when the mtime is stale
(`hb_stale_ms 400`); the C++ bridge independently stands the robot via `K1_REQUIRE_HB` (zero @400 ms,
kPrepare @1500 ms). All three links fail-closed; default OFF = byte-identical tethered.

- [x] Writer verified (Start-HbRelay ~25 Hz).
- ⚠️ **CAVEAT (still open):** this is a *soft* stand, **not** an independent hardware power cutoff
  (per [[k1-hardware-backstop]]), and it was **not** on-robot arm-validated. Do not treat it as
  sufficient to authorize untethered operation until robot-armed + paired with the hardware backstop
  the `UNTETHERED_FOLLOW.md` gate requires.

---

## P1.2b — demo/field profile safety values (human review)

**What:** the `demo.yaml`/`field.yaml` values are STARTING recommendations that change behavior for
those profiles (dev/no-profile stays byte-identical). Review before P1.2 lands.

- **demo:** require_heartbeat true, obstacle_brake true, max_follow_range 3.0, coast_frames 6,
  max_seconds 90.0, obstacle_target_margin 0.4.
- **field:** require_heartbeat true, obstacle_brake true, obstacle_brake_start 2.0, max_follow_range 4.0,
  hb_stale_ms 300, coast_frames 4, max_seconds 120.0, min_safe_range 0.8, vx_max 0.15.

- [ ] Approve / adjust the demo + field values (see PHASE1_PLAN.md → P1.2).

---

## P2.1 — model/wheels location + blob strategy (human review)

**What:** the reorg can move `yolo11n-pose.onnx` / OSNet / `wheels/*.whl` into `models/`, or leave them at
root. Also whether model blobs go via git-lfs or a fetch script (brief P2.1/P2.3). Affects the app's
`Ensure-*` source paths.

- [ ] Decide models/wheels location + git-lfs vs fetch-script (default rec: `models/` for onnx, keep
  `wheels/` recipe; models via fetch-script not raw git). Pending Phase 2.

---

## P3.7 — fsm/control extraction: pure move vs injected interfaces (RESOLVED by default)

**What:** the brief's P3.7 says extract `fsm.py` + `control.py` "with perception/ID/bridge passed
in as **injected interfaces**." That is a **dependency-injection restructure of `Follower`** — a
BEHAVIOR-CHANGE, not a pure move, and it can only be fully verified with person-path clips (the
local gate is no-person).

**Decision (2026-07-07, my call — you were unsure):** **leave `Follower` (+`Seed`+`CommandChannel`)
as the thin orchestrator in `follow_person_k1.py`.** This honors the §2 invariant *"if a seam can't
be cut without a logic change, stop and flag it — do not improve while moving,"* keeps every P3
commit byte-identical, and matches the plan doc's own "leaves a thin orchestrator" phrasing. The
seam extractions (P3.0–P3.6) already deliver the testable-core goal.

- [ ] **Opt in** to the injected-interfaces refactor later as a deliberate BEHAVIOR-CHANGE (needs
  person-path clips / robot validation), or leave as-is.

## P3.x — dataclass grouping (DEFERRED)

**What:** fold `Follower`'s ~77 `self._` fields into `SafetyState`/`RelockState`/`TrackState`/
`VizState`/`Other`. Field-for-field, no semantic change — but it rewrites *hundreds* of
`self.X → self.group.X` attribute accesses across the orchestrator (large, error-prone; marginal
value now that the seams are out).

- [ ] Deferred — do it if the orchestrator's state sprawl becomes a real maintenance problem.

## P4.3 — collapse detect + pose into one model (EVALUATED, merge PENDING P5.1)

**What:** the brief asks whether to drop the separate detection model and use the YOLO-pose model's
person boxes for detection too (it yields boxes **and** keypoints in one pass) — "evaluate, don't
assume; only merge if success holds."

**Data (on-Orin per-inference, dummy 480×640, median of 15, 2026-07-08):**

| model | backend | ms |
|---|---|---|
| `yolo11n.onnx` (detection, current) | ONNX/CUDA | **19.1** |
| `yolo11n-pose.onnx` | ONNX/CUDA | 19.7 |
| `yolo11n-pose.engine` (P4.2b) | TensorRT FP16 | **9.3** |

**Finding (corrects the intuition that pose is "heavier"):** pose ≈ detect on the same backend (the
keypoint head is nearly free on yolo11n), and the pose **TRT engine is ~half** the current detect
cost. So routing detection through the pose engine would cut the driving-loop neural cost
**19.1 → 9.3 ms (~51%)** — a win in **both** TRACK (1 pass, cheaper) and acquisition (2 passes → 1).
Not the regression I first assumed.

**Decision: do NOT merge yet — record the path, gate on P5.1.** The collapse swaps the (Booster-tuned)
`yolo11n.onnx` person boxes for the pose model's boxes; the brief requires **detection-quality parity
on the labeled suite before committing**, and P5.1 (the task-success scorer) is not built. Cost is
resolved (favorable); *quality* is the open risk. Also note a lower-risk alternative that needs no
model swap: TRT the **existing** detect model (same person-box quality, just FP16 → ~9–10 ms) — but
it's a Booster `.onnx` with no `.pt`, so that needs a raw ORT-TRT-EP runner (YOLO pre/post reimpl) or
a `trtexec` engine + runner (deferred with P4.2b detection-TRT).

- [ ] After P5.1 exists: A/B the pose-engine-for-detection path vs the detect model on the labeled
  suite; merge only if person-box success holds. Else keep both (current split is fine) and/or take
  the TRT-the-detect-model route instead.

---

## P6.1a — Record SDK odometry (`/odometer_state`)?

**What:** P8 (cross-run stitching) wants a trajectory prior and P7 could use odometry as an extra
observation. The C++ bridge exposes **no** pose feedback (velocity-out only), so odometry would come
from the ROS topic the K1 reportedly publishes (`/odometer_state`, planar x/y/θ).

**Why not wired in P6.1:** the exact ROS **message type** can't be confirmed off-robot, and adding a
live subscription with a guessed type is not a mechanical, verifiable change (§0 of the brief: "when
unsure, stop and ask"). It is also **not** part of the P6.1 gate (RGB + depth + intrinsics + JSONL).

**To action (small follow-up commit), confirm on-robot first:**
```
ros2 topic info /odometer_state     # exact type + publisher present?
ros2 topic hz   /odometer_state     # publish rate
```
Then add a guarded BEST_EFFORT subscription (topic configurable, default `''` = disabled → inert) that
logs `x/y/θ` to the `.rrd` and an `odom_x/odom_y/odom_theta` triple into the JSONL. Fail-safe: a
missing or mismatched topic records nothing and never touches the follow.

- [ ] Confirm `/odometer_state` type/rate on-robot, then wire the guarded odometry recorder.

## P6.1b — A `capture.yaml` profile for P7/P8 runs?

**What:** pixels + depth + intrinsics record **only** under `--rerun` (save mode), which `demo`/`field`
leave **off** for loop-cost (P4.5). So P7/P8 data collection needs a distinct capture-enabled profile.

**Recommendation:** add `config/capture.yaml` (inherits `field`/`demo` safety, then sets
`rerun: true`, `rerun_mode: save`, and a chosen `rerun_image_every_n` trading fidelity vs loop-cost).
A profile changes nothing unless selected, so this is SAFE recording-config — but the fidelity/N and
whether capture piggybacks on a demo or is operator-selected is a call worth making deliberately
(and the `--rerun`+`--drive` loop-cost gate in `RERUN_PLAN.md` must pass at the chosen N).

- [ ] Decide capture-run mechanism: standalone `capture.yaml` vs a `--capture` flag on an existing
  profile; set `rerun_image_every_n` for P8 fidelity within the loop-cost budget.
