# Phase 4 — Runtime & perf: plan

`IMPLEMENTATION_README.md` Phase 4. This is the **robot-runtime-perf** phase: most gates measure or
tune the Orin's on-device inference + loop latency, so they can only run on the robot. Off-robot I can
write the code (the TRT path, the STAND escalator, the batch clamp) + headless tests; the *measure-and-
tune* gates are `VERIFY ON ROBOT`. **No perf knob changes without a before/after number** (§6).

## Robot needed?  ✅ on + drive-capable for the perf gates
| Task | Tag | Robot? |
|---|---|---|
| P4.1 measure loop p50/p99 | SAFE | a real `k1_follow.err` (send one, or run once) |
| P4.2 TRT models + warmup | BEHAVIOR-CHANGE | **robot** (device TRT build + p99) |
| P4.3 collapse 2 models → 1 | BEHAVIOR-CHANGE | **robot** + labeled suite |
| P4.4 retune staleness tiers | BEHAVIOR-CHANGE | **robot, drive-capable** |
| P4.5 batch clamp + demo cost-gate | SAFE | code off-robot; verify on robot |
| P4.6 persistent-fault STAND escalator | BEHAVIOR-CHANGE | code + headless selftest off-robot; real stand on robot |

---

## P4.1 — Measure the loop first  `SAFE`  (the baseline everything else is judged against)
The node **already emits** the timing: `_loop_ms_hist` (deque, 60 s @10 Hz), a 10 s-cadence `LOOP-MS`
line with p50/p99/max, and a `/diag/loop_ms` scalar into the `.rrd` (`follow_person_k1.py:307-308,
1089-1093`). So P4.1 is: **parse p50/p99 from `k1_follow.err`** (or the `.rrd`) and record the numbers
in `docs/` (a `LOOP_BASELINE.md`), split by rerun-on/off and pose-on/off.
- **Do off-robot** if you give me a recent `k1_follow.err`; I'll add a tiny `eval/loop_stats.py` parser
  (there's already `eval/rrd_loop_stats.py` for the `.rrd` path — reuse it).
- **Gate:** the recorded p50/p99 table lands in `docs/` before any perf knob moves.

## P4.2 — TensorRT both neural models + warm-up  `BEHAVIOR-CHANGE`  `VERIFY ON ROBOT`
- **Already done for OSNet:** `identity.py:293-297` runs the ReID engine on the ORT **TensorRT EP**
  (fp16 + engine cache). The reference config is `eval/onnx_dynamic_batch.py:42-46`.
- **Remaining:** put **YOLO detection** (`perception.PersonDetector`, ultralytics `YOLO(..., task=detect)`)
  and the **YOLO-pose** gesture model on the same TRT-EP path (fp16, cached under `/home/booster`), and
  run **N warm-up inferences at node startup** (the first ultralytics/TRT inference is slow — a cold
  first frame otherwise blows the loop budget and can trip P4.4's tiers).
- **Off-robot:** I can write the TRT-provider wiring + the warmup loop (mirroring OSNet + the eval ref).
- **Gate:** `VERIFY ON ROBOT` — p99 loop dt drops vs the P4.1 baseline; follow success unchanged on the
  P5.1 labeled suite.

## P4.3 — Collapse two models into one (evaluate, don't assume)  `BEHAVIOR-CHANGE`  `VERIFY ON ROBOT`
Detection uses `yolo11n.onnx`; the gesture trigger uses `yolo11n-pose.onnx` — **two forward passes**.
YOLO-pose yields person **boxes AND keypoints**. Prototype using the pose model's boxes for detection
too (one model). **Compare detection quality on the labeled suite BEFORE committing** — only merge if
success holds; if it regresses, keep both and record why.
- **Gate:** `VERIFY ON ROBOT` — equal-or-better follow success at ~half the neural cost.

## P4.4 — Retune the staleness tiers — ONLY after P4.2  `BEHAVIOR-CHANGE`  `VERIFY ON ROBOT`
The **only** permitted edit to the C++ safety floor. Current tiers (`loco_follow_bridge.cpp:96-97`):
`STALE_MS = 800`, `STALE_PREP_MS = 3000` (compile-time `static const`). The source comment itself notes
the tradeoff: *"too tight stutter-stops a healthy follow; too loose lengthens the runaway."* Once p99 is
known-good (post-P4.2), lower toward **~1.5× measured p99** (goal back toward **400 / 1000**).
- **Do NOT weaken anything else** in the floor. Rebuild via the P2.2 CMake.
- **Gate:** `VERIFY ON ROBOT` — zero stutter-stops on a healthy-follow clip at the tighter tiers.

## P4.5 — Batch clamp + demo-path cost gating  `SAFE`
- **Batch clamp:** clamp the ReID inference batch to the **pre-warmed set (1/2/4)** so a 5+-person frame
  can't stall on an on-the-fly TRT engine build (`identity.ReidEngine` — the batched embed path).
- **Demo cost-gate:** confirm `config/demo.yaml` leaves JPEG `emit_frame` streaming **off** and the Rerun
  sink **off** in the headless path (both cost CPU; already flag-gated — just verify the profile). Note:
  `demo.yaml` today doesn't set `stream`/`rerun` (both default off) — so the headless demo is already
  clean; add explicit `stream: false` / `rerun: false` for documentation if wanted.
- **Off-robot:** the batch clamp is code; the demo-profile check is inspection. Verify on robot.

## P4.6 — General persistent-fault → STAND escalator  `BEHAVIOR-CHANGE`
Today only ReID/overrun escalate (`_reid_watchdog`, `_overrun_streak`); a chronically-throwing hot loop
is caught only by the loose C++ bridge tier. The `run()` loop already catches per-frame exceptions
(`log("FRAME-ERR %s" % e)`) but only logs. **Add a consecutive-exception counter** there that escalates
to `_stand()` after **N** throws (mirror `_reid_watchdog`); reset on a clean frame.
- **Off-robot:** implement + add a **headless `selftest` variant** that injects a persistent throw into
  `_process_frame` and asserts the robot stands within N frames (extends `config_selftest`/`replay_eval`).
- **Gate:** the injected persistent throw stands within N frames (headless); real stand `VERIFY ON ROBOT`.

---

## Integration order (what I can start now vs robot-gated)
1. **Now, off-robot:** P4.6 (escalator + headless selftest — a clean BEHAVIOR-CHANGE with a headless
   gate), P4.5 batch clamp + demo-profile check, and the P4.2 **code** (TRT wiring + warmup, un-measured).
2. **Needs a `k1_follow.err`:** P4.1 baseline (send one and I do it off-robot).
3. **Needs the robot on + drive-capable:** P4.2/P4.3/P4.4 validation (p99 deltas, success on the labeled
   suite, no stutter at the tighter tiers) — these are the actual perf gains and must be measured on the Orin.

**Recommendation:** get me a recent `k1_follow.err` for the P4.1 baseline and I'll land P4.6 + the P4.2
code off-robot; then we do the measure-and-tune pass (P4.2→P4.4) in one robot session.
