# UNTETHERED OUTDOOR K1 FOLLOW — Operator Pre-Flight Gate

**Status: DO NOT RUN UNTETHERED. This document is the gate, not the green light.**
Scoped 2026-06-26 (design + 4-lens adversarial-safety workflow; all four lenses returned RETHINK). Grounded against the live `follow_person_k1.py`, `loco_follow_bridge.cpp`, `K1Finder.ps1` + the [perception-expansion-plan].

Going untethered does not *remove* the need for a deadman — it **removes the deadman you currently have** (the SSH link) and forces you to rebuild it. Today nothing replaces it. This doc is the safety gate that must be cleared first.

## Why untethered is currently FORBIDDEN (verified against live code)
- **The deadman does not exist yet.** The bridge has ONE staleness key (`g_last_v_ms`/`g_v_active`); the command switch knows only `v/stop/ping/prep/walk/damp/quit`. **No `hb`, no operator-presence variable.** A dead phone with a healthy `v` stream keeps the robot walking.
- **An auto-resume edge becomes a hole untethered.** Bridge re-enters `kWalking` on the first fresh `v` after a stale-prep (the standby-recovery fix — correct *tethered*, where the operator still holds SIGINT kill-authority). Untethered, with only a flickering heartbeat, it becomes stop→auto-walk→chase. Must be gated behind a deliberate re-arm.
- **Hardware e-stop unverified.** The only in-repo "hardware" stop is software `ChangeMode(kDamping)` — which depends on Orin+SDK+WiFi being alive AND makes the robot go *limp* (a fall). No demonstrated Orin-independent motor kill.
- **Geofence + session cap fence nothing by default.** `--max-follow-range` default 0 (disabled); `--max-seconds` default 120 (far too long untethered); geofence acts only on validated depth, so a sunlight depth blackout freezes the latch.
- **Depth-down keeps yawing.** Depth-invalid forces `vx=0` but `vyaw` still streams → blind yawing pursuit. Must promote to a hard stand untethered.
- **No fall detection.** No IMU/tilt/fall trigger anywhere. A stumble with fresh v keeps being commanded.

## Failure-mode → safe-state (the two LOUD gaps)
Most failures reach stop once **Layer A (heartbeat)** is built. **Two do NOT, ever, by software:** (a) a **fall/stumble** with fresh v+hb, and (b) a **wedged Orin / DDS stall**. Both depend entirely on a verified **hardware e-stop** reachable by a human → which caps max speed/standoff to "operator can catch it."

## Architecture (deadman-first)
Robot's own offline WiFi AP (hostapd/WPA2, **no internet, no Starlink**) → operator carries a **passive-safe BLE/RC hold-to-run fob** (primary heartbeat; dropped/dead/out-of-range = no heartbeat) with the **phone as backup** (one giant STOP + hold-to-run + read-only STATUS) → on-robot **relay process** (NOT in the 10 Hz loop): heartbeat → new `hb` on a **dedicated bridge fd** (own thread); enum tokens (STOP/HOLD/PARK/ARM/STATUS, allow-list) → the Phase-1 `/tmp/k1_cmd` drain → existing FSM + 3-layer clamps + bridge watchdog. **The phone/fob NEVER emits velocity** — only the existing control law does, behind the existing clamps.

Heartbeat is ANDed with the v-gate in **both** the bridge watchdog **and** `_drive_vel` (a hung heartbeat-reader with a fresh v must still stop).

## Launch & arm without a PC
- **Boot-disarmed:** systemd launches the node in `--preview` (no `--drive` ⇒ no bridge ever spawned ⇒ structurally cannot walk). A crash/relaunch stays disarmed.
- **ARM is a separate deliberate human act** (fob press-and-hold / phone slide-to-arm) requiring a fresh heartbeat — never on boot, never on marker alone.
- **Marker still seeds identity, after arming.** Reuse the bring-up chain (ping→frame→prep→walk) verbatim; surface OK/ABORT to the phone STATUS.
- **No-auto-resume:** after any safe-stop, a returning heartbeat clears stale but leaves drive DISARMED until a fresh ARM. Do NOT reuse the bridge v-recovery edge for hb.

## Outdoor operating envelope (first trial — ALL mandatory, enforced in code)
Flat/firm/level/dry ground (empty field/lot) · open, **solo, no bystanders in the geofence** · **slow** (vx ~0.06–0.08 m/s, well under the 0.30 clamp) · tight depth-validated geofence ~2.5–3.0 m, min-range ≥1.0 m, inside measured AP coverage · session cap tens of seconds · diffuse daylight, sun behind/above, never near the sun · **operator walking backward, hardware e-stop in hand, arm's reach.** Treat as "tethered-by-proximity even though wireless."

**NOT safe yet** (each unlocks only when its sensor/behavior prerequisite ships): sidewalks/curbs/steps/drop-offs (no downward sensing), slopes/uneven/soft (no IMU), traffic, crowds, thin/glass/overhead/side obstacles (single forward cone), night/low-sun/backlight. The Phase-2 reflex is a **forward-clearance reflex, NOT obstacle avoidance** — blind to sides, drop-offs, and useless while depth is down.

## Roadmap (sits ON the perception plan)
- **P0 — perception Phase 0 (depth-safety) finished + verified on-robot**, plus depth-DOWN → hard `_stand` in the untethered profile; run the depth DIAGNOSE outdoors in sunlight.
- **P1 — perception Phase 1 command spine** (enum + `/tmp/k1_cmd` drain) landed so STOP/ARM have an on-robot consumer.
- **P2 — build the deadman (BLOCKERS):** (1) **verify the hardware e-stop** on the physical robot — if none independent of the Orin, untethered is FORBIDDEN above arm's-reach slow walk; (2) heartbeat gate (`hb` + second staleness key on the watchdog thread + inside `_drive_vel`, own fd); (3) no-auto-resume; (4) mandatory untethered launch-guard (refuse `--drive` without a small geofence + tight cap); (5) prefer a passive-safe BLE/RC fob as primary.
- **P3 — AP + heartbeat integration + bench failure-injection matrix** (verify EACH failure reaches zero+kPrepare, robot MOVING; measure AP coverage + hb latency to SET thresholds — do not guess).
- **P4 — one tiny supervised untethered trial** in the envelope above.
- **P5 — widen the envelope only as sensing ships** (no row unlocked by operator confidence).

## The gate — untethered is FORBIDDEN until ALL are true
1. Hardware e-stop verified independent of the Orin; its reach documented as a hard speed/standoff cap.
2. Heartbeat gate built (own fd + watchdog thread + inside `_drive_vel`), thresholds field-measured.
3. No-auto-resume built; re-arm is a deliberate human gesture.
4. Geofence + tight session cap made mandatory by a launch-refusal guard.
5. Depth-DOWN promoted to a hard stop untethered.
6. Full failure-injection matrix passed on a stand, then at slow speed.
7. Envelope enforced as a launch-path refusal, not runbook prose.

As of 2026-06-26 the heartbeat is unbuilt, the auto-rewalk edge is ungated for untethered, the hardware e-stop is unverified, and fall + wedged-Orin reach no software stop. **Do not run untethered.**
