# Follow-Loop Optimization Plan — speed + reliability without sacrificing quality

**Node:** `K1Finder/follow_person_k1.py` (10 Hz safety-critical control loop, Jetson Orin, ROS2 Humble)
**Authored:** 2026-07-06, through the five autonomy skills (planner / ops / runtime-safety / advisor / policy-engineer)
**Method:** 6-dimension adversarial code profile (34 findings) + live on-Orin hardware probe + hand grounding.

---

## Bottom line

The 10 Hz loop sits **right at its 100 ms budget** (drive-mode p50 77–97 ms, p90 up to 126, p99 163, warmup 970).
That near-budget state is what shed `--rerun`, flickered the gesture lock, and threw `SLOW-LOOP` warnings.

The **single dominant controllable cost** is the gesture pose model (`yolo11n-pose`) running on **every frame**
in the default gesture-lock mode — `every_n` is forced to `1` at [follow_person_k1.py:1891](../follow_person_k1.py) —
at **p50 56 ms / p99 279 ms**. Plus a hardware finding the code can't see: the **GPU is not clock-pinned**
(measured live at **306 MHz of 1173 MHz max**; `jetson_clocks` was never applied), so every inference pays a
cold-clock ramp — that's the p99 tail and part of the warmup spike.

**Sequencing philosophy (measure between phases):** do the free hardware/model wins (Phase 0) and the
byte-identical quick wins (Phase 1) FIRST, re-measure `LOOP-MS` p99 on-robot, and only then decide whether the
structural rework (Phase 3) is even needed. It very likely won't be after 0+1.

**The invariant contract — nothing below is allowed to violate these:**
- **INV-1** 3-layer velocity clamps + `forbid_forward` keystone (forward vx never leaks past a forbid).
- **INV-2** byte-identical-when-off for optional features (`--rerun`, `--obstacle-brake`, `--commands`).
- **INV-3** FSM safe-floor: a stale/lost track forces zero velocity (watchdog enforcement, separate from FSM).
- **INV-4** re-ID identity trust: never lock/hold the wrong person; no relock-lunge.
- **INV-5** depth-safety: no forward drive on the bbox-height pinhole when depth is down.

---

## Phase 0 — Free hardware + model wins (no control-logic change, compounds with everything)

| # | Change | Location | Gain | Risk / mitigation | Owner |
|---|---|---|---|---|---|
| 0.1 | **`jetson_clocks`** — pin GPU + EMC clocks to max. Measured live: GPU at 306/1173 MHz, not pinned (nvpmodel is `MAXN_SUPER`, CPU pinned, but GPU DVFS is dynamic). Wire it into `run_follow.sh` pre-flight (or a systemd unit) so it's a deliberate, visible bringup step. | `run_follow.sh` pre-flight; `sudo jetson_clocks` | Removes the per-inference DVFS ramp → cuts p50 **and** p99 variance + shrinks the 970 ms warmup spike. Likely the biggest single win, **zero code risk.** | **Higher power draw + heat**, doesn't survive reboot. Mitigation: apply only for follow sessions, measure battery impact, make it an explicit bringup line (ops owns it). | policy-engineer / ops |
| 0.2 | **Re-export the pose model TRT-FP16 @ imgsz 480** (`stage_pose.py`: `.export(format='engine', half=True, imgsz=480)`). Today it's plain FP32 ONNX @ 640 on the CUDA EP via the heavy `ultralytics.predict()` wrapper — while ReID already uses a lean raw-ORT **TensorRT** path. A raised hand is coarse whole-body geometry; 640-FP32 is overkill. | `stage_pose.py:35`; consumed at [follow_person_k1.py:371](../follow_person_k1.py) | imgsz 640→480 ≈ 0.56× pixels (~30–40% pose-latency cut); +FP16 more; a TRT engine can bring p50 ~56→~20–30 ms and compress the 279 ms p99 tail. | Lower res/FP16 → weaker keypoint precision → risk is a **missed** real raise (safe: no false lock), never a wrong-person lock (INV-4 gates are geometry/IoU, unchanged). Mitigation: A/B in `--lock-trigger both` (the GBIND audit harness) before flipping default; validate raise-detect range on-robot; reversible by swapping the model file. | policy-engineer |

---

## Phase 1 — Quick code wins (byte-identical-verifiable, directly move stationary p50/p99)

Every item is validatable by `replay_eval COMPARE-OK` (control/velocity byte-identity) + an on-robot `LOOP-MS`/`GESTURE-MS` probe. None touch the clamps, FSM behavior, re-ID identity gates, or depth-safety latch.

| # | Change | Location | Gain | Risk / mitigation | Owner |
|---|---|---|---|---|---|
| 1.1 | **Skip pose entirely when no persons this frame** — early-return `(set(), {})` at the top of `_raisers` when `not persons`. A gesture can only bind to a tracked person, so a zero-candidate pose run is guaranteed-None waste. | `_raisers` [:454](../follow_person_k1.py); call site [:559](../follow_person_k1.py) | Kills a full 56 ms (p99 279 ms) pose run on every empty-scene SEARCH/REACQUIRE frame — the frames the operator is waiting on. | Behaviorally equivalent (zero persons → IoU match already `continue`s → result already discarded). **Byte-identical.** | runtime-safety |
| 1.2 | **Apply `--gesture-every-n` in gesture mode** (the #1 lever). Change [:1891](../follow_person_k1.py) from `(… if lock_trigger=='both' else 1)` to `max(1,int(args.gesture_every_n))` (default 3). This **restores the intended design point** — `--gesture-hold`'s help text was written assuming every-n 3 ("6 @ every-n 3 ≈ 1.8 s"). Have the app pass it explicitly so it's operator-visible. | [:1889–1891](../follow_person_k1.py); `K1Finder.ps1:964` | Removes ~2 of every 3 pose inferences in stationary states: stationary p50 ~56→~19 ms amortized, p99 spikes 1/3 as often. **Directly fixes the lock flicker.** | Slower gesture sampling. **Pair with Phase 2.1** (wall-clock confirm) so time-to-lock doesn't lengthen under load. No safety invariant touched. Reversible via the flag; tune to 2 if 3 feels slow. | planner |
| 1.3 | **Cheap brightness pre-gate in `low_light_boost`** — test a strided-luma sample before the full-frame `BGR2YCrCb`; only do the round-trip when actually dark. Dark-branch CLAHE+gamma math untouched. | `low_light_boost` [:1463](../follow_person_k1.py); called [:2551](../follow_person_k1.py) | In the common bright case (incl. tightest-budget TRACK): removes a full-frame convert + mean + alloc per frame (~0.5–1.5 ms + allocator churn). | Trigger point must stay equivalent — validate on a dim clip that enhancement still fires. **Byte-identical on bright clips.** | runtime-safety |
| 1.4 | **Guard ReID cache write + eviction on `n>1`** — at the default `--reid-every-n=1` the cache is never read, yet `_embed_persons` writes it and rebuilds+scans the whole dict every TRACK frame. | `_embed_persons` [:3462–3473](../follow_person_k1.py) | Per TRACK frame: removes a set build, a full-dict scan, and N writes. Sub-ms but hottest branch; bigger in crowds. | At n==1 embeddings are always fresh → empty cache changes nothing. **Byte-identical.** Keep n>1 path intact. | runtime-safety |
| 1.5 | **Bounded `deque(maxlen=5)` for `_track_range_hist`** — delete the per-frame `[-5:]` slice-rebind (append auto-evicts). Same for `_clr_hist`. | [:1970](../follow_person_k1.py), [:3709](../follow_person_k1.py) | Removes a list alloc + slice copy per accepted TRACK frame; removes allocator churn from the hottest branch. | The F1 glitch-median (relock jump-gate reference) must read identical last-5 — a deque(maxlen=5) is the same window. **Verify the median read accepts a deque** (open question). | runtime-safety |
| 1.6 | **Clear `GestureTrigger.holds`/`_miss` on seed→TRACK entry** — today cleared only on FOLLOW re-arm ([:2259](../follow_person_k1.py)), so a SEARCH→seed→TRACK→loss→REACQUIRE cycle can carry a stale confirm count into a recycled track_id. | add clear at `_try_seed`/S_TRACK entry (~[:3107](../follow_person_k1.py)) | Removes a cross-episode confirm leak; every acquisition starts clean. | Only RESETS a counter at episode boundaries — can't make a lock easier. No invariant touched. | planner |

---

## Phase 2 — Reliability: wall-clock-normalize the frame-count timers

**Root mechanism:** the loop free-runs with no fill-sleep once `dt>period` ([:2609](../follow_person_k1.py)), so **every "N consecutive frames" gate stretches in wall-clock exactly when the loop is slow** — this *is* the gesture flicker, and it's what makes Phase 1 decimation safe. These change TIMING of accumulation only; identity/range margins untouched. Validated by arm-test, not byte-identity.

| # | Change | Location | Gain | Risk / mitigation | Owner |
|---|---|---|---|---|---|
| 2.1 | **Frame-rate-independent gesture confirm** — gate on continuous wall-clock hand-up time (`--gesture-hold-s ≈ 0.9–1.0 s`) via a per-track first-raise timestamp, PLUS a min frame floor (≥3). Raise `--gesture-miss-tol` to ~3–4 so one p99 blurry frame doesn't hard-reset a near-complete hold. **Safety partner to 1.2.** | `GestureTrigger.detect` confirm accounting [:566–577](../follow_person_k1.py) | Confirm time deterministic (~1 s) regardless of loop rate; eliminates the "6 frames unreachable at 6–12 Hz" flicker. | Require BOTH continuity (reset timestamp on a >miss_tol gap) AND the frame floor, so a brief spurious raise can't accumulate across a slow gap. Identity walls (2-raiser refuse, arm-owned, ambiguity) untouched → INV-4 held. | planner |
| 2.2 | **Wall-clock loss/coast bound** — add `--lost-grace-s` (~0.8 s) parallel to the frame counters on the loss→coast escalation; whichever fires first. Complements (not replaces) the C++ staleness watchdog. | `_track` loss [:3503](../follow_person_k1.py); `_try_coast` [:3770](../follow_person_k1.py) | Bounded real-time blind-hold independent of loop rate; predictable time-to-safe-stand under load. | Only ever moves toward a SAFER stand → **INV-3 strengthened.** Set the bound generously so nominal is byte-identical; only the slow-loop tail changes. | runtime-safety |
| 2.3 | **Wall-clock-normalize the relock ladder** (audit 5 → armed 8 → range-admission 2) as sustained-agreement DURATION with frame counts as a secondary floor (≥5). Leave the 30 s REACQUIRE path. **Do NOT touch identity margins.** | `_try_reacquire` [:3929/:3955/:4005](../follow_person_k1.py) | Walking-search relock completes under load instead of deferring to PARKED when the operator IS present; deterministic recovery. | Keep a min frame floor so identity votes stay sufficient; all identity/range gates (INV-4) unchanged — a look-alike still can't win. Arm-test with a decoy. | planner |
| 2.4 | **ID-stability debounce on track_id CHANGES** — when `best.track_id != seed.track_id`, require a few frames / ~0.3 s before re-binding, routing the interim through `_hold`/coast (last-good centroid) rather than switching on frame 1. Same-id updates stay byte-identical. | `_track` accept [:3555–3571](../follow_person_k1.py) | Prevents a 1–2-frame ByteTrack id hop during a crossing from re-pointing the follow at a look-alike. **Strengthens INV-4.** | Apply only to id changes; route through hold/coast → **never a new forward command** (INV-1/INV-5 respected). Assert no forward vx during the debounce. Byte-identical on single-person clips. | runtime-safety |

---

## Phase 3 — Structural: perception worker thread (default-OFF, arm-gated, LAST)

The only change that fixes the ROOT cause: **all inference runs synchronously on the control thread**, so p99 inference *is* p99 loop time. High leverage, high risk — touches the safety spine. **Ship Phases 0–2 first; they may put the loop comfortably under budget and defer this entirely.**

| # | Change | Location | Gain | Risk / mitigation | Owner |
|---|---|---|---|---|---|
| 3.0 | **MANDATORY PRECONDITION (ships in the SAME change as 3.1, never after):** stamp each perception bundle with its source-frame monotonic time; in the control loop compute `age = now - stamp`; if `age > --percept-max-age` (~1.5–2× period) treat exactly like NO-FRAME → `_stand()`/`_hold()` + zero velocity via the existing watchdog path, and refuse to advance any confirm/vote on a stale bundle. | new age gate in `run()` before consume; models on [:2571–2581](../follow_person_k1.py) | Converts decoupling from unsafe to safe with a microsecond check: worst-case inference latency becomes a freshness cost the staleness gate absorbs, never a stale-drive. **Preserves INV-3.** | Shipping 3.1 WITHOUT this violates INV-3. Pure arithmetic; only tuning risk is too-tight → spurious safing. Default ~2 periods, validate vs measured p99. Strictly additive to the NO-FRAME path. | runtime-safety |
| 3.1 | **Perception worker thread** — move YOLO detect + tracker.update + ReID embed (+ stationary pose) onto a dedicated worker consuming `take_if_new`, publishing an immutable bundle `{seq, stamp, persons, hint}` into a single-slot double-buffer via one atomic ref-swap under a new `_percept_lock`. The 10 Hz loop reads the freshest bundle each tick, never blocking on inference; FSM + clamps run on the control thread. Keep detect→embed→associate **atomic within one worker pass** (no cross-frame pipelining). Flag defaults OFF; inline path is the byte-identical fallback. (GIL note: `ort.run` + ONNX kernels release the GIL, so control-thread arithmetic genuinely overlaps inference — the concrete win.) | `run()` [:2476–2663](../follow_person_k1.py); `_process_frame` [:2553](../follow_person_k1.py) | Control-loop p99 drops from ~163 ms toward its non-inference cost (few ms) — a solid decoupled 10 Hz even when a frame spikes to 279 ms. Removes the mechanism behind SLOW-LOOP, the rerun shed, and the flicker. | **HIGHEST leverage, HIGHEST risk.** INV-1: clamps + `forbid_forward` stay on the control thread; never move velocity emission into the worker. INV-4: atomic single-ref publish (no torn `persons`/`_ph`). INV-2: default-OFF keeps inline byte-identical. INV-3: relies on 3.0 (ship together). Keep heavy rerun image/depth logging off this publish path. | runtime-safety |
| 3.2 | **(optional, only if crowd OSNet cost is measured as a real lever)** default `--reid-every-n` to 2–3 so isolated non-bound bystanders reuse a ≤n-frame cached embedding. The bound target + anyone overlapping are ALWAYS re-embedded ([:3449](../follow_person_k1.py)). | `_embed_persons` [:3449](../follow_person_k1.py) | Shrinks the OSNet batch on crowded frames. A refinement, not a top lever. | Bound/overlapping always fresh → INV-4 preserved; only isolated bystanders reuse. Keep n small; **measure on-robot before flipping the default.** | planner |

---

## Rejected / risky — tempting optimizations that WOULD sacrifice quality (do NOT do these)

1. **Don't run pose in TRACK/SEARCHING to "save a state check."** Pose is deliberately gated OFF in driving states ([:552](../follow_person_k1.py)); the S_TRACK gesture-overrun auto-disable ([:2651](../follow_person_k1.py)) exists *because* an in-follow pose frame destabilized a walking gait (the June trip). **Never run a heavy model in the control path while driving.**
2. **Don't weaken `--reid-every-n` on the bound target or overlapping people.** The always-re-embed branches are the identity moat (INV-4); caching the target's vector would let a stale embedding hold the WRONG person through a crossing — the relock-lunge failure.
3. **Don't fix the flicker by lowering `gesture-hold`/`miss-tol`.** That trades flicker for a spurious/false lock. The correct fix is wall-clock normalization (2.1) that keeps the same evidence, loop-rate-independent.
4. **Don't ship the worker (3.1) without its staleness gate (3.0), and don't move clamps/velocity into the worker.** A bundle that finished 200–970 ms ago would drive on a stale target — a direct INV-3 violation.
5. **Don't delete/short-circuit the `low_light` dark branch, the depth-starved latch (INV-5), or the C++ staleness watchdog to shave ms.** Safety floors, not perf knobs — the only sanctioned `low_light` win is gating WHEN it runs.
6. **Don't pipeline `detect(N)` against `embed(N-1)`.** A one-frame identity lag complicates the anti-glitch range history + re-ID trust (INV-4). Keep detect→embed→associate atomic; decouple only worker-from-control.
7. **Don't drop the ambiguity / arm-owned / two-raiser refusal gates** to make gesture cheaper — those are the wrong-person-lock walls.

---

## Open questions (resolve before the items they gate)

1. **Measured OSNet `embed_batch` latency + typical person-count/frame** — needed to size the 3.2 crowd default. Add GESTURE-MS-style instrumentation to the ReID path first.
2. **Does the Orin toolchain build a clean `yolo11n-pose` TRT `.engine`?** If flaky, fall back to imgsz=480 + `half=True` ONNX (lower risk). (0.2)
3. **Operator-acceptable hand-up-to-lock dwell** — sets whether every-n 3 (~1.8 s) is fine or should be 2. Decide via arm-test. (1.2)
4. **Is Phase 3 even needed after 0–2?** Re-measure `LOOP-MS` p99 on-robot after 0+1 before committing to the structural change.
5. **Does the F1 median read site accept a `deque`?** Verify before shipping 1.5 to guarantee byte-identity.

---

## Validation discipline (the gate for every change)

- **Byte-identity** (Phase 0 model-swap, all of Phase 1, the OFF path of Phase 3): `replay_eval selftest` + `replay_eval compare --flags-b=…` must return **COMPARE-OK**.
- **Behavior** (Phase 2, Phase 3 ON path): arm-test on-robot — gesture confirms in ~1 s across fast + slow loops; a decoy cannot win a relock; no forward vx during a debounce hold.
- **Latency** (Phase 0, everything): on-robot `LOOP-MS` p50/p99 + `GESTURE-MS`, measured **in deploy power mode** (post-`jetson_clocks`) — p99 is the number that matters (one tail spike = one dropped control cycle).
- **Sequencing:** 0 → measure → 1 → measure → decide on 2 → decide on 3. Never batch across phases without a measurement gate.

## Ownership map

- **policy-engineer** — 0.1 (clocks), 0.2 (TRT/FP16 pose export), the latency-measurement discipline.
- **planner** — 1.2/1.6 (gesture cadence + lifecycle), 2.1/2.3 (confirm + relock timing), 3.2 (crowd default).
- **runtime-safety** — 1.1/1.3/1.4/1.5 (byte-identical in-loop trims), 2.2/2.4 (safe-floor + ID debounce), 3.0/3.1 (staleness gate + worker).
- **autonomy-ops** — 0.1 bringup wiring, the loop-cost probe, power/thermal budget of pinned clocks.
- **embodied-ai-advisor** — kept the plan honest: measure before the structural rework; the worker is high-risk and may prove unnecessary.
