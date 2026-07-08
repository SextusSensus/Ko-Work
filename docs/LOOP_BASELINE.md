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

## Capture B — pinned clocks (`sudo jetson_clocks`) — **TBD**

Pending a 1–2 min follow with the GPU pinned. This is the state P4.2/P4.4 must be compared
against, since pinning is already the shipped Phase-0 recommendation (`run_follow.sh` warns
when unpinned; persistence across reboot is an open decision in DECISIONS.md).
