# P4.1 — Follow-loop timing baseline

Every Phase-4 perf knob (P4.2 TRT, P4.3 model collapse, P4.4 staleness retune) is judged against
the numbers on this page. Do not change a knob without a before/after against this table
(IMPLEMENTATION_README.md P4.1). Parser: `eval/loop_stats.py` over the `LOOP-MS` lines in
`k1_follow.err` (trailing ~60 s windows, logged every ~10 s, 600 samples @ 10 Hz when full).

## Capture A — 2026-07-07, stock clocks (UNPINNED), rerun auto-off

- **Run:** full DRIVE with armed heartbeat deadman, gesture lock → real follow, ~100 s
  (10 LOOP-MS windows). First successful drive on the refactored stack (post-P3 modules,
  post `b9843b0` bridge-protocol fix).
- **Launch:** `drive /boostercamera/head/raw/rgb --stream --standoff-m 1.2 --vx-max 0.18
  --appearance osnet --reid-engine osnet_x0_25_msmt17.onnx --auto-reacquire --arm-reacquire
  --rerun --rerun-mode save --require-heartbeat --lock-trigger gesture --commands`
- **Clock state:** GPU **unpinned** — confirmed post-run via sysfs: cur=306 MHz
  (min 306 / max 1173). The robot was rebooted minutes before the run and `jetson_clocks`
  does not survive reboot (the 2026-07-06 pinned measurement cut loop p99 ~25% and the
  pose-latency tail ~86%). Treat Capture A as the WORST-CASE clock state.
- **Rerun:** launched `--rerun` but `RERUN-DISABLED-SLOW` fired during warmup (8-frame
  over-budget streak), so **every window below is effectively rerun=off**. No rerun=on
  baseline exists yet.

### Raw windows (budget = 100 ms, 10 Hz)

| window | n | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| 1 (warmup) | 10 | 121 | 1052 | 1052 | 1052 |
| 2 | 100 | 110 | 134 | 1052 | 1052 |
| 3 | 191 | 108 | 129 | 159 | 1052 |
| 4 | 280 | 108 | 132 | 155 | 1052 |
| 5 | 369 | 109 | 133 | 153 | 1052 |
| 6 | 459 | 108 | 129 | 152 | 1052 |
| 7 | 556 | 107 | 128 | 150 | 1052 |
| 8 (full) | 600 | 104 | 124 | 149 | 155 |
| 9 (full) | 600 | 96 | 121 | 147 | 153 |
| 10 (full) | 600 | 86 | 118 | 143 | 153 |

### Headline numbers (steady state = the three full 600-sample windows)

| metric | value |
|---|---|
| p50 | **86–104 ms** |
| p90 | **118–124 ms** |
| p99 | **143–149 ms** |
| max (post-warmup) | **153–155 ms** |
| warmup spike (one-off, first inference) | **1052 ms** |

### Reading

- The loop **misses the 100 ms budget at p50 in the early minutes** and only dips under it
  late in the run (86–96 ms) — at stock clocks, 10 Hz is not reliably held. This is the
  P4.2 (TRT + warm-up) target.
- The single **1052 ms first-inference spike** rides the max column for ~60 s of windows.
  P4.2's startup warm-up inferences should eliminate it. P4.4 must account for it either
  way: a 400 ms staleness tier armed from frame 0 would trip on this spike (today's
  800/3000 ms tiers absorb it only because the velocity stream hasn't started yet — verify
  that ordering holds before retuning).
- **P4.4 implication (do NOT act before P4.2):** healthy steady p99 ≈ 150 ms unpinned →
  1.5× ≈ 225 ms. The 400 ms goal in the brief looks reachable once clocks are pinned and
  TRT is in; retune only against a **pinned + P4.2** capture, not this one.

## Capture B — 2026-07-07, PINNED clocks (`sudo jetson_clocks`), rerun ON

- **Run:** full DRIVE follow (armed heartbeat, gesture lock → `LOCKED ... conf=0.92`), ~40 s
  (4 LOOP-MS windows, run ended clean). Same launch line as Capture A.
- **Clock state:** GPU **pinned** — cur=max=min=**1173 MHz** (confirmed before and after the run).
- **Rerun:** stayed **ON for the entire run** — every window `rerun=on`, **no `RERUN-DISABLED-SLOW`
  shed**. This is the fix for the "recording cuts off mid-run" report: at stock clocks the loop was
  over budget and the loop-cost backstop dropped Rerun ~20 s in (Capture A); pinned, the loop stays
  under budget so the recording runs full-length (499 MB `.rrd`, `k1_follow_1783471908.rrd`).

### Raw windows (budget = 100 ms, 10 Hz, rerun=on)

| window | n | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| 1 (warmup) | 10 | 100 | 826 | 826 | 826 |
| 2 | 109 | 75 | 102 | 122 | 826 |
| 3 | 208 | 73 | 101 | 122 | 826 |
| 4 | 307 | 70 | 98 | 122 | 826 |

### A/B — pinning is a clear win (note B carries Rerun's load and is STILL faster)

| metric | Capture A (stock, rerun **off**) | Capture B (pinned, rerun **on**) | Δ |
|---|---|---|---|
| p50 | 86–104 ms | **70–75 ms** | ~−28% |
| p90 | 118–124 ms | **98–102 ms** | ~−18% |
| p99 | 143–149 ms | **122 ms** | ~−18% |
| max (warmup spike) | 1052 ms | **826 ms** | ~−21% |

- The loop now holds **under the 100 ms / 10 Hz budget at p50 from the first steady window** — and
  that's *with* Rerun on. Pinned + rerun-off (the true P4.2/P4.4 reference) will be lower still, so
  **122 ms is an upper bound** on the healthy pinned p99.
- **Pin persistence is the actionable item:** `jetson_clocks` does not survive reboot, and every
  reboot this session reset it (Capture A was an accidental unpinned run right after a reboot).
  Make pinning durable (boot service / NOPASSWD sudoers) — tracked in DECISIONS.md — or every
  session silently regresses ~20–28%. `run_follow.sh` already warns when unpinned.

## P4.2 outcome (2026-07-08)

- **P4.2a — PersonDetector warm-up (SAFE, shipped `451d364`, deployed):** YOLO detect was the only
  model not warmed at construction, so its first predict built CUDA kernels *in* the loop = the
  ~826/1052 ms first-window spike. Now 3 dummy predicts run at construction. Byte-identical
  (COMPARE-OK ×2). VERIFY ON ROBOT: first-window `LOOP-MS max` should fall from ~826 ms toward steady.
- **P4.2b — pose model on TRT (BEHAVIOR-CHANGE, shipped `ba67283`+`4130083`):** built
  `yolo11n-pose.engine` (FP16) on the Orin from the local `.pt` via `stage_pose_engine.py`. Measured
  A/B (same ultralytics pipeline, dummy 480×640, median of 12): **onnx/CUDA 21.3 ms → engine/TRT
  9.8 ms (~54% faster)**. App now prefers the engine when present (`Resolve-GestureModel`), reverts
  to `.onnx` if deleted. Pose runs in acquisition states only, so this does **not** move the driving
  loop p50/p99 — it cuts acquisition-frame cost and the `GESTURE-DISABLED-SLOW` risk. VERIFY ON ROBOT:
  hand-raise still locks reliably (FP16 gesture quality).
- **Detection TRT — deferred by design:** the detection model is a Booster-shipped `.onnx` with no
  matching `.pt` on the offline robot, so a clean ultralytics `.pt→.engine` isn't available and a raw
  ORT-TRT reimplementation of YOLO pre/post is high-risk with no P5.1 to validate. Detection is the
  only model that runs every driving frame, but the loop already holds budget at pinned clocks, so
  this waits for a provenance/parity check + P5.1.

## P4.4 — staleness-tier retune (BEHAVIOR-CHANGE to the C++ floor, staged 2026-07-08)

The ONLY permitted edit to `loco_follow_bridge.cpp` in this brief. The command-staleness watchdog
keys on `age = now - last 'v'`, which IS the follow loop's inter-iteration dt (`LOOP-MS`): tier-1
(`STALE_MS`) zeroes velocity, tier-2 (`STALE_PREP_MS`) stands. The tier must sit ABOVE the worst
healthy loop dt (or it stutter-stops a good follow) yet as low as possible to shrink runaway.

**Change:** `STALE_MS 800 → 400`, `STALE_PREP_MS 3000 → 1000` (returning to the pre-pin goal).
Basis: pinned p99 ~122 ms, steady max ~155 ms, warmup spike moved off-loop by P4.2a. 400 ms keeps
~2.6× margin over the steady max while halving the tier-1 runaway window — **14.4 cm → 7.2 cm** at
`vx_max` 0.18 m/s. HB deadman tiers (400/1500) untouched. Deployed: recompiled clean on the Orin,
binary newer than source (run_follow.sh won't rebuild).

**NOT YET VERIFIED — VERIFY ON ROBOT (the gate):** run a normal healthy follow (person in view,
walking) for 1–2 min, then check the log:
```
grep -c "WATCHDOG stale" /home/booster/k1_follow.err   # MUST be 0
```
Zero `WATCHDOG stale` lines = no healthy-loop stutter-stop → pass. If any fire, the loop tail exceeds
400 ms on this robot → loosen `STALE_MS` toward the observed max and re-verify. (Tier-1 is only a
brief velocity-zero-in-gait, so a stutter during the test is safe, not a fall.)

### Reference numbers this was set against

Healthy pinned p99 ≤ 122 ms → 1.5× ≤ ~185 ms; the 400 ms chosen is more conservative than the
1.5×-p99 floor, deliberately, because a safety tier should clear the worst-case transient (crowded
ReID frame, depth glitch), not just p99. A pinned **rerun-off** capture would refine this further.
