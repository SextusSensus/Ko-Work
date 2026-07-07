# SCOPE — NL Command + Planning Layer for the Booster K1

**Status:** Phase-1 NODE-SIDE de-escalation channel **IMPLEMENTED (2026-06-29), default-off**, in `K1Finder/follow_person_k1.py`: the `CommandChannel` watched-file reader (`--cmd-file`, default `/tmp/k1_cmd`), the per-tick non-blocking drain (in `run()`, after `take_if_new`, before `_process_frame`), the `commanded_hold` latch (one top-level guard in `_process_frame` that freezes the whole FSM in a stand — stronger than gating the 5 auto-resume paths individually), and the **de-escalating commands STATUS / HOLD / PARK / STOP** with `CMD … applied/ignored` ACK lines. Gated behind `--commands` (default OFF ⇒ byte-identical). The motion-initiating **RESUME / FOLLOW are now ALSO wired (2026-06-29)** behind a **per-command ARM credential**: under `--drive` the command line must carry the `ARM` token (e.g. `RESUME ARM`) — set only by the operator's ARM-MOTION confirm, never a re-read of the launch flag — else they are refused and the WAIT latch stays set. RESUME re-drives only a live `S_TRACK` lock (with the SCOPE §2.4 post-`_resume_walk` recheck) and cannot recreate a lost identity; FOLLOW re-arms `S_SEARCH` trigger-agnostically and clears any stale gesture hold; in `--preview` both only clear the latch + log. Compiles + unit-tested (channel priority on the command WORD, ARM gate refuse/allow under drive vs preview, RESUME-not-held no-op, full 6-command set, default byte-identity). **HOST SIDE NOW WIRED (2026-06-29, K1Finder.ps1):** the Tracker-follow launch auto-adds `--commands` (`Get-TrackExtraArgs`); a `Send-FollowCmd` function does the one-shot `ssh … 'echo TOKEN > /tmp/k1_cmd'` (mirrors the `pkill` pattern); a **Cmd button row** (WAIT / RESUME / PARK / STATUS / FOLLOW) in `$grpTrackCtl`, enabled only while a follow session runs, with RESUME/FOLLOW gated on `$trackArmChk` (ARM MOTION) + a typed confirm under DRIVE and carrying the `ARM` token. The existing `$trackStopBtn` Ctrl-C STOP stays the supreme deadman. **AST-parse-clean; the WinForms LAYOUT (Tracker row bumped 152→196 px) needs visual verification on the user's machine** — I cannot render it. Test now over raw SSH or via the buttons once launched. **Not started:** the keyword-grammar typed box (buttons cover the enum; a free-text box + parser is the later LLM/voice front-end). Below is the original pre-commit scope.

> **Skill-validation hardening (2026-06-29 — autonomy-ops pass):**
> - **Stale-token clear on startup.** `CommandChannel.__init__` truncates `/tmp/k1_cmd` so a token left by a prior session can NEVER fire on the first tick (the framework's most-cited failure mode — matters most once RESUME/FOLLOW are wired and a stale one would mean motion).
> - **Fail-loud channel.** A persistent read fault now logs `CMD-CHANNEL unreadable` ONCE (then file commands silently drop) — but the **Ctrl-C / gamepad / hardware e-stop remain the real deadman**, independent of this file, so the safety floor never depends on it.
>
> **Command-channel safety contract** (trigger / fail-safe / owner):
> | Mechanism | Trigger | Fail-safe state | Owner |
> |---|---|---|---|
> | **CMD STOP** | operator writes `STOP` | terminal session end → bridge EOF → kPrepare stand | operator; **additive** to Ctrl-C, never a replacement |
> | **commanded HOLD** | operator writes `HOLD` | latched kPrepare stand; FSM frozen; bounded by `--max-seconds` | operator; cleared ONLY by RESUME (deliberate) |
> | **Ctrl-C / Stop-Follow** | GUI / `[char]3` to PTY stdin → SIGINT | same terminal stop → kPrepare | the SUPREME, un-bypassable stop (unchanged) |
> | **Read fault** | `/tmp/k1_cmd` unreadable | file commands drop; logged once; real deadman unaffected | ops — investigate; do not rely on file-STOP |
>
> **Field runbook (this slice):** Pre-flight — launch with `--commands`; confirm `/tmp/k1_cmd` is writable over SSH; confirm the Ctrl-C deadman still halts (it does NOT go through this file). Operate — send via `ssh … 'printf "%s\n" TOKEN > /tmp/k1_cmd'`; watch for the `CMD <word> applied` ACK; a `HELD` banner/heartbeat confirms a WAIT. Recover — a held robot resumes only via RESUME (**not wired yet** → use STOP + relaunch), and auto-ends at `--max-seconds`. If commands stop ACKing, check for `CMD-CHANNEL unreadable` and fall back to Ctrl-C. Shutdown — STOP or toggle the GUI follow OFF.

**Original status:** pre-commit scope. Read this BEFORE writing any code.
**Grounded against (live, not the `_opt_backup_*` copy):**
`K1Finder/follow_person_k1.py`, `K1Finder/loco_follow_bridge.cpp`, `K1Finder/K1Finder.ps1`.

This document scopes a natural-language command front-end that sits **strictly above**
the existing follow FSM and the C++ safety floor. It does NOT propose new motion code in
Tier 1. It is honest that "go to X" (Tier 2) is a separate, from-scratch robotics program.

---

## 1. Architecture in one diagram-in-words

```
            OFF-ROBOT (operator PC, episodic)                 |   ON-ROBOT (Orin, 10 Hz, real-time)
                                                              |
  operator utterance ("wait here", "stop", "follow")          |
        |                                                     |
        v                                                     |
  INTENT ROUTER  ── deterministic keyword grammar (v1)         |
  (K1Finder.ps1)    └─ LLM classifier (later drop-in)          |
        |                                                     |
        |  emits exactly ONE token from a FROZEN enum          |
        |  { cmd, arg?, request_id, ts, source }               |
        |  NEVER free text · NEVER a velocity · NEVER an        |
        |  identity / "that person"                            |
        v                                                     |
  ARM gate + echo/confirm (motion-initiating only)             |
        |                                                     |
        |  separate short-lived `ssh exec` writes ONE line      |
        |  to /tmp/k1_cmd  (NOT the node's PTY stdin)           |
        +────────────────────────────────────────────────────>|  WATCHED-FILE COMMAND CHANNEL
                                                              |        |  drained once per tick, non-blocking,
                                                              |        |  at run() loop top (line ~1417),
                                                              |        |  BEFORE _process_frame
                                                              |        v
                                                              |  EXISTING FSM  (S_SEARCH / S_TRACK /
                                                              |   S_REACQUIRE / S_PARKED)  + new
                                                              |   commanded_hold latch
                                                              |        |
                                                              |        v  (only ever via _drive_vel,
                                                              |           gated drive && walking)
                                                              |  C++ BRIDGE: HARD clamps (VX/VY/VYAW),
                                                              |   staleness watchdog (STALE_MS 800 /
                                                              |   STALE_PREP_MS 3000), EOF→kPrepare
                                                              |        |
                                                              |        v
                                                              |  B1LocoClient.MoveCommand(vx,vy,vyaw)
```

**The single load-bearing invariant: the brain never emits velocity.** The NL layer can
only select one member of a fixed enum. The enum is mapped — on the robot, in Python — onto
FSM transitions that already exist (plus one new latch). Velocity reaches the robot only
through `_drive_vel` (`follow_person_k1.py:1257`), which is gated on `self.drive and
self.walking`, and is then HARD-clamped + watchdog-bounded in the C++ bridge
(`loco_follow_bridge.cpp:61-68, 93-99`). Identity is created ONLY by `_try_seed` via a real
ArUco marker — no command can assert "follow that person".

---

## 2. TIER 1 — cheap, shippable

Tier 1 is **not new motion code**. It is a frozen command enum injected as a new FSM input.
Five of six commands reuse existing transitions verbatim; the only genuinely new behavior is a
**commanded HOLD** latch distinct from the perception-loss stand.

### 2.1 Command table

All FSM line references are to the live `follow_person_k1.py`.

| Command (canonical) | NL triggers (v1 keyword set) | Target state / transition in the REAL FSM | Guard / hysteresis | Reuses existing or needs new state | ARM / confirm? |
|---|---|---|---|---|---|
| **STOP** *(hard / e-stop)* | explicit e-stop affordance ONLY — never the bare word "stop" | sets `self._stop_requested=True` → `run()` `finally` → `_cleanup` → bridge EOF → `kPrepare` (`1409, 1473-1483`); also `_on_signal` path (`1252`) | none — always honored, processed first each tick | reuses `_stop_requested` / SIGINT path verbatim | **No ARM** (de-escalating). Terminal: ends the session → full relaunch to resume. |
| **HOLD** *(a.k.a WAIT / STAY / bare "stop")* | "wait", "wait here", "stay", "hold", **bare "stop"** | set `commanded_hold=True` FIRST, then `_stand()` (zero vel → `kPrepare`, `1265-1280`) | latches; suppresses ALL auto-resume (see 2.3) | **NEW latch** `self.commanded_hold` | **No ARM** (de-escalating, only reduces motion) |
| **RESUME** *(a.k.a CONTINUE)* | "resume", "continue", "carry on", "follow again" | clear `commanded_hold`; if `--drive` + stood, `_resume_walk()` (`1282-1304`); per source state → S_TRACK if a live Seed exists, else re-open S_REACQUIRE/search | ignore a 2nd RESUME while `_resume_walk` mid-flight; per-source-state behavior (2.4) | reuses `_resume_walk` verbatim | **YES** — motion-initiating |
| **FOLLOW** *(a.k.a COME)* | "follow", "follow me", "come with me" | clear `commanded_hold`; force `self.state=S_SEARCH` (`1160`), reset `marker_streak`; if `--drive`+armed+stood, `_resume_walk` so it is ready when a marker seeds | cannot create identity — only re-arms the marker hunt (`_try_seed` path `1581-1588`) | reuses S_SEARCH / `_try_seed` path | **YES** — motion-initiating (when it resumes walk) |
| **PARK** *(a.k.a STAND-DOWN / REST)* | "park", "stand down", "rest" | set `self.state=S_PARKED`, `parked_since=now` → existing terminal stand, watches for marker, bounded clean-exit (`1635-1661`) | reuses REACQUIRE-timeout terminal; tag log with trigger=command | reuses S_PARKED verbatim | **No ARM** (recoverable stand) |
| **STATUS** | "status", "what are you doing" | no state change; emit one status/ACK line | none | none — read-only | No |

**REJECT class (hard-rejected at the enum boundary, logged, no action):** anything outside the
six tokens — "go to the door", "patrol", "come back", "follow that person", any velocity or
distance. This keeps Tier-2 spatial intents from leaking into Tier-1 and silently no-op-ing or
mis-mapping. `COME_HERE`/approach is explicitly NOT in the v1 enum — there is no
commanded-approach primitive in the FSM (the control law only ever drives toward the
*currently associated* anchor, `2047-2056`), so it would emit a command the node cannot honor.

### 2.2 Command-channel mechanism (episodic, not the 10 Hz loop)

**Transport — watched file ONLY.** Write one line to `/tmp/k1_cmd` on the robot via a
**separate, short-lived `ssh exec`** per command (the pattern `Stop-Tracker` already uses,
`K1Finder.ps1:772`): e.g. `ssh user@ip 'printf "%s\n" "$TOKEN" > /tmp/k1_cmd'`.

- **DO NOT reuse the node's stdin.** The live Follow launch uses `ssh -tt` (forced PTY) with
  `RedirectStandardInput=$true` (`K1Finder.ps1:914-916`), and that stdin **already carries the
  e-stop**: `Stop-Follow` writes `[char]3` (Ctrl-C) to it to deliver SIGINT (`K1Finder.ps1:945`).
  A `select()` reader on the node's stdin would race/eat that Ctrl-C — breaking the single most
  important halt path. The stdin-as-command-channel option is **dropped**.
- **DO NOT enable `-tt` on the Tracker stream.** The Tracker ssh deliberately omits `-tt`
  because a PTY corrupts the binary `--stream` JPEG stdout. The file channel sidesteps the
  whole `-tt`/binary-stream conflict.

**Drain point.** Add a single non-blocking read at the top of `run()`, immediately after
`take_if_new` (`line 1417`) and BEFORE `_process_frame` (`1427`): `stat`+read-one-line+truncate.
A per-tick file stat/read is microseconds — nowhere near the YOLO+OSNet budget that already
trips SLOW-LOOP (`1457-1463`). Nothing new runs in the 10 Hz path.

**Hard constraints (acceptance criteria, not footnotes):**
- **Non-blocking, one-record-per-tick.** A blocking read would stall the loop and trip the
  bridge watchdog (`STALE_MS=800` → zero velocity, `cpp:93`). Wrap in try/except like every
  other loop op (the loop "must never die", `1430`); a malformed/torn line → **NO-OP**, never a
  mis-parse (consistent with the enum hard-reject rule).
- **Process order each tick: STOP, then HOLD, then everything else.** A pending STOP/HOLD must
  never be jumped by a FOLLOW/RESUME in the same drain.
- **File-channel write contract.** Writer does atomic write-then-`rename` (or `printf > file`);
  reader reads-then-truncates. A half-written token must map to NO-OP.
- **Node-side ACK.** On every consumed token emit one line: `CMD <word> applied` or
  `CMD <word> ignored:<reason>` via the existing flushed `log()` (`133`). The existing
  `--stream` stderr/banner has NO token-level acknowledgement, so this line is what lets the
  host command box show a real pending→acked transition over SSH latency.

### 2.3 The one genuinely new state: commanded HOLD latch

`self.commanded_hold` (bool) + `self.hold_since`. Distinct from `self.standing` (a recoverable
perception-loss fail-safe that auto-resumes the instant the target reappears).

While `commanded_hold` is True it MUST suppress **every** auto-resume / auto-drive path, not
just one:
1. the S_TRACK standing-resume gate (`1990-1993`),
2. the in-S_TRACK **range-gate** resume (`1977-1986` — re-drives when range returns inside the fence),
3. `_try_coast` re-establishment of follow,
4. `_try_reacquire` auto-relock,
5. marker re-seed in S_REACQUIRE/S_PARKED (`1616`, `1646`).

It is cleared **only** by an explicit RESUME or FOLLOW.

**WAIT must be a true latched `kPrepare` stand, not a zero-velocity hold.** `_hold()` alone
streams `v 0 0 0`, which keeps the robot in `kWalking` holding station (`cpp` sets
`g_v_active=true`) — NOT balanced. And `_stand()` is gated on `self.walking and not
self.standing` (`1274`): if a prior lost-track stand already set `self.standing`, a commanded
WAIT would be a silent `_stand()` no-op. Therefore: **set the latch FIRST, then call
`_stand()`**, and let the *latch* (not `self.standing`) be the thing RESUME clears.

### 2.4 Motion-initiating discipline (must hold on EVERY such path)

RESUME, FOLLOW-that-resumes-walk, and any HOLD-exit-to-walk are a **strict NO-OP unless the
session was launched `--drive` AND a live ARM credential accompanies the command.** This mirrors
`start_drive_chain` and the `$armChk` / ARM-MOTION typed confirm (`K1Finder.ps1`). The ARM must
be a **per-command credential carried in the enum record**, NOT a re-read of the launch
`--drive` flag (which is already true mid-session) — otherwise an NL RESUME walks with no live
human-in-loop. In `--preview` these commands only clear the latch and log.

**`_resume_walk` post-return recheck (hard requirement).** `_resume_walk` blocks ~5 s
(`_sleep_interruptible` 3 s + 2 s, `1293-1297`); during that window the loop drains NO commands.
On return it MUST re-check `commanded_hold` AND `_stop_requested` and immediately `_stand()` if
either flipped — otherwise a "wait"/"stop" issued during the bring-up is swallowed for the full
~5 s, the exact window an operator most wants to abort.

**RESUME per source state (so it never implies re-identification):**
- S_TRACK with live Seed → clear hold, re-drive.
- S_REACQUIRE / S_PARKED (identity lost) → NO-OP with operator reason ("no live target; show
  marker"). RESUME cannot recreate a lost lock — only the marker (`_try_seed`) or an armed
  OSNet relock can.

**Marker-during-HOLD — defaulted SAFE.** A marker shown while `commanded_hold` is latched
**updates identity but does NOT auto-resume walking and does NOT clear the latch**; an explicit
RESUME/FOLLOW is required. Rationale: `_try_seed` jumps to S_TRACK (`1747`) and the next `_track`
frame auto-resumes via `_resume_walk` (`1990-1993`), so "allow + log" would silently let a
marker override an operator's WAIT and initiate motion with no arm action.

### 2.5 The typed box in K1Finder + deterministic-grammar-first

- **UI:** a single-line TextBox + Send (Enter-to-send) near the Tracker/Follow group
  (`$grpFollow`, `K1Finder.ps1:408-412`). Disabled unless a follow session is live and owns
  loco; **hard-disabled in code whenever the Control tab owns loco** (mirror the `trackToggle`
  disable at `:828`) so voice/GUI cannot command a walking humanoid simultaneously — by
  construction, not by runbook. Note the Control-tab follow and the Tracker-tab follow are
  mutually exclusive; bind the box to whichever session owns loco.
- **v1 = deterministic keyword grammar, zero model.** A small PowerShell tokenizer:
  lowercase, strip punctuation, match per-enum synonym sets with **de-escalation precedence**
  (a sentence with both "stop" and "follow" → HOLD/STOP wins). Pure, offline, auditable;
  provably can only emit an enum member or REJECT. This is the entire NL layer for a 6-word
  vocabulary. **Bare "stop" → recoverable HOLD; terminal STOP is a separate explicit
  e-stop affordance.**
- **Motion tokens** (FOLLOW/RESUME) greyed out / refused unless DRIVE + ARM-MOTION, reusing
  `$armChk` / `$followDriveChk`, and the confirm dialog **echoes the PARSED enum, not the raw
  utterance** ("Interpreted as: RESUME — robot will walk. Proceed?"). De-escalating tokens
  (HOLD/PARK/STATUS/STOP) send immediately, no confirm — stopping must never be blocked.
- **LLM later, OFF-robot, behind the SAME enum contract.** The runtime PC has no Python/Node
  (per memory), so an LLM is a deferred **external host-side service**, NOT a v1 deliverable.
  It is a drop-in replacement for the grammar's `match()` step: same input (utterance), same
  output (validated enum line), same ARM/confirm gate, with schema-constrained decoding +
  post-validation against the same whitelist. The keyword grammar stays as the always-available
  offline fallback (field link is often SSH-only).
- **Command-audit log.** One structured line per command at the ingest boundary: `request_id`,
  source, raw NL (if any), parsed enum, confidence, ACCEPT/REJECT + reason, ARM state, FSM
  state before→after, floor action taken. Rejections logged as loudly as accepts.

### 2.6 Tier-1 effort estimate

| Piece | Effort |
|---|---|
| Frozen enum + JSON/line contract | S |
| Watched-file channel + non-blocking drain + ACK log | M |
| `commanded_hold` latch (suppress all 5 auto-resume paths) | M |
| RESUME / FOLLOW under ARM + post-`_resume_walk` recheck + per-state semantics | M |
| PARK / STOP wiring (mostly reuse) | S |
| K1Finder typed box + keyword grammar + ARM gating UI + audit log | M |
| **Total Tier-1** | **~M (a few focused days), no new perception/compute** |

Tier 1 is feasible on the Orin: nothing new runs in the 10 Hz path.

---

## 3. TIER 2 — spatial / navigation (expensive, SEPARATE project)

**Do not undersell this. Tier 2 is not a follow-mode add-on — it is a from-scratch,
Nav2-class autonomy program on a robot that has none of the prerequisites.**

The K1 today is a monocular-camera **velocity-follow** robot. The entire robot-side interface
is `B1LocoClient.MoveCommand(vx,vy,vyaw)` + `ChangeMode` + `GetMode` (`loco_follow_bridge.cpp`).
There is **NO map, NO SLAM, NO localization, NO odometry consumed anywhere, NO costmap, NO
goal-pose interface, NO IMU/TF subscription**. The only metric sense is depth-at-centroid in
`_range_for()` (a single forward scalar, used for standoff and the geofence) — not a pose.

### 3.1 From-scratch stack required for any "go to X" (honest dependency order)

1. **Pose / localization source** *(L–unknown)* — a continuous 6-DoF (or planar 3-DoF) pose in a
   fixed frame. The single missing primitive all absolute commands need. Options: monocular VIO
   (head cam + IMU), LiDAR/RGB-D SLAM front-end, or external (UWB/mocap/GPS). The code already
   refuses metric world tracks because the camera is monocular and scale-ambiguous. Mono-only VIO
   on a walking biped without a confirmed IMU feed is near-non-viable, not merely inaccurate.
2. **Obstacle sensing for a costmap** *(L)* — the head mono cam is weak for this (no native dense
   depth, narrow FOV, can't see low/thin/transparent obstacles or drop-offs). Realistically a
   **hardware add** (depth cam / 2D-3D LiDAR) — software task becomes procurement + integration.
3. **Mapping / SLAM front-end** *(XL)* — build/persist/relocalize a map so "the door" has a
   place to live. Research-grade on a vibrating bipedal platform; map quality bounds everything
   downstream.
4. **Global planner + local controller + costmaps + recoveries + goal-pose→velocity decomposer**
   *(XL)* — the full Nav2 stack. Only the *final* step (twist → the existing clamped velocity
   bridge) is small and reusable; everything upstream is absent. Bipedal kinematics break Nav2's
   diff/holonomic assumptions (`vy` exists but is clamped tiny).
5. **NL→spatial-goal grounding** *(M, but only meaningful AFTER the stack exists)* — place
   vocabulary, patrol = ordered waypoints, "come back" = stored origin pose. Emits a goal pose /
   named place to the nav stack, never velocity.

### 3.2 What the Booster SDK may / may not provide

- **Provides:** locomotion — turns `vx/vy/vyaw` into a gait (`MoveCommand`), and `ChangeMode`.
- **Does NOT provide (in the API the bridge uses):** any `GoToPose`, map, localizer, odometry,
  or pose query. **`GetMode`/`MoveCommand`/`ChangeMode`/`Init` are the entire surface in-repo.**
- **UNVERIFIED, load-bearing:** whether the SDK exposes IMU or joint odometry at all. Nothing
  in-repo proves it. Until this one question is answered on-robot, items 1/2/3/4 are of
  **UNKNOWN feasibility (may require added hardware)**, which is a stronger statement than
  "effort L".
- **Compute is a HARD blocker, not a caveat:** the Orin is already saturated by YOLO11n + OSNet
  ReID at 10 Hz — the code logs SLOW-LOOP overruns and `STALE_PREP_MS` was *raised* from 1000 to
  3000 ms specifically because "the real Jetson loop is slower than the blind initial guess"
  (`cpp:96-99`). A Nav2 stack + SLAM + VIO **will not co-reside** on this box without a second
  compute module or dropping perception.

### 3.3 Minimal-viable subset that IS shippable today: relative open-loop moves

The **only** Tier-2 capability achievable on today's hardware: timed, dead-reckoned **relative**
moves over the existing velocity bridge — "back up 2 m", "turn around 180°". No map, no
localization; distance/angle integrated from **commanded** velocity (open-loop, ±20–40% from
gait slip — "nudge"-class, not docking).

Requirements / safety carve-outs (all hard, all in a NEW `S_MANEUVER` state distinct from
S_TRACK/S_REACQUIRE so a blind move is never mistaken for a perception lock):
- **Python-side distance AND time caps** (`--max-maneuver-seconds`, `--max-maneuver-dist`)
  enforced before each `_drive_vel`. **Do NOT lean on the C++ watchdog for this** — the
  staleness watchdog only fires on STALE `v`; a *healthy* stream re-arms `g_last_v_ms` every
  ~100 ms and the watchdog never bounds distance/duration.
- **Forward translation DISABLED by default.** A blind forward move drives toward an unseen
  volume — strictly less safe than a coast (which already defaults `coast_vx_scale=0`). Default
  to **reverse + yaw only**; gate any forward move on a live depth-at-centroid min-clearance
  check (depth path only, never the bbox-height fallback).
- **Same ARM discipline as `--drive`** + a maneuver-specific typed confirm; unreachable in
  `--preview`; never auto-entered from an NL parse alone (it initiates motion with ZERO
  perception target — worse than follow).
- **Watchdog reconciliation:** during S_MANEUVER, suppress the `--stall-seconds` no-frame→stand
  and the lost-target→REACQUIRE escalation (a blind move has no tracked person), but KEEP
  `--max-seconds`, SIGINT/SIGTERM/EOF→stand/kPrepare, and the maneuver-local distance/time
  termination → `_stand()`.
- **Preemption:** a Tier-1 STOP/HOLD must abort an in-flight maneuver immediately. The maneuver
  stream must be a per-loop step (not a blocking sleep) so the next tick can see a queued stop.
- **On-robot dead-reckoning calibration** (commanded `vx/vyaw` → actual displacement, per
  surface, accounting for gait slip and accel/decel ramps) is real, empirical effort the "M" tag
  hides → realistically **M+ with on-robot time**.
- **"Come back" has NO honest implementation today, relative OR absolute.** There is no odometry
  to integrate ACTUAL motion — only commanded velocity — so inverse-integration drifts. Do NOT
  expose "come back" in the vocabulary.

### 3.4 Tier-2 effort estimate

| Capability | Effort | Gated on |
|---|---|---|
| Relative open-loop move (reverse/yaw, capped, ARM-gated) | **M+** (incl. on-robot calibration) | nothing (existing bridge) |
| Pose / localization source | **L–unknown** | SDK IMU/odom availability (unverified) → possible hardware add |
| Obstacle sensing / costmap | **L** | likely depth/LiDAR hardware add |
| SLAM / mapping | **XL** | obstacle + pose |
| Nav2 planner + controller + goal→velocity | **XL** | map + pose + obstacle + a 2nd compute module |
| NL→spatial-goal grounding | **M** | the whole stack existing |
| **Absolute "go to X" overall** | **XL, multi-month, sensor- AND compute-blocked** | all of the above |

**Recommendation:** scope absolute navigation OUT of the command layer. It is autonomy-planner
R&D, not a feature. The only thing the typed-command vocabulary may honestly expose today is the
capped relative-move primitive — and only after its safety carve-outs are implemented.

---

## 4. Safety invariants the layer must hold + the ops/planner boundary

**Invariants (enforce as regression assertions):**
1. **The brain never emits velocity.** No command path reaches `MoveCommand` except through
   `_drive_vel` under `self.drive && self.walking`. (Assert: no command path constructs a raw
   `v`/bridge write.)
2. **NL never creates identity.** `_try_seed` (marker) is the ONLY Seed creator. The enum carries
   no target descriptor; "follow that person" is impossible.
3. **The C++ floor stays below the enum, untouched.** HARD clamps + staleness watchdog + EOF→
   kPrepare are never widened or bypassed; the layer never widens `self.vx_*/vyaw_*`.
4. **STOP/HOLD are parser-free-priority and de-escalation-bias.** Any ambiguity resolves to stop.
   STOP/HOLD are processed first each tick, are exempt from ARM (safing must never be gated), and
   the GUI STOP / `Stop-Follow` Ctrl-C deadman remains the supreme, un-bypassable authority — NL
   STOP is additive, never a replacement.
5. **The NL/LLM parse is EPISODIC and OFF-PROCESS.** It runs on the operator PC (or a separate
   host service), once per utterance — NEVER in the 10 Hz loop, NEVER in the node's process (the
   Orin is full; it would contend for compute and the GIL).
6. **Motion-initiating commands require a live per-command ARM credential**, not a re-read of the
   launch `--drive` flag.
7. **`commanded_hold` never re-enables motion**; the only motion-initiating exits from HOLD are
   RESUME/FOLLOW under the ARM gate.
8. **The command channel is the watched file only** — never the node's PTY stdin (it carries the
   Ctrl-C e-stop) and never the bridge's stdin (raw clamped velocity, below the FSM).
9. **Single loco driver extends to the command channel** — the box is hard-disabled when another
   source owns loco.
10. **Distinct status states** for HOLD / STOP / PARK so an operator can tell an intentional hold
    from a perception failure (today they look identical to lost-track / PARKED).

**Autonomy-ops vs autonomy-planner boundary:**
- **autonomy-planner (decide):** chooses the enum value from the utterance (the keyword grammar
  / LLM). Off-robot, episodic.
- **autonomy-ops (enforce):** the node-side ingest, the ARM gate, the `commanded_hold` latch, the
  audit log, the FSM/clamp/watchdog floor, the field runbook (authority, deadman, single-driver,
  who may issue NL). Planner proposes a discrete goal; ops decides what it is allowed to do and
  guarantees it can never reach velocity below the floor.

---

## 5. Phased roadmap (smallest first step first)

**Phase 0 — finish the current follow work.** Land the in-flight Stage-2/3 re-ID hardening of
`follow_person_k1.py`. No NL work begins until the FSM (S_SEARCH/S_TRACK/S_REACQUIRE/S_PARKED) is
stable, because every Tier-1 command maps onto these transitions. *No new dependencies.*

**Phase 1 — Tier-1 typed grammar commands (the deliverable this doc scopes).**
- **Smallest first step:** the frozen enum + the watched-file channel + the per-tick
  non-blocking drain + the ACK log line — with **only STATUS and HOLD** wired (HOLD reuses
  `_stand()` behind the new latch; STATUS is read-only). This proves the channel, the drain
  timing, and the pending→acked round-trip end-to-end **before any motion-initiating command or
  any LLM exists.** No ARM risk because neither moves the robot toward anything.
- Then: PARK and STOP (de-escalating, no ARM).
- Then: RESUME and FOLLOW behind the per-command ARM credential, the post-`_resume_walk`
  recheck, per-source-state semantics, and the marker-during-HOLD safe default.
- Then: the K1Finder typed box + deterministic keyword grammar (bare "stop" → HOLD;
  de-escalation precedence) + ARM-gated motion tokens + audit log. Ship here. No LLM.

**Phase 2 — LLM intent + voice (later, OFF-robot, same enum contract).**
- LLM classifier as an external host-side service behind the same enum + ARM/confirm gate, with
  schema-constrained decoding and post-validation; keyword grammar stays the offline fallback.
- Voice/ASR is an explicit non-goal until typed-enum+confirm is field-validated; when added it
  feeds the SAME enum line and the SAME gate — a new front-end, never a new path to velocity, and
  the stop-keyword pre-filter runs on ASR *text*, never gated on ASR confidence.

**Phase 3 — Tier-2 navigation (separate program).**
- Earliest honest step: the capped, reverse/yaw-only, ARM-gated relative-move primitive in a new
  `S_MANEUVER` state (with on-robot dead-reckoning calibration). This is the ONLY Tier-2 item
  shippable on today's hardware.
- Everything absolute ("go to X", patrol, "come back") is blocked on: verifying SDK IMU/odom
  availability, a pose source, obstacle sensing (likely a hardware add), SLAM, a Nav2-class
  stack, and a second compute module. Multi-month autonomy-planner R&D — not a command-layer
  feature.
