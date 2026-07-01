# Supreme Hardware Backstop — Booster K1 untethered outdoor follow

**Date:** 2026-06-26
**Question:** What is the real hardware kill for the K1, independent of the Orin + Booster SDK + WiFi
link? Its action, and its range?
**Status of evidence:** Robot was offline this session (PC on home Wi-Fi `192.168.1.x`, not the K1
`192.168.10.x` subnet) so the on-robot SDK docs at `/home/booster/Workspace/booster_robotics_sdk`
could not be read directly. Findings below combine the **on-PC SDK file mirror** (`k1_tree.txt`,
`k1_paths_list.txt`, `loco_follow_bridge.cpp`) with **Booster's official K1 instruction manual,
the Robotics Center K1 setup/quickstart guides, and the T1 (same controller family)**. Two specifics
remain officially undisclosed and are flagged MUST-VERIFY.

---

## TL;DR

There is **no confirmed wireless, hardware-level, motor-power-cutoff kill** for the K1. Every stop
path — including the manufacturer's own — ultimately rides the robot's **onboard low-level control
firmware**. Ranked by independence from *our* stack (Orin user-SDK / loco bridge / WiFi / laptop):

1. **Booster wireless remote (gamepad) → `LT + BACK` = DAMP.** The most-independent *wireless* stop.
   Action: **all joints go passively compliant → robot goes limp / collapses.** Independent of our
   SDK path, but NOT a power cutoff and NOT independent of the onboard controller or the 2.4 GHz link.
   **Range: NOT officially published — MUST be field-measured.**
2. **Wired e-stop button (ships with K1).** Most authoritative stop, but **tethered** → caps
   untethered radius to the cable, and is documented as a **"soft" emergency stop** (firmware-mediated
   damping, not a verified electrical contactor). Triggers damping; requires restart of the robot
   control service / reboot to resume.
3. **On-body buttons (back panel: WALK / STAND / F1 + power button).** Arm's-reach only. The power
   button is the only true electrical cutoff and = **uncontrolled collapse**.

**Design consequence:** the supreme *independent wireless* backstop is the gamepad DAMP, and its
trustworthy range is unknown. Until it is empirically measured outdoors with body occlusion, the first
untethered follow radius must be the lesser of (validated gamepad-DAMP range) and the wired-e-stop
tether — i.e. **practically arm's reach / a few metres**, exactly the hard constraint anticipated.

---

## 1. Physical body e-stop button

- The K1 **ships with a wired emergency-stop button** (Robotics Center setup/quickstart: a "wired
  e-stop button," must be "within reach of the operator at all times," "tested before every session,"
  "never operate without a functioning e-stop").
- The robot **back panel** carries physical buttons **WALK / STAND / F1** plus the **power button**
  (K1 instruction manual). `STAND` → ready/PREP (controlled stand); `WALK` → walking; `F1` function
  not documented. These are on-body → arm's reach only.
- The K1 manual lists the e-stop as a **"soft emergency stop"** safety feature. "Soft" = the stop is
  executed by the safety/control **firmware** (it commands damping), **not** documented as a hardware
  contactor that physically cuts battery current to the motors.
- ⚠️ **MUST-VERIFY:** whether the *wired e-stop accessory* opens a real power/enable line at the
  hardware level, or merely signals the firmware to damp. The public docs only say "soft." This is the
  single most important open question for trusting any backstop.

## 2. Wireless kill / damping fob

- Booster does **not** ship a dedicated wireless *kill fob*. It ships a **wireless game-style remote
  controller** (Xbox-layout: LT/RT/A/BACK/START), confirmed on-robot by the SDK mirror:
  `/opt/booster/RemoteController` ROS package, `idl/b1/RemoteControllerState.h`, `idl/b1/ButtonEvent.h`.
- Damping/mode combos (Robotics Center setup guide):
  - **`LT + BACK` → DAMP** (limp)
  - **`LT + START` → PREP** (controlled stand)
  - **`RT + A` → WALK**
- **Independence:** the remote talks to the on-robot RemoteController/RobotCore service over its own
  2.4 GHz link — independent of our Orin user-SDK, the loco bridge, the SSH/DDS link, WiFi, and the
  laptop. That makes it the best *wireless* backstop we have. **But** it still depends on the onboard
  low-level controller being alive and the gamepad link being in range; if the low-level controller
  itself wedges, the DAMP command may not land.
- ⚠️ **MUST-VERIFY — RANGE:** no Booster/operator source publishes the remote's RF range. It is a
  2.4 GHz gamepad; comparable controllers are ~10 m line-of-sight and degrade sharply with body
  occlusion and outdoors. **Do not assume a number — bench/field measure the *reliable DAMP-landing*
  range (with the robot's body between fob and receiver) before relying on it.**

## 3. Resulting robot state per stop

| Stop path | Mechanism | Resulting state | Independent of Orin/SDK/WiFi? | Range |
|---|---|---|---|---|
| Gamepad `LT+BACK` (DAMP) | wireless cmd → onboard ctrl → damping | **limp / collapse** | Yes (rides onboard ctrl + 2.4 GHz) | **unknown — measure** |
| Gamepad `LT+START` (PREP) | wireless cmd → onboard ctrl | **controlled stand** | Yes | unknown — measure |
| Wired e-stop button | "soft" → firmware damping | **damping (limp)**; needs service restart/reboot to resume | Yes (but tethered) | **cable length** |
| Back-panel STAND / WALK / F1 | on-body buttons | STAND→controlled stand; WALK→walk | Yes | arm's reach |
| Power button | electrical cutoff | **uncontrolled collapse** | Yes (true HW cutoff) | arm's reach |
| Auto-damping | firmware detects uncontrolled/fall state | **limp / collapse** | onboard only | n/a |
| **our** `damp`/`md`, Ctrl-C/SIGINT | SDK `ChangeMode(kDamping)` / kill SSH proc | damp / stop+PREP | **NO** — needs Orin+SDK+link | n/a |

(K1 also "automatically enters damping mode in uncontrolled states to prevent damage," and boots into
DAMP — joints passively compliant.)

## 4. Confirmation that our software paths are NOT a hardware kill

- `loco_follow_bridge.cpp`: `damp` → `ChangeMode(RobotMode::kDamping)`; all stops end in
  `MoveCommand(0,0,0)` + `ChangeMode(kPrepare)`. Rides the Booster SDK over Fast-DDS on the Orin.
- `K1Finder.ps1` DAMPING (`md`) → same SDK call; the tab's "STOP / e-stop" affordance is a
  **Ctrl-C / SIGINT** written to the SSH-launched process (`K1Finder.ps1:945`,
  `SCOPE_nl_command_layer.md:96`).
- All of the above die if the Orin hangs, the SDK service wedges, the DDS/SSH link drops, or WiFi
  fails. They are operator conveniences, **not** a backstop.

## Open items to close before extending the untethered radius
1. Read the on-robot manual + SDK headers when the K1 is reachable; confirm the e-stop's electrical
   nature (hard cutoff vs soft) — check `b1_api_const.hpp`, `RemoteControllerState.h`, `ButtonEvent.h`,
   and any `/home/booster/Documents` manual.
2. **Measure the gamepad reliable-DAMP range** outdoors, with body occlusion. This number sets the
   first safe radius.
3. Ask Booster directly: (a) is the wired e-stop a true power/enable cutoff? (b) official remote RF
   range? (c) does any stop survive a wedged low-level controller?
4. If no independent HW power-cutoff exists, treat that as a **hard design constraint**: first
   untethered follow stays at arm's reach / a few metres with the operator holding the gamepad, DAMP
   thumb ready.

## Addendum — can the Booster mobile app launch the OSNet follow? (2026-06-26)

**No.** Three independent reasons:
1. **Closed app, no custom-code hook.** The Booster app's documented role is remote control, status
   feedback, and Bluetooth/WiFi network config. It can switch the robot between Booster's *built-in*
   "agents" (default / soccer / conversational-AI / dance) — these are the on-robot `BoosterAgent`
   framework modes (`/opt/booster/BoosterAgent/.../booster_agent_manager`,
   `booster_agent_framework_node`). There is **no public/ documented way to register a custom agent**.
   Booster's open source = `booster_robotics_sdk`, ROS2 SDK, `robocup_demo`, `booster_gym`,
   `booster_train`, `booster_deploy`, `booster_assets` — **no agent-framework SDK, no mobile-app plugin
   SDK.** So `follow_person_k1.py` cannot be exposed as an "agent" the app's picker launches.
2. **OSNet isn't the app's follow.** Any follow the app might offer is Booster's, not our YOLO+OSNet
   re-ID node; the app cannot be pointed at our pipeline.
3. **Single-driver conflict.** The app's remote/agents drive locomotion through the robot control
   stack; our loco bridge also drives locomotion. Only one owner at a time (our SINGLE LOCO DRIVER
   RULE). App and OSNet follow are mutually exclusive, not collaborators.

**What the app IS still good for (untethered):** WiFi/AP network config; battery/status; (the wireless
remote + DAMP backstop is separate from the app and stands on its own — see above).

**The real no-laptop launch paths (don't need the app):**
- **On-robot autostart** — systemd unit / boot script starts `run_follow.sh` in PREVIEW on boot; arm
  DRIVE via gamepad or a physical button. (autonomy-ops bringup.)
- **Robot-hosted phone web UI** — tiny Flask/HTTP page served from the K1 (robot runs Python/ROS2;
  the "no Python/Node" constraint is the *Windows PC* only). Operator opens it on their phone over the
  robot AP — replicates the "phone app" UX but launches OUR follow. Mirrors RoboticsCenter's
  `k1_agent.py` WebSocket-bridge pattern. Depends on WiFi/AP → keep gamepad DAMP as backstop.
- **Gamepad as start/stop** — read `RemoteControllerState` / `ButtonEvent` (SDK headers present) in the
  follow node; map an unused combo to arm/disarm. Same device as the DAMP backstop, over the
  independent 2.4 GHz link. ⚠️ VERIFY on-robot that a user node can read controller state while the
  controller is also the loco authority (possible mode conflict).
- **Phone SSH** (Termius) running `run_follow.sh` — crude, zero new code.

Any of these is gated by the existing untethered-follow safety gate (deadman + the DAMP backstop).

## Sources
- Booster K1 Instruction Manual (ManualsLib): https://www.manualslib.com/manual/4118138/Booster-K1.html
- Robotics Center — K1 Setup Guide: https://www.roboticscenter.ai/hardware/booster-k1/setup
- Robotics Center — K1 Quickstart: https://www.roboticscenter.ai/en/hardware/booster-k1/quickstart
- Robotics Center — K1 hardware page: https://www.roboticscenter.ai/hardware/booster-k1
- Booster T1 user manual (same controller family): https://static.generation-robots.com/media/user-manual-booster-t1-en.pdf
- On-PC SDK mirror: `K1Finder/k1_tree.txt`, `k1_paths_list.txt`, `loco_follow_bridge.cpp`
