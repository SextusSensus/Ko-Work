# Phone control surface for the OSNet follow — design + hard safety findings

**Date:** 2026-06-26 (multi-agent workflow: map → research → design panel → judge → adversarial verify)
**Question:** Operator has a PHONE (no gamepad). Use it to run the OSNet follow. Must work OFFLINE.
**Verdict:** Buildable and fully offline-compatible (everything is robot-hosted; no cloud). BUT the
phone is a SOFTWARE deadman + convenience UI — it is **not** and can never be the safety backstop. First
operation stays **tethered-by-proximity** (arm's reach, wired e-stop in hand). See [[k1-hardware-backstop]],
[[k1-offline-capable]], and the existing UNTETHERED_FOLLOW gate.

---

## Recommended architecture (deadman-first; judged safest of 3, 8.5/10)

- **Connect:** phone joins the robot's own WiFi AP (offline, no internet uplink) and opens one web page
  **served by the robot** — no app install, no cloud. Offline-clean by construction. NEVER use the
  RoboticsCenter `k1_agent.py` bridge (it's a WebSocket to the cloud).
- **Video:** MJPEG tap (`multipart/x-mixed-replace`) of the frame the node *already* annotates +
  encodes for `--stream` (`_stream_frame` follow_person_k1.py:1542; `emit_frame` K1F1 framing :147).
  A bare `<img src=/video.mjpg>` renders it on any phone browser — zero phone-side decoder. **Reuse the
  already-encoded JPEG; do not re-encode** (the Jetson loop is SLOW-LOOP-prone). Keep FPS/quality low.
- **Control = INTENT only, never velocity.** Phone sends an allow-listed enum (ARM / DISARM / STOP /
  STANDOFF± / SPEED± / STATUS) plus a high-rate heartbeat. The robot's existing control law owns all
  motion, behind the **sacred 3-layer clamps** (args → python HARD_VX/VYAW ceilings → C++ `clampf`
  bridge floor :61-68). No joystick on the phone.
- **Deadman = hold-to-run heartbeat.** While a finger is held the phone pulses `hb` ~5-10 Hz. Absence
  (finger up, screen lock, app background, link drop, out of range) → robot zeroes velocity then
  `ChangeMode(kPrepare)` (stable stand, never limp). Enforced in THREE independent places: a 2nd
  staleness tier on the bridge's own ~50 Hz watchdog thread (clone of loco_follow_bridge.cpp:166-182),
  the bridge `v`-handler clamping to zero on stale hb, and the single python velocity choke
  `_drive_vel` (:1302-1304).
- **Latched software e-stop, NO auto-resume.** STOP latches; a returning heartbeat clears "stale" for
  telemetry but leaves DRIVE disarmed until a deliberate human re-arm.

## NON-NEGOTIABLE safety fixes (adversarial pass found these break a naive build)

These are not polish — without them a flaky WiFi link or a re-seen marker makes the robot walk at a
person with no human in the loop. All must be in v1:

1. **Gate ARM on the MODE-CHANGE edge, not just `_drive_vel`.** The stand→walk transition is
   `_resume_walk()` (:1327-1349, fired from `_process_frame` :2100-2103) and bring-up `walk()`, which
   call `bridge.walk()` DIRECTLY — bypassing the `_drive_vel` heartbeat/e-stop gate. `_resume_walk`/
   `walk()` must refuse unless `_armed AND _hb_fresh() AND not _estop_latched`.
2. **Marker re-seed must NOT auto-resume walking.** `_try_seed` from S_REACQUIRE/S_PARKED
   (:1710-1713, :1740-1743) currently returns to TRACK → `_resume_walk` → walks again with NO human
   re-arm. That violates "never arm on marker alone." A re-seed may restore identity but must leave
   DRIVE disarmed pending a deliberate ARM.
3. **Kill/guard the bridge auto-rewalk edge for this profile.** loco_follow_bridge.cpp:274-276 re-enters
   `kWalking` on the first fresh `v` after a prep. On a flickering link this becomes
   **stop → auto-walk → chase** on every dropout. Heartbeat-loss must LATCH disarm (a new
   `g_hb_latched`) that blocks `kWalking` from the `v` path until an explicit re-arm token (not a `v`,
   not an `hb`).
4. **Make the heartbeat sound over a real network.** Embed a monotonic **send-sequence + phone
   timestamp** in every `hb`; compute freshness from the embedded timestamp, **not receive time**, and
   reject a burst whose sequence/spacing implies buffering. Carry `hb` over a **datagram**
   (UDP / WebRTC datachannel), never plain TCP — else a TCP head-of-line stall flushes 5 s of stale
   heartbeats as one "fresh" burst, and a background-throttled JS timer keeps pulsing after the screen
   locks. Both defeat a naive deadman. Also stop the pulse on
   `pointerup/pointerleave/blur/visibilitychange/pagehide`.
5. **Per-client auth.** A token bound at ARM (or single-client socket bind). "Bound to the AP subnet"
   is NOT authentication — any device on the shared PSK could POST `/hb` or `/rearm`.
6. **Single-driver on the loco-service side.** The bridge itself must acquire a single-owner lock /
   DDS-participant guard at `Init` — a shell lockfile in run_follow.sh can't stop a second bridge
   subprocess or a stray `run_loco.sh` client from writing MoveCommand to the same service.
7. **Boot-disarmed + launch-refusal.** Autostart in `--preview` (systemd) so a crash/relaunch
   structurally cannot walk. `start_drive_chain` (:1352) must REFUSE `--drive` without a mandatory
   small geofence (`--max-follow-range` 2.5-3.0, `--min-safe-range` ≥1.0) AND a tight `--max-seconds`
   (tens of s) AND low `--vx-max` (~0.06-0.08). Defaults today disable both fences. Promote depth-DOWN
   to a hard stand (line 2173 currently keeps yawing blind during a sunlight depth blackout).

## The hard residual constraint (irreducible — all 3 verifiers agreed)

**There is no Orin-independent wireless hardware kill.** The phone STOP, the heartbeat deadman, the
e-stop latch, the ARM gate, and the single-driver mutex ALL live on Orin + Booster SDK + WiFi + the
python loop. A **fall/stumble with a fresh heartbeat, a wedged Orin, or a DDS stall reaches NO software
stop** — the loco service holds the last MoveCommand and the in-process watchdog dies with the process.
There is also no IMU/fall detection anywhere (line 339: "no IMU fusion here").

Therefore, regardless of how clean the phone UI is, first operation is bounded to where a human with the
**wired e-stop in hand can physically catch the robot**: arm's reach, ~0.06-0.08 m/s, depth-validated
geofence ~2.5-3.0 m, tens-of-seconds session cap, solo, no bystanders, flat/dry/level, diffuse daylight.
The phone must show a persistent **"NO WIRELESS HARDWARE KILL — wired e-stop in hand"** banner whenever
DRIVE is armed. Untethered above that stays FORBIDDEN until a verified Orin-independent motor kill
exists AND the moving-robot failure-injection matrix (flicker-rearm, background-pulse, HOL-flush,
marker-reseed) passes. **No software deadman lifts this.**

## Build phasing

- **Phase A (~0.5-1 day):** robot-hosted page + MJPEG video + enum control (STOP/STANDOFF/SPEED/STATUS),
  **preview only, no drive.** Validates the whole spine cheaply and offline.
- **Phase B (~1-2 days):** the heartbeat deadman + ALL seven safety fixes above; bench
  failure-injection matrix proving every loss reaches zero+kPrepare *while moving* with a latch that
  does not auto-clear; **field-measure** HB_STALE/HB_STALE_PREP above real loop+AP jitter from
  `k1_follow.err` (the live bridge STALE_PREP_MS is 3000 ms — far too loose to be the operator deadman).
- **Phase C (optional):** aiortc WebRTC for lower-latency/longer-range video, MJPEG kept as fallback.
  (The robot's vendor RTC stack — `.bytertc`/RTCCli/booster_rpc_bridge — is closed binary with no API,
  so it can't be reused without a reverse-engineering spike.)

## Loose end to pin
Robot IP/subnet drift: memory says `192.168.10.102`, K1Finder.ps1 default is `192.168.1.81`, README
cites `.102`. With the robot running its own AP, pin and test the phone's reachability path so
STOP/STATUS can't target the wrong host.
