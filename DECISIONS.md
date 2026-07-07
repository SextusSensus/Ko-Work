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

**Recommendation: KEEP.** It's the printable source for the still-supported ArUco
lock trigger, and it's tiny (~43 KB).

- [ ] **Keep** `follow_marker.png`
- [ ] **Remove** it (ArUco lock trigger is being fully retired in favor of gesture)

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
`K1Finder.ps1` is the shipping UI. But per the brief, *"one of the two app stacks
is the intended final UI"* — so **confirm the direction** before I `git rm`.

- [ ] **Remove** the WPF stack + `_redesign/` (WinForms `K1Finder.ps1` is final)
- [ ] **Keep both** for now (WPF redesign is still intended)
- [ ] **Other** (e.g. keep `_redesign/` docs, remove only the WPF `.ps1`/launchers)

---

## P1.2 — Does a heartbeat *writer* exist? (pending — resolved in Phase 1)

**What:** the untethered operator-deadman (`--require-heartbeat`) reads a heartbeat
file/signal; the deadman is inert unless something *writes* it at ~10 Hz.

**Status:** to be verified when Phase 1 is executed. `k1_hb` / heartbeat references
exist in 13 files (node + app + docs); whether a live ~10 Hz *writer* is present is
not yet confirmed. Will be filled in here with the finding (and flagged if none).

- [ ] (pending Phase 1)
