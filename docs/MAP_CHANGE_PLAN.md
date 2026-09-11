# Map-Change Manager — design

**Status:** design + offline module. NOT wired into the follow loop yet. `VERIFY ON ROBOT` for the
in-loop emitter (loop cost unmeasured — the robot is down as of 2026-09-08, Aurora 12V adapter failed).

**One line:** watch the live depth view against the prebuilt map-assist prior, and when the world
disagrees with the map, record *what* changed, *where*, and *how sure we are* — so the robot
accumulates a documented picture of the space that has changed around it.

---

## 1. Why this is tractable now (and wasn't before)

The expensive part of change detection is **registration**: two observations of the same room are
useless until you know they are in the same frame. `docs/RECON_CONTRACT.md` explicitly DEFERS
cross-run alignment (P8.3) for exactly this reason — each Aurora scan session has its own arbitrary
origin, so `drive_20260907_164419.ply` and `drive_20260908_101056.ply` cannot be subtracted.

Map-assist does not have that problem. The Aurora odometry-floor bridge publishes a **MAP-frame**
pose on `/aurora_odom`, and `_map_assist_confirm` already uses it to place `self._ma` (the prior)
into the robot's forward corridor. So at runtime, live depth returns and prior points are *already
in one frame*. **Relocalization is the registration.** The change detector is then a comparison on
a shared grid, not a SLAM problem.

This is the whole reason the manager can exist. It also fixes its hard precondition:

> **The manager is INERT unless `--odom-topic` carries a map-frame pose.** With `/odometer_state`
> (robot frame, per-run origin) the prior and the live view are in different frames and every
> comparison is garbage. Fail closed: no map-frame pose → emit nothing. This mirrors the existing
> drive-gate frame assertion in `run()`.

---

## 2. The safety position — this changes NO decisions

The manager is a **pure sink**. It reads the same depth selection the obstacle layer already
computes and writes findings. It returns no clearance, caps no velocity, and is not consulted by
`_corridor_clearance`, `_obstacle_memory`, `_localmap_clearance`, or `_map_assist_confirm`.

That is deliberate and it is what makes it shippable. The invariant across this whole subsystem is:

> memory may only ever REDUCE clearance, never raise it (`_localmap_update` docstring).

A change detector is the natural place to violate that, because its most interesting finding is
exactly the forbidden one — *"the map says obstacle here but the world is now clear."* Acting on
that would let a stale-map bug open the throttle. So the VANISHED class is **reported and never
acted on**. If a future task wants to use it, that is a separate `BEHAVIOR-CHANGE` with its own
review; it is out of scope here.

Consequence: a bug in the manager produces a wrong *report*. It cannot produce a collision.

---

## 3. What counts as "out of place"

Two classes, asymmetric in both difficulty and safety.

| Class | Meaning | Evidence needed | Safety |
|---|---|---|---|
| `APPEARED` | live return in a cell the prior calls empty | an occupied cell | live depth already brakes on it; report is additive |
| `VANISHED` | prior point in a cell the live view sees *through* | a carved free-space ray | **report only, never acted on** — this is the stale-map case |

**`VANISHED` requires evidence of absence, not absence of evidence.** A prior cell with no live
return usually just means we did not look there — occlusion, the height band, the stride subsample,
or the cell being outside the depth cone. The only honest evidence that a mapped obstacle is gone is
a depth ray that **passes through the cell and terminates beyond it** (or returns nothing out to
max range). So the update carves free space along each ray up to `return_range − carve_margin`, and
only carved cells can vote VANISHED. Without the carve this class is a noise generator.

---

## 4. Algorithm

Per observation tick, given a map-frame pose `(x, y, θ)` and the depth selection already computed by
`_localmap_update` (height-banded, operator-excluded, stride-subsampled):

1. **Rasterize the prior once** at load: `self._ma` (N,2) → a `set` of occupied grid keys at
   `--map-change-res-m` (default 0.10 m, matching `localmap_res_m`). Done once, never edited.
2. **Project live returns** to map frame → occupied cell keys this tick.
3. **Carve** free cells along each ray from the robot to `range − carve_margin`, plus full-range
   rays for no-return bearings.
4. **Vote** per cell: `live-occupied ∧ prior-empty → APPEARED`; `carved-free ∧ prior-occupied →
   VANISHED`; agreement clears nothing (agreement is the common case and is not stored).
5. **Accumulate** votes in a persistent per-cell record: `{votes, first_t, last_t, n_views,
   viewpoint_bearings}`.
6. **Gate on persistence AND parallax** before a cell is a finding (§5).
7. **Cluster** surviving cells into connected regions — a change is a contiguous object, not N loose
   cells. This mirrors the contiguity doctrine already applied to the obstacle layer (commit
   948f64c: "an obstacle is a connected NEAR region, not N loose pixels").
8. **Emit** one `map_change` record per region, and fold it into the persistent ledger (§6).

## 5. The parallax gate — and a free diagnostic

A finding requires the cell to be seen changed from **≥ `--map-change-min-views` distinct viewpoint
bearings** (default 2, separated by `--map-change-min-parallax-deg`, default 25°), across
`--map-change-min-votes` observations.

This is not just noise rejection. It has a specific and valuable property:

> **A body-fixed sensor artifact cannot survive the map frame.** Anything rigidly attached to the
> robot — the arm, the Aurora rig, a lens obstruction — sits at a fixed *robot-relative* position, so
> as the robot moves it smears across many different *map* cells and never accumulates votes in any
> one of them. A real object in the world does the opposite: it accumulates in exactly one place from
> every viewpoint.

That is directly relevant to the open bug: the persistent ~0.36–0.53 m reading that fully brakes the
robot while the map reports the real obstacle at 1.71 m. If that phantom is self-occlusion, the
manager will show it as a **smear of low-vote cells that track the robot** and never as a finding —
which would be positive evidence for the self-occlusion hypothesis, from data we already record.
Diagnostic byproduct, not a design goal, but worth the instrumentation.

## 6. Outputs — the documentation layer

Three artifacts, in the repo's existing formats. No new format is invented.

**(a) Per-run events → `k1_events.jsonl`** (the always-on log written by `robot/common.py:EventLog`,
joined on wall-time `t` like every other artifact per `docs/RUN_ARTIFACTS.md`). One line per emitted
finding:

```json
{"t": 1757340012.412, "ev": "map_change", "cls": "APPEARED",
 "map_xy": [3.42, -1.08], "extent_m": [0.40, 0.30], "cells": 12,
 "votes": 31, "n_views": 4, "parallax_deg": 71.0, "conf": 0.86,
 "nearest_prior_m": 0.62, "map": "drive_20260908_104304"}
```

**(b) The change ledger → `map_changes/<map_id>.json`**, kept next to the map and **accumulating
across runs**. This is the "increased perception of the changed space" — keyed by map location, so a
spot that changes repeatedly builds history rather than being re-reported cold every run:

```json
{"map_id": "drive_20260908_104304", "res_m": 0.10, "updated": "...",
 "regions": [{"id": "r_0342_-108", "map_xy": [3.42,-1.08], "cls": "APPEARED",
              "first_seen": "...", "last_seen": "...", "n_runs": 3,
              "observations": [{"run_id": "...", "votes": 31, "conf": 0.86}],
              "status": "persistent"}]}
```

`status` ∈ `new | persistent | resolved` — `resolved` when later runs stop confirming it, which is
how the ledger self-cleans instead of growing forever.

**(c) The report.** `eval/map_change_report.py` renders the ledger into a human/agent-readable
summary: what changed, where, how confident, whether it is new or long-standing. This is the layer an
agent reads to narrate findings; the detector itself is pure numerics and never calls a model.

---

## 7. Where it runs, and why offline first

**The control loop must not grow a new per-frame cost that has never been measured** (`P4.1`:
measure the loop before changing perf knobs; the loop already has SLOW-LOOP and camera-stall
problems). So:

- **Now (robot down):** `robot/map_change.py` is a pure, dependency-light module — numpy only, no
  rclpy, no ROS imports — driven by `eval/map_change_selftest.py`. Fully buildable and testable on
  the Windows box.
- **Later (`VERIFY ON ROBOT`):** wire it into `_localmap_update`'s existing selection behind
  `--map-change on` (default **off** → byte-identical decision stream when unset, per §3 of
  CLAUDE.md), measure the added p50/p99, and only then decide whether it runs in-loop every frame,
  decimated, or purely post-run from the recorded artifacts.

Splitting it this way means the module is validated before it ever touches the loop, and the
in-loop step is a small, reviewable diff.

---

## 8. Offline validation plan (no robot required)

1. **Synthetic, known-truth:** a generated prior + generated depth observations with a *planted*
   change. Assert the manager finds it, localizes it within a cell or two, and — the important half —
   assert a run with **no** planted change emits **zero** findings. A change detector that cannot stay
   silent is worthless.
2. **Body-fixed artifact:** inject a return at a constant robot-relative range across a moving pose
   trace. Assert it produces **no** finding (§5). This is the phantom-obstacle case.
3. **Real prior:** run 1 and 2 against the actual `drive_20260908_104304.ply` so the point density,
   noise, and the outlier landmarks are real rather than idealized.
4. **Real pose traces:** drive the viewpoint sequence from the recorded localization CSVs in
   `C:\Users\toddm\aurora_sdk\localization_data\` rather than a synthetic path.

Gate: `python eval/map_change_selftest.py` prints `SELFTEST-OK`.

### Results (2026-09-08, all green)

Synthetic cases all pass, including both negatives. Against **all three real Aurora maps**, driven
over each map's own recorded trajectory:

| map | band pts | poses | self-consistent | planted object |
|---|---|---|---|---|
| `drive_20260907_164419` | 1,620 | 629 | 0 findings | detected (70° parallax) |
| `drive_20260908_101056` | 10,251 | 1,950 | 0 findings | detected (147°) |
| `drive_20260908_104304` | 4,787 | 1,803 | 0 findings | detected (211°) |

Two real defects were found by these cases and fixed, rather than tuned around:

1. **Stray-landmark false VANISHED.** On 104304 the first real run produced one finding on cells
   holding a *single* raw prior point. These maps carry a tail of outlier landmarks
   (`force_map_global_optimization` is device-blocked, so they are never pruned) and most occupied
   cells hold exactly one point — 2,103 of ~3,000. Walk through where a floating landmark appears to
   be and the algorithm honestly reports it gone, but it was never there. Fixed with
   `min_prior_support` (default 3): only multi-point, real structure may vote VANISHED.
2. **Harness ray-slip.** Casting synthetic rays against the *sparse* cloud let them pass between the
   points of a real wall and carve through it, manufacturing false VANISHED on well-supported cells
   (support 3 and 7 on 101056). Real depth is dense and stops at the surface, so the *simulated
   world* is now dilated solid while the *prior* handed to the manager stays sparse and untouched.

The end-to-end ledger lifecycle was also exercised on 104304: an object present for two runs and
removed for the third accumulates as `persistent` with per-run corroboration, then stops being
confirmed.

## 9. Known limitations — stated, not hidden

- **Depth holes are a real-world version of the harness ray-slip.** Dark, reflective, and very
  distant surfaces return no depth. A ray that slips through a genuine hole in the live depth can
  carve through mapped geometry and produce a false VANISHED. Mitigated by persistence + parallax +
  `min_prior_support`, not eliminated. This is a report-quality issue and not a safety one *only
  because* VANISHED is never acted on (§2).
- **Not validated against real recorded depth.** `runs/` is empty and the replay stub still forces
  `latest_depth → None` (P5.2 unbuilt), so every case here uses synthetic raycast. Real-depth
  behaviour is `VERIFY ON ROBOT` and is not claimed.
- **Loop cost unmeasured.** The in-loop emitter is not written yet, for exactly this reason (§7).
- **Confidence is an ordering heuristic**, not a calibrated probability. It exists so the report can
  put the best-corroborated finding first.
- **A dense map may have no plantable open floor.** On 101056 the strict spot search (open floor,
  clear line of sight, ≥60° parallax) can come up empty on some trajectories; the case reports SKIP
  rather than failing, since a detector cannot be faulted for missing what was never observed.
