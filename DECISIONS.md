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
