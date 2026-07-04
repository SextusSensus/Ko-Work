# Rerun (rerun.io) integration — plan

**Status:** APPROVED (all 4 phases incl. LeRobot) + **BUILT 2026-07-03**. Designed by a 5-lens
workflow + adversarial synthesis, grounded in the live `follow_person_k1.py` / `K1Finder.ps1` /
`eval/replay_eval.py` and Rerun 0.33. Governed by the loaded skills: runtime-safety (loop budget),
autonomy-ops (offline staging), embodied-ai-advisor (tool-fit + honest thesis framing).

## Build status (2026-07-03) — verified against rerun-sdk **0.33.1**
- **Sink `k1_rerun.py` (shared by node + replay): BUILT + LOCALLY VALIDATED.** Inert-by-default;
  crash-safe (per-method no-op + a 20-fault auto-disable latch); log copies (no aliasing);
  embedded default blueprint. A real 84 KB `.rrd` reproducing the FREEZE + LUNGE shapes; the `.rrd`
  re-opens. `parse_track_line` unit-checked on every TRACK-line shape.
- **Phase 1 (offline `.rrd` from `replay_eval.py --rerun`): BUILT.** Wired via a dependency-light
  sink + the `mod.log` tap + the tapped `seed` box. Full `replay_eval --rerun` run needs the robot
  (`rclpy`/YOLO); the sink half is validated. Checked-in blueprint `eval/k1_follow.rbl`.
- **Phase 2 (live on-Orin `--rerun`): BUILT, compiles (py 3.13 + robot py 3.10 syntax).**
  **Byte-identical when off by construction** — every `_RR` call sits behind a single `if _RR.ok:`
  branch; images log ONLY on the cam-spin thread; scalars/boxes/state on the control loop after the
  TRACK log. `/diag/rr_track_ms` + `/diag/loop_ms` self-measurement + a `--rerun-overrun-frames`
  auto-disable backstop (RERUN-DISABLED-SLOW). **Dual-thread `frame_idx` timeline: EMPIRICALLY
  CONFIRMED thread-local** (two threads' cursors do not bleed). NOT yet run on the Orin — the
  loop-cost gate below is still the prerequisite before `--rerun` + `--drive`.
- **Phase 3 (app): BUILT, parses clean.** `Rerun` checkbox → `--rerun` (default-off), `Ensure-
  RerunWheels` (import-verified offline stage, warn-only), `k1_rerun.py` deployed with the node,
  `RERUN` added to the stderr whitelist, `Open .rrd` button (pull newest + launch viewer). **Still
  needs:** the aarch64/cp310 rerun-sdk wheel closure staged into a local `wheels/` dir (robot is
  offline); button pixel-placement unverified (app not launched).
- **Phase 4 (LeRobot export): BUILT + READ-PATH VALIDATED.** `eval/rrd_to_lerobot.py` reads a real
  `.rrd` (chunk-stream path) → `observation.state (T,7)` + `action (T,2)` + head_rgb → RAW LeRobot-v3
  layout (parquet + meta + mp4/PNG-fallback). Canonical `lerobot`-lib path written but unrun (lib
  absent). See `LEROBOT_EXPORT.md`. **Two corrections to this plan:** (a) there is NO native
  rerun→LeRobot exporter in 0.33; (b) `rr.save()` writes a *legacy footerless* `.rrd` so the read
  path is `RrdReader().stream().collect()`, not the `dataframe` API.

## On-robot validation (2026-07-04, robot @ 192.168.1.81, py3.10 aarch64 glibc2.35)
- **numpy BLOCKER found + resolved.** rerun **0.33 requires numpy>=2**; the robot is numpy **1.26.3**
  and the whole follow stack (onnxruntime 1.22, cv2 4.11, rclpy) is ABI-bound to numpy 1.x —
  installing numpy 2 would brick it. Pinned **rerun 0.23.1** (the newest release still `numpy>=1.23`;
  0.23.2+ jumped to `numpy>=2`), staged the aarch64/cp310 wheel closure offline (numpy EXCLUDED),
  `pip install --user`. Verified: rerun 0.23.1 imports, **numpy 1.26.3 untouched**, cv2/ort/rclpy/
  pyarrow all intact, node imports with the sink (default-off), robot sink writes a real `.rrd`. The
  sink is API-compatible with BOTH 0.23.1 and 0.33.1 (one `flush()` no-arg fix). App `Ensure-
  RerunWheels` pinned `==0.23.1 --user`.
- **Phase 1 on robot:** `replay_eval selftest` = SELFTEST-OK (node+YOLO+determinism); `run --rerun`
  → 13 MB offline `.rrd`.
- **Phase 2 loop-cost GATE: PASS in preview, at-the-edge during following, DRIVE deferred.**
  - *Preview (SEARCH):* baseline loop p50=73/p90=78/p99=82 ms; `--rerun` p50=77/p90=83/p99=97 ms —
    comfortably under the 100 ms budget, SLOW-LOOP=0.
  - *Active TRACK following (`--rerun`, images 1/10), measured 2026-07-04 on a 576-frame gesture-lock
    run:* the **direct Rerun control-loop cost `/diag/rr_track_ms` = p50 2.84 / p90 6.55 / p99 13.32
    ms** (max 25). Total `/diag/loop_ms` = p50 79.6 / p90 100.3 / p99 131.9 ms — i.e. the loop sits
    **right at budget (p90≈100 ms)** while following with Rerun on. Rerun is ~3 ms typical; its p99
    tail (~13 ms, the predicted batcher contention) is what pushes an already-heavy following loop over.
  - *Backstop verified:* with `--rerun-image-every-n 3 --rerun-overrun-frames 8`, `RERUN-DISABLED-SLOW`
    fired on the startup TRT-load/warmup stall (8 consec. over-budget) BEFORE the lock — gait safe,
    recording shed; with `-overrun-frames 30 --image-every-n 10` it rode through the whole follow.
  - Added an always-on `LOOP-MS` p50/p99 stderr pulse (tagged rerun on/off) + `eval/rrd_loop_stats.py`.
  - **DRIVE NOT cleared:** the drive loop is already 104–107 ms (audit); +Rerun's tail pushes it
    consistently over → the backstop sheds Rerun (gait safe, recording lost). For `--rerun`+`--drive`,
    use a HIGH `--rerun-image-every-n` (10+) and expect the backstop to arbitrate; measure in a
    supervised drive session before trusting a recording under drive.
- **Version compat CONFIRMED:** laptop rerun **0.33.1 reads the robot's 0.23.1 `.rrd`** (extracted
  `/diag/loop_ms`), so the "Open .rrd" viewer + Phase-4 export work on robot recordings.
- **`RerunLogger/` FOUND:** `/opt/booster/RerunLogger/bin/booster-rerun-logger` (a compiled SDK
  binary). Our node-side path is independent of it; a future optimization could piggyback it for
  images to cut the control-loop cost. Not yet probed for what it streams.
- **Gesture lock WORKS** (2026-07-04, `--gesture-debug`): first attempts didn't fire because the
  hand was raised while already in TRACK (`GDBG STATE-SKIP state=TRACK ... pose not run` — the
  acquisition pose only runs in SEARCH/REACQUIRE/PARKED, by design). Raising a hand during SEARCH
  locks cleanly (`holds={1:12} need=6 confirmed=[1]` → `LOCKED ... gesture handoff complete`, conf
  ~0.6). Captured a 576-frame following `.rrd` (range 1.6–1.7 m depth, real vx/vyaw) — the Phase-4
  motion episode.

- **Laptop-side closure (2026-07-04, robot charging):** the **native viewer opens the robot `.rrd`**
  (rerun-cli 0.33.1 at `anaconda3\Scripts\rerun.exe` — the exact path `Find-RerunViewer` probes, same
  `Start-Process` action as the app's "Open .rrd" button). **Phase-4 mp4 validated on real data**
  (`imageio-ffmpeg` → h264 544×448 @10fps, 247 real robot frames readable back). New
  `eval/rrd_loop_stats.py` = the reusable loop-cost-gate extractor (laptop 0.33+ only). Export
  validation also caught + fixed a sink bug: the FSM search state is `SEARCH_MARKER`, not `SEARCH`
  (was logging -1 on the `/fsm/state_id` series). Wheel closure gitignored; regeneration + the
  numpy pin documented in `wheels/README.md`. Work committed on `rerun-observability` (88c043e).

**Remaining (robot-gated):** a locked following `.rrd` (range/vx/vyaw) for a real Phase-4 episode +
the TRACK loop-cost — retry gesture lock WITH `--gesture-debug` (it did not fire in the first capstone
attempt; robot went offline before the debug re-run); the `--rerun`+`--drive` gate in a supervised
drive session; probe what `booster-rerun-logger` streams; app GUI click-test (tick Rerun, Open .rrd);
arm-validation.

## Why
Twice this session a real bug was **misdiagnosed from a text log** — the relock **lunge** (depth
glitch 9.15 m→0.73 m → max forward vx) and the **frozen follow** (a 0.6 m geofence stood the robot
130+ frames while the operator was 0.36 m away). Both are *obvious* on a scrubbed Rerun timeline:
`/follow/range` vs the 0.6/1.2 reference lines, `/cmd/vx`, `/cmd/forbid_forward`, `/follow/range_source`,
the FSM state, all under one scrubber. Rerun turns *"grep a 400-line `.err`"* into *"scrub and see."*
That is the whole near-term value.

## Recommendation — offline-first, live-gated-on-measurement
The 10 Hz control loop already runs **104–107 ms against its 100 ms `SLOW-LOOP` budget** with zero
headroom (`follow_person_k1.py:2437`). So **no un-measured compute touches the driving loop.** Phase 1
delivers ~90% of the debugging value with *none* of that risk; nothing touches the loop until Phase 2
proves the cost empirically.

## ⚠️ Verify first (could reshape Phases 2–4)
The **Booster SDK ships a `RerunLogger/` module** (`k1_tree.txt:315`, alongside `RobotCore`/
`ServerPerception`). Rerun is a *supported path* on this robot. **Before writing any image-logging
code, check on-robot what `RerunLogger` already streams (RGB/depth/pose/joints?) and whether it runs.**
If it already publishes the camera images, we **piggyback it for images and only ADD our follow
scalars** — far cheaper for the loop and less code. This is the single highest-leverage unknown.

---

## Phases

### Phase 1 — Offline `.rrd` from the replay harness (laptop-only, ZERO robot risk) — effort M
Attach a Rerun sink to the existing headless `replay_eval.py` so a recorded clip → a scrubbable `.rrd`
on the laptop. No code on the driving loop.
- `--rerun` path **inside `replay_eval.py replay()`** (L94–119): `rr.init('k1_follow')` + `rr.save(path)`
  once; `rr.set_time` on a `frame_idx` sequence timeline = the loop counter.
- The node already routes all telemetry through `mod.log`, which replay monkeypatches (L105). **Wrap
  that tap** to also parse the `TRACK` line (`follow_person_k1.py:3511`) into `rr.Scalars` — *zero node
  change*; the `.rrd` is built from data already crossing that line.
- Log the schema below (minimum: `/follow/range` + 0.6/1.2 lines, `/cmd/vx`, `/cmd/vyaw`,
  `/cmd/forbid_forward`, `/follow/range_source`, `/fsm/state`, `/events`, RGB `rr.Image` + target `rr.Boxes2D`).
- Install the Rerun 0.33 **native viewer on the laptop** (has internet); pin the exe path (needed for
  the Phase-3 "Open last .rrd" button). Fallback `python -m rerun <file>`.
- Check in a default **blueprint (`.rbl`)**: left = RGB+boxes over depth; center = stacked
  `/follow/range`(+refs), `/cmd/vx`, `/cmd/forbid_forward`, `/follow/range_source` under one scrubber;
  right = `/fsm/state` + `/events` + `/health/depth_fps`.
- **Honest limit:** `StubNode.latest_depth` returns `None` (`replay_eval.py:49`) → `rsrc` is forced to
  `bboxH`, so the offline `.rrd` **can reproduce the FREEZE but not the depth-glitch LUNGE** until a
  depth-bearing bag + an extended StubNode exist (Phase-2 dependency).

### Phase 2 — Live on-Orin logging, default-off `--rerun`, gated on a measured loop probe — effort L
Crash-safe node logging with a **byte-identical default-off path**, images kept **entirely off the
10 Hz loop**, and an auto-disabling loop-cost gate.
- Module-level `_RR` wrapper (mirror the `_STREAM` global) with an `enabled` gate + `set_frame/
  log_scalar/log_boxes/log_text/log_image`, **each in `try/except` that no-ops on any exception**.
  `init_rerun(args)` does a **lazy `import rerun` in try/except** → absent wheel/import failure =
  disabled + warn, node runs (mirrors the `GestureTrigger` degrade idiom, `:368`).
- Flags near `:3927` (alongside `--stream`): `--rerun` (store_true default False),
  `--rerun-mode {save,connect}` (default `save`), `--rerun-addr`, `--rerun-image-every-n` (default 3).
  Init `_RR` in `main()` where `_STREAM` is flipped (`:4304`), before `run()`.
- **Control-loop site (cheap scalars/boxes/text ONLY)** in `_track` after L3376 and L3507: the truthful
  post-clamp values (`self._prev_vx`/`self._prev_vyaw`, ~`:1972`), `forbid_forward` (`:3486`), range,
  rsrc, bearing, cost/sim/conf; `log_boxes(best['box'], track_id)`; `log_text(self.state)`; mirror the
  existing `RANGE-GATE`/`DEPTH-STARVED`/`AUTO-RELOCK`/`GALLERY` log() calls to `/events`.
  **Never call `rr.Image` in `_track`/`_process_frame`.**
- **Image site on the `cam-spin` sensor thread ONLY** — `_cb` (after `to_bgr`, inside the existing try
  at `:1567`) and `_depth_cb` (`:1590`), gated on `self._seq % image_every_n == 0`, logging a **COPY**
  (never hand `self._depth` by reference — another thread reads it under `_depth_lock`).
- **Dual timeline:** `frame_idx` sequence keyed to `self._seq` (`:1586`) on **both** threads + wallclock
  — `self._seq` is the only value tying a cam-spin image to its control-loop decision.
- Default `--rerun-mode save` → timestamped `.rrd` on `/home/booster/rerun/` (200 ms flush, crash-safe).
  **Bound growth**: image decimation + timestamped rotation + a size/count budget + prune.
- **Loop-safety gate (MANDATORY before any `--drive` with `--rerun`)** — see below.

### Phase 3 — App/operator surface (`K1Finder.ps1`) — effort M
- `Ensure-RerunWheels($ip)` — a **size-verified clone of `Ensure-ReidModel` (`:136`)**: scp
  `rerun_sdk-0.33-cp310-abi3-manylinux_2_28_aarch64.whl` + `pyarrow`(aarch64 cp310) + `typing_extensions`
  to `/home/booster/wheels/`, kill-on-timeout, `stat -c%s` size-verify, then one
  `pip install --no-index --find-links /home/booster/wheels rerun-sdk`. **Success = remote
  `python3 -c "import rerun"` returns a version**, not file presence.
- **Install into the python3 `run_follow.sh` actually launches** (system vs `/opt/booster` venv) — the
  top "installed but ImportError at launch" failure. Verify first.
- Default-OFF Tracker-tab **`Rerun` checkbox** → `Get-TrackExtraArgs (:783)` → launch string (`:1049`).
  Stage failure → WARN + "start anyway" (a follow is **never** blocked by Rerun).
- Add any `RERUN-*` status prefix to the node-stderr **whitelist** (the audit's #1 observability defect
  — unlisted prefixes are silently dropped) **or** keep Rerun fully on its side-channel — decide explicitly.
- **"Pull recording"** button (clone the Robot-Files scp-DOWN pattern `:1631`) → `%TEMP%\k1finder`; an
  **"Open last .rrd"** button `Start-Process`es the pinned viewer exe (no `Invoke-Item` precedent today).
- *Optional* live cockpit: `--rerun-mode connect` to the laptop viewer over LAN (8 ms flush) — opt-in for
  **supervised DRIVE only**, never default, **forbidden untethered**.

### Phase 4 — LeRobot capture tier — **pipeline-proof ONLY** (honestly scoped) — effort M
- After a depth-valid `.rrd` exists, run Rerun's built-in `.rrd → LeRobot v3` export.
- Episode: `observation.images.head_rgb` (MP4), `head_depth` (MP4 — **lossy**, the `.rrd` stays the
  artifact of record), `observation.state=[range, bearing, rsrc_is_depth, track_id, anchor_sim, conf,
  depth_fps, state]`, `action=[vx, vyaw]` (`vy≡0`, 2-DOF).
- **Do not build this as "capture training data"** — see thesis-fit. Designate Rerun as the **QA/labeling
  viewer for the future dual-POV + LiDAR + skeletal rig** (LiDAR→`Points3D`, per-POV `Pinhole`/`Transform3D`,
  skeletal→lines); the follow is the low-stakes place to build that muscle.

---

## Rerun schema (entity → archetype → source → thread)

| Entity | Archetype | Source (node) | Thread |
|---|---|---|---|
| `/camera/rgb` (+ child `/rgb` Pinhole) | `rr.Image` + `rr.Pinhole` (hfov) | the frame `emit_frame` draws | **cam-spin** (decimated) |
| `/camera/depth` | `rr.DepthImage` (m) | `latest_depth()`/`self._depth` | **cam-spin** (decimated) |
| `/camera/rgb/target` | `rr.Boxes2D` (+id label) | `best['box']`/`seed.box`, `seed.track_id` | control-loop |
| `/follow/range` (+ 0.6/1.2/max lines) | `rr.Scalars` | `self._viz_range` (L3376) | control-loop |
| `/follow/range_source` (2=depth/1=bboxH/0) | `rr.Scalars` step | `rsrc` (L3373) — the bug hinge | control-loop |
| `/follow/bearing` | `rr.Scalars` | `self._viz_bearing` (L3375) | control-loop |
| `/cmd/vx` (+ clamp band) | `rr.Scalars` | `self._prev_vx` (truthful sent, ~L1972) | control-loop |
| `/cmd/vyaw` (+ clamp band) | `rr.Scalars` | `self._prev_vyaw` | control-loop |
| `/cmd/forbid_forward` (0/1) | `rr.Scalars` step | the L3486 boolean — **most diagnostic new signal** | control-loop |
| `/fsm/state`, `/fsm/substate` | `rr.SeriesLines(str)`/`TextLog` | `self.state`, `seed.track_state` | control-loop |
| `/health/depth_fps`, `/depth_state`, `/depth_starved` | `rr.Scalars`/str | `depth_health()`, `_depth_starved` | control-loop |
| `/reid/anchor_sim`, `/gallery_sim`, `/dist_sim` | `rr.Scalars` (+floor) | `a_s`/`g_s`/`d_s` (L3443) | control-loop |
| `/events` | `rr.TextLog` (levels) | mirror each `log()`: AUTO-RELOCK, RANGE-GATE, DEPTH-STARVED, GALLERY-ADMIT, DRIVE-ABORT, SLOW-LOOP | control-loop |
| `/world/target` (+1.2 m ring, optional) | `rr.Points3D` | `x=rng·cos(bearing), y=rng·sin(bearing)` | control-loop |
| timeline (frame_idx seq + wallclock) | `rr.set_time` | `self._seq` (L1586) — cross-thread key | startup |

## Loop-safety gate (mandatory before any `--drive` with `--rerun`)
1. Confirm rerun-sdk 0.33 aarch64/cp310 **imports on this Orin** in deploy power mode (`nvpmodel -m 0`,
   `jetson_clocks`).
2. `/diag/rr_ms` probe (`time.monotonic()` delta around the loop's `_RR` scalar calls, reusing the
   `GESTURE-MS` instrument ~`:379`). Capture **p50/p99 in PREVIEW**, separately for scalars-only vs
   scalars+decimated-images.
3. **PASS/FAIL:** with `--rerun` on, added control-loop cost p99 leaves total loop dt comfortably < 100 ms
   **and** the `SLOW-LOOP`-over-budget streak does **not** increase vs the `--rerun`-off baseline over a
   sustained run. Fail any → `--rerun` stays **preview-only, never `--drive`.**
4. **Auto-disable backstop (ships with the code):** an independent overrun counter mirroring the gesture
   `on_overrun` (`:2455`) — N consecutive over-budget frames with `_RR` on → auto-disable `_RR` before the
   C++ staleness watchdog must safe.
5. **Byte-identical proof:** `replay_eval.py compare --flags-a "" --flags-b "--rerun ..."` (`:190`) must
   return `COMPARE-OK` — identical decision streams (Rerun is side-channel only).
6. Don't trust "scalars are free" — they take the Python-SDK batcher lock the cam-spin image path also
   contends. The measurement decides whether images+scalars share one `RecordingStream` or split.

## Offline deploy
On an internet PC: `pip download --platform manylinux_2_28_aarch64 --python-version 310 --only-binary=:all:
rerun-sdk==0.33 pyarrow typing_extensions` for the full closure (numpy 1.26.3 already on robot; **pyarrow
aarch64 is the risky wheel**). Stage via `Ensure-RerunWheels` (size-verified). `.rrd` → laptop via a
"Pull recording" button; viewer installed on the laptop, pinned exe for "Open last .rrd". **Rerun is never
a safety dependency** — a missing wheel degrades to "no recording" and the follow proceeds (same contract
as the OSNet histogram fallback).

## Thesis-fit (honest — embodied-ai-advisor)
- **TRUE:** a first-class **debugging/observability** win; the two misdiagnosed bugs are the concrete
  justification.
- **SELLABLE:** proves the `.rrd → LeRobot v3` **capture plumbing**, de-risking the real rig's ingest path;
  positions Rerun as the QA/labeling viewer for the future moat data.
- **CAVEAT (diligence-killer — in writing):** the follow's episode is **mono head RGB + flaky head depth
  + a 2-DOF twist from a hand-written P-controller**. It is **not** the dual-POV+LiDAR+skeletal thesis data
  on *any* axis. Do **not** pitch the LeRobot export as "we now capture training data" — one DD question
  collapses it. Rerun here is an **internal field-debug tool**; label the LeRobot dataset internal-tooling.

## Open decisions (yours)
1. **Do you want the LeRobot tier (Phase 4) at all**, or is the `.rrd` scrub-and-see debugging the whole
   near-term deliverable?
2. Confirm **offline-first sequencing** (Phase 1 → gate → Phase 2), i.e. no live node logging until the
   Orin probe passes.
3. **Check `RerunLogger/` on-robot first** (could let us piggyback SDK images) — do this before Phase 2.

## Key risks
Control-loop contention (central; mitigated by off-loop images + gate + auto-disable) · Rerun fault
propagation (per-method no-op) · buffer aliasing (log copies; verify with compare) · offline `.rrd` can't
reproduce the lunge yet (needs depth bag) · Orin disk (rotation+budget+prune) · offline pip closure
(pyarrow + correct python3) · **thesis oversell** (the biggest non-technical risk).
