# UNTETHERED OUTDOOR K1 FOLLOW — Operator Pre-Flight Gate

**Status: DO NOT RUN UNTETHERED. This document is the gate, not the green light.**
Scoped 2026-06-26 (design + 4-lens adversarial-safety workflow; all four lenses returned RETHINK). Grounded against the live `follow_person_k1.py`, `loco_follow_bridge.cpp`, `K1Finder.ps1` + the [perception-expansion-plan].

> **UPDATE 2026-07-02 (P2 #12):** the software heartbeat layer of blocker (2) now EXISTS and has a
> UI path — the bridge's `K1_REQUIRE_HB`/`K1_HB_FILE` mtime deadman (zero @400ms → kPrepare @1.5s,
> ANDed with the v-gate) + the node's `--require-heartbeat` gate in `_drive_vel`, wired to the
> Tracker tab's red **`Deadman HB`** checkbox, which also starts a byte-driven relay (remote
> touches happen only on bytes received from the app, so mtime freshness == live connectivity).
> This is a **laptop-tethered-equivalent** deadman (the laptop+WiFi is the operator link), NOT the
> passive-safe fob this doc requires for untethered. **The gate stands.**
>
> **UPDATE 2026-09-11 (council audit):** re-verified against live code. Software HB is real and
> fail-closed when armed. The body below no longer claims “the deadman does not exist.” Remaining
> blockers (HW e-stop, no-auto-resume on HB restore, launch-guard, passive fob, depth-DOWN → hard
> stand, session-cap envelope) are still open. **Untethered remains FORBIDDEN.**

Going untethered does not *remove* the need for a deadman — it **removes the SSH-link deadman you
currently lean on** and forces a replacement that survives a dead laptop. Today’s software HB still
depends on the laptop+WiFi path. This doc is the safety gate that must be cleared first.

## Why untethered is currently FORBIDDEN (verified against live code, 2026-09-11)

- **Software HB exists, but it is not an untethered deadman.** Bridge + node both gate on a fresh
  operator heartbeat when armed (`K1_REQUIRE_HB` / `--require-heartbeat`). Unticking Deadman HB in
  the UI does **not** pass `--allow-untethered-unsafe`; drive refuses. That is correct fail-closed
  behavior — and it still requires the laptop relay. A passive-safe fob / Orin-independent kill is
  **not** built.
- **Auto-resume on HB restore / post-kPrepare is still a hole.** Bridge re-enters walking on a fresh
  `v` after prep; node `HB-OK` resumes velocity without a deliberate re-ARM. Tethered (operator holds
  SIGINT) this is tolerable; untethered it becomes stop→auto-walk→chase. Must stay disarmed until
  explicit ARM.
- **Hardware e-stop unverified.** The only in-repo “hardware” stop is software `ChangeMode(kDamping)`
  — which depends on Orin+SDK+WiFi being alive AND makes the robot go *limp* (a fall). No demonstrated
  Orin-independent motor kill.
- **Session caps are far above “tens of seconds.”** App field-night cap is 300s; launcher default
  without the env override is much higher. Untethered launch-guard (refuse `--drive` without a tight
  geofence + short cap) is not implemented.
- **Depth-DOWN keeps yawing.** Depth-invalid / depth-starved forces `vx=0` but `vyaw` still streams →
  blind yawing pursuit (TURN-ONLY). Must promote to a hard stand untethered.
- **No fall detection.** No IMU/tilt/fall trigger anywhere. A stumble with fresh v+hb keeps being
  commanded.

## Failure-mode → safe-state (the two LOUD gaps)

Most failures reach stop once **Layer A (heartbeat)** is armed on the laptop tether. **Two do NOT,
ever, by software:** (a) a **fall/stumble** with fresh v+hb, and (b) a **wedged Orin / DDS stall**.
Both depend entirely on a verified **hardware e-stop** reachable by a human → which caps max
speed/standoff to “operator can catch it.”

## Architecture (deadman-first)

Robot's own offline WiFi AP (hostapd/WPA2, **no internet, no Starlink**) → operator carries a
**passive-safe BLE/RC hold-to-run fob** (primary heartbeat; dropped/dead/out-of-range = no heartbeat)
with the **phone as backup** (one giant STOP + hold-to-run + read-only STATUS) → on-robot **relay
process** (NOT in the 10 Hz loop): heartbeat → `hb` on a **dedicated bridge fd** (own thread); enum
tokens (STOP/HOLD/PARK/ARM/STATUS, allow-list) → the Phase-1 `/tmp/k1_cmd` drain → existing FSM +
3-layer clamps + bridge watchdog. **The phone/fob NEVER emits velocity** — only the existing control
law does, behind the existing clamps.

Heartbeat is ANDed with the v-gate in **both** the bridge watchdog **and** `_drive_vel` (a hung
heartbeat-reader with a fresh v must still stop). On HB restore: clear stale, **stay DISARMED** until
a fresh ARM — do not reuse the bridge v-recovery edge.

## Launch & arm without a PC

- **Boot-disarmed:** systemd launches the node in `--preview` (no `--drive` ⇒ no bridge ever spawned ⇒
  structurally cannot walk). A crash/relaunch stays disarmed.
- **ARM is a separate deliberate human act** (fob press-and-hold / phone slide-to-arm) requiring a
  fresh heartbeat — never on boot, never on marker alone.
- **Marker still seeds identity, after arming.** Reuse the bring-up chain (ping→frame→prep→walk)
  verbatim; surface OK/ABORT to the phone STATUS.
- **No-auto-resume:** after any safe-stop, a returning heartbeat clears stale but leaves drive
  DISARMED until a fresh ARM.

## Outdoor operating envelope (first trial — ALL mandatory, enforced in code)

Flat/firm/level/dry ground (empty field/lot) · open, **solo, no bystanders in the geofence** ·
**slow** (vx ~0.06–0.08 m/s, well under the 0.30 clamp) · tight depth-validated geofence ~2.5–3.0 m,
min-range ≥1.0 m, inside measured AP coverage · session cap **tens of seconds** · diffuse daylight,
sun behind/above, never near the sun · **operator walking backward, hardware e-stop in hand, arm's
reach.** Treat as "tethered-by-proximity even though wireless."

**NOT safe yet** (each unlocks only when its sensor/behavior prerequisite ships): sidewalks/curbs/
steps/drop-offs (no downward sensing), slopes/uneven/soft (no IMU), traffic, crowds,
thin/glass/overhead/side obstacles (single forward cone), night/low-sun/backlight. The Phase-2 reflex
is a **forward-clearance reflex, NOT obstacle avoidance** — blind to sides, drop-offs, and useless
while depth is down.

## Roadmap (sits ON the perception plan)

- **P0 — perception Phase 0 (depth-safety) finished + verified on-robot**, plus depth-DOWN → hard
  `_stand` in the untethered profile; run the depth DIAGNOSE outdoors in sunlight.
- **P1 — perception Phase 1 command spine** (enum + `/tmp/k1_cmd` drain) landed so STOP/ARM have an
  on-robot consumer.
- **P2 — finish the deadman (BLOCKERS):** (1) **verify the hardware e-stop** on the physical robot —
  if none independent of the Orin, untethered is FORBIDDEN above arm's-reach slow walk; (2) no-auto-
  resume on HB restore; (3) mandatory untethered launch-guard (refuse `--drive` without a small
  geofence + tight cap); (4) prefer a passive-safe BLE/RC fob as primary (laptop HB stays as backup).
- **P3 — AP + heartbeat integration + bench failure-injection matrix** (verify EACH failure reaches
  zero+kPrepare, robot MOVING; measure AP coverage + hb latency to SET thresholds — do not guess).
- **P4 — one tiny supervised untethered trial** in the envelope above.
- **P5 — widen the envelope only as sensing ships** (no row unlocked by operator confidence).

## The gate — untethered is FORBIDDEN until ALL are true

1. Hardware e-stop verified independent of the Orin; its reach documented as a hard speed/standoff cap.
2. Heartbeat path survives a dead laptop (passive fob or equivalent); thresholds field-measured.
3. No-auto-resume built; re-arm is a deliberate human gesture.
4. Geofence + tight session cap made mandatory by a launch-refusal guard.
5. Depth-DOWN promoted to a hard stop untethered.
6. Full failure-injection matrix passed on a stand, then at slow speed.
7. Envelope enforced as a launch-path refusal, not runbook prose.

As of 2026-09-11: software laptop-tethered HB is built and fail-closed; auto-rewalk on HB restore is
ungated; hardware e-stop is unverified; fall + wedged-Orin reach no software stop. **Do not run
untethered.**
