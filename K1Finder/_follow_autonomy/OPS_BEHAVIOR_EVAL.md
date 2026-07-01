# Follow-Mode Autonomy — Behavior, Ops & Eval

**Scope:** the runtime contract for taking `K1Finder/follow_person_k1.py` from `--preview` to `--drive`, and the regression gate that authorizes it. Complements (does not repeat) the two existing docs:

- **`PLAN.md`** owns the *perception design* (architecture, staged rollout, A–E coverage).
- **`STAGE_2_3_IMPL.md`** owns the *code-level implementation* (class bodies, edit sites, commit sequence).

This doc owns three seams the other two leave open: **the formalized behavior FSM**, **the ops safety floor (incl. the bridge command-staleness watchdog)**, and **the replay eval gate** — plus how the three compose.

> **Anchor authority.** Line numbers were verified against a 1807-line snapshot of `follow_person_k1.py` and a 225-line `loco_follow_bridge.cpp`. Stage 3 (`_try_reacquire`, `TS_RELOCALIZING`) has since landed, so exact line numbers have shifted again — treat anchors as approximate and re-grep. Produced by an autonomy-planner + autonomy-ops + sim-eval workflow with adversarial critique.

---

## 1. TL;DR & prioritized action list

The follow stack is genuinely modal and correctly an FSM. Its identity spine is sound: the frozen-anchor veto gates identity **every tick** (not just on track presence); on-enter side effects are idempotent; and stop-enforcement lives in ops, not the FSM. The hardening gaps concentrate in **the give-up states** and **the missing bridge deadman**.

| # | Severity | Tag | Action |
|---|----------|-----|--------|
| 1 | **CRITICAL** | [ops] | **Add a bridge-side command-staleness deadman to `loco_follow_bridge.cpp`.** A Python hang *inside* `_process_frame` under `--drive` (onnxruntime/cv2 blocking call) never iterates the loop, never re-issues `v`, never trips the frame-stall watchdog or `max_seconds` — and the bridge blocks on `getline` holding the last `MoveCommand`. The robot walks on the last `v` until a human kills it. The one hazard the EOF latch cannot cover. See §3.2 (dedicated-thread, two-tier design; why poll-then-getline is unsafe). |
| 2 | ✅ DONE (2026-06-25) | [behavior] | **`S_PARKED` give-up state implemented.** `reacquire_timeout` now transitions `S_REACQUIRE → S_PARKED` (named, stood, marker-recovery + throttled heartbeat). Bounded exit via `--park-timeout` (>0 → clean process-exit → bridge EOF → ops safes; default 0 = terminal-until-`--max-seconds`). |
| 3 | **HIGH** | [eval] | **Define code success predicates + a `--compare` scorecard that exits non-zero on regression,** wired per commit using the existing rollback flags on one binary. No measurable pass/fail today — only eyeballing. See §4. |
| 4 | ✅ DONE (2026-06-25) | [ops] | **Depth-validated follow-range geofence implemented.** `--max-follow-range`/`--min-safe-range` (default 0 = disabled); in `_track`, a precondition gate computed *before* the resume block (no chatter), escalates through `_stand()`/`_hold()`, gates on `rsrc=='depth'` only. Not spliced into the control law. |
| 5 | **MEDIUM** | [behavior] | **Add N-of-M debounce to the anchor-veto LOCKED↔lost boundary and coast entry.** Single hard thresholds chatter at sim≈0.5 (mid-turnaround). **Debounce only — never lower the per-frame admission floor** (a lower exit floor makes an impostor easier to ride; N-of-M only delays the loss declaration, failing toward STOP). |
| 6 | **MEDIUM** | [behavior] | **On-separation isolation re-confirm before re-binding `track_id` after a coast gap.** PLAN.md §2.3 promises it; the live file rebinds on the first anchor-passing frame. Anchor veto keeps it identity-safe, but a same-clothing crosser at the re-acquire instant rides the *motion track*. Use `max_iou_other` (present). |
| 7 | **MEDIUM** | [ops] | **Surface failed bridge writes** (`Bridge._send` swallows `BrokenPipeError`/`OSError`→None; `_drive_vel` ignores it). Defense-in-depth behind #1. |
| 8 | **MEDIUM** | [eval] | **Replicate `latest_depth` max_age=0.5 staleness in the replay adapter** so the `rsrc=='depth'` branch matches on-robot; freeze YOLO detections for the FSM-regression lane. |
| 9 | **MEDIUM** | [behavior] | **Make `S_SEARCH`'s unbounded wait an explicit decision** (periodic elapsed-time log; optional `--search-timeout → S_PARKED`). Observability only. |
| 10 | **LOW** | [ops] | **Make the bridge watchdog observable** (`OK staleguard <ms>` in the handshake; one line per stale trip) + a lightweight timestamped safety record. |

**On #1's placement:** the bridge command-staleness watchdog is **CRITICAL**. The frame-stall watchdog does **not** cover a hang inside `_process_frame`; neither does `max_seconds`. The FSM is only allowed to assume "the stop floor holds" *if ops ships this watchdog*.

---

## 2. Behavior contract (FSM)

The planner *names* states; it never re-implements the stop floor (ops). **GPS handoff is genuinely out of scope** — there is no GPS/geo anywhere in the code; the house `GPS_FOLLOW → VISUAL` pattern does not apply and must not be invented.

### 2.1 States, sub-states, safe floors

Node-level `self.state`: `S_SEARCH` / `S_TRACK` / `S_REACQUIRE`. `S_TRACK` carries `Seed.track_state`: `TS_LOCKED` / `TS_COASTING` / `TS_RELOCALIZING` (Stage 3, within `S_REACQUIRE`).

| State | Tick guard | Safe floor |
|---|---|---|
| **S_SEARCH** | stable marker + a person plausibly owns it → `_try_seed` → S_TRACK | **no motion ever** |
| **S_TRACK / TS_LOCKED** | anchor-veto pass AND `cost ≤ gate` AND not ambiguous → control law | anchor-fail / no-detection → coast or `lost_count++` |
| **S_TRACK / TS_COASTING** | bound track unseen AND `time_since_update ∈ (0, coast_frames]` | **yaw-only**; vx forced 0 unless validated depth (SACRED) |
| **S_TRACK hard-loss** | `lost_count ≥ lost_grace` → `_stand()` → S_REACQUIRE | `_stand()` kPrepare |
| **S_REACQUIRE** | success: marker re-seed → S_TRACK; **(Stage 3)** passive vote → S_TRACK, audit-only on HS | `_stand()` kPrepare, idempotent every tick |

**The property the design gets right (keep it):** presence ≠ identity is enforced **every tick** — `_associate` applies the frozen-anchor veto to every candidate every frame; the motion `track_id` is only a preference/tie-break among already-anchor-passing candidates, never an admission; `anchor_hist` is created/replaced only in `_try_seed`.

**Ladder ordering is correct:** `TS_COASTING` (bounded) bridges brief occlusion *before* `lost_count` increments; hard loss enters `S_REACQUIRE`; the marker (and the Stage-3 passive vote) is the give-up. Coast and reacquire are time-disjoint (coast walks in S_TRACK; reacquire stands in S_REACQUIRE).

### 2.2 Invariant every gap is measured against
> **Every state needs a timeout AND a degraded-signal exit.** A state left only on success is the #1 planner pitfall.

### 2.3 Gap list
1. **[HIGH] `S_REACQUIRE` timeout fires nothing** — `reacquire_timeout` only changes a log string; the only thing that ends an abandoned REACQUIRE is `max_seconds` killing the process (an ops backstop). The robot is SAFE (stood), so this is a correctness/observability gap, not a fall risk. **Fix:** a named `S_PARKED` that stays stood, keeps emitting a heartbeat (don't go silent on an unattended robot), and has its own bounded terminal exit (preferred: clean process-exit → bridge EOF → kPrepare). Do not relocate the dead-end onto `S_PARKED`.
2. **[MEDIUM] `S_SEARCH` has no timeout** — waits for a marker forever. For initial-acquire (no motion) this is arguably intended; make it a *documented* decision with a periodic "still waiting" line.
3. **[MEDIUM] Hard-threshold chatter** at the anchor veto / ambiguity / coast boundaries. **Fix (SAFETY-CONSTRAINED):** N-of-M consecutive anchor-fail debounce before declaring lost; flag-gated, default = today. **⚠ Do NOT use a lower exit floor than the enter floor** — that makes a same-clothing impostor admissible-to-keep during the exact crossing window; N-of-M only *delays the loss declaration* and fails toward STOP.
4. **[MEDIUM] `track_id` rebind after a coast gap has no separation re-confirm** — anchor veto keeps it identity-safe, but a same-clothing crosser can capture the motion track. **Fix:** require the candidate bbox-isolated before re-binding after a `TS_COASTING` gap.
5. **[landed] `TS_RELOCALIZING`** — now in the file (Stage 3): time-boxed (`reloc_seconds`), strictly gated, passive (runs only after `_stand`, no `_drive_vel`), audit-only on HS.

### 2.4 Stop-enforcement & GPS notes
- **Stop-enforcement lives in OPS.** The FSM names behavior and calls thin `_stand`/`_hold` wrappers; the bridge enforces the hard floor on EOF/SIGINT/SIGTERM/quit + HARD clamps.
- **The FSM relies on an assumption ops must enforce:** continued motion requires the loop to keep ticking and re-issue `v`. The frame-stall watchdog does NOT cover a hang inside `_process_frame`. This is a **named unmet assumption** handed to ops (§3 #1). Document it near the state constants.
- **GPS handoff: future.** A coarse-approach `S_APPROACH` would sit *before* `S_SEARCH` with the same safe-floor, single control hook, and its own timeout. Noted, not built.

---

## 3. Ops safety contract & runbook

Ops **adds floors**; it never authors behavior. Every mechanism only ZEROES or STANDS. Nothing here touches the control law, the 3-layer clamps, `target_point_from_box`, COASTING yaw-only+depth, identity-in-`_try_seed`, the `--preview` default, or the K1F1 wire format.

### 3.1 Safety-contract table (✅ = present; **bold** = missing)

| Mechanism | Trigger | Fail-safe action | Owner | Status |
|---|---|---|---|---|
| HARD velocity clamps | every `v` | clamp VX[-0.10,0.30] VY[±0.15] VYAW[±0.40]; NaN→0 | bridge | ✅ |
| EOF/SIGINT/SIGTERM/quit safe | pipe/process death or `quit` | `MoveCommand(0,0,0)` then `kPrepare`; idempotent CAS | bridge | ✅ |
| Fail-safe STAND | hard loss / camera stall / frame error | zero vel THEN `kPrepare` (never `kDamping`); gates velocity off | python | ✅ |
| Frame-stall watchdog | no NEW frame > `stall_seconds` (~1s) | `_stand()` | python | ✅ |
| Session watchdog | `max_seconds` (120s) | break → cleanup → bridge EOF | python | ✅ |
| Ping-before-walk bringup | always | read-only `GetMode`, real frame, settle, then walk | python | ✅ |
| App STOP (de-facto e-stop latch) | operator STOP | kill process → bridge EOF → `kPrepare`; no auto-resume | app+bridge | ✅ (only latch) |
| **Command-staleness deadman** | **no fresh `v` > STALE_MS while moving** | **`MoveCommand(0,0,0)`; escalate `kPrepare` at STALE_PREP_MS** | **bridge** | **MISSING — #1** |
| **Max-follow-range stand** | **depth-validated range > `max_follow_range`** | **`_stand()` via precondition gate** | **python** | **MISSING — #4** |
| **Write-fail trip** | **K consecutive `_send`→None while walking** | **`_stand()` + break** | **python** | **MISSING — #7** |
| **Operator deadman (latched)** | **no operator heartbeat in window** | **latched `_stand()`; no auto-resume** | **python** | **OFF; transport TBD** |

### 3.2 Command-staleness watchdog design (the headline ops floor)
The bridge blocks on `getline` and `MoveCommand` is fire-and-forget; the B1 loco service holds the last commanded velocity until a new one arrives. If the Python driver hangs but the pipe stays **open**, no EOF fires, `getline` never returns, and the robot keeps walking. **Required design — a DEDICATED WATCHDOG THREAD, not poll-then-getline:**
1. **Dedicated thread + `std::atomic<steady_clock::time_point> last_v_time`** updated by the command thread on each valid `v`. The watchdog issues `MoveCommand(0,0,0)` when `(now - last_v) > STALE_MS`, **independent of the getline thread.** *Why not poll-then-getline:* `std::getline` on a `POLLIN`-ready fd can block on a partial line (no `\n`); a dribbling 1-byte/200ms ssh write keeps it readable forever and the deadline is never evaluated → the watchdog wedges.
2. **Two-tier floor:** zero velocity at `STALE_MS` (jitter cover, stay in gait), then escalate to `ChangeMode(kPrepare)` at a longer `STALE_PREP_MS`. EOF/quit/SIGINT keep immediate-kPrepare.
3. **Serialize all `MoveCommand` behind a bridge mutex** (or verify thread-safety on-robot first) — a dedicated thread races the command loop on the Fast-DDS writer otherwise.

**Preserves existing paths:** `STALE_MS` set well above the 100ms stream period + jitter (~300-500ms) so normal cadence never trips; first tier auto-ZEROs (not latch, not ChangeMode) and clears on the next valid `v`; `safe_shutdown` idempotency means a staleness-zero then EOF-safe is fine; the zero tier must NOT call `ChangeMode` (races the EOF `kPrepare`); fd0 is never touched by the fd1/fd2→/dev/null juggling.

**On-robot gates (no sim can answer):** does the B1 service hold the last `MoveCommand` indefinitely or self-zero? measure actual inter-`v` jitter before fixing `STALE_MS` (too tight → stutter-stop a humanoid mid-follow).

### 3.3 Max-follow-range stand (#4) — as a PRECONDITION, not inline
Add `--max-follow-range`/`--min-safe-range` dropping to `_stand()`/kPrepare. **Evaluate as a precondition gate through the existing `_track`/`_stand` path — do NOT splice between the vx-clamp and `_drive_vel`** (that creates two owners of the stand decision in one tick). **Gate on `rsrc=='depth'` only**, mirroring the SACRED COASTING vx rule.

### 3.4 Deadman / e-stop
App STOP is a true latch (process-kill → EOF → kPrepare) but the **only** latch, and depends on the app delivering the kill. No operator-liveness deadman exists (the 10Hz `v` stream is *driver* liveness, not *operator* presence). An operator deadman must LATCH (no auto-resume), ships OFF; transport unresolved (heartbeat-file mtime, second ssh exec channel, or accept process-kill as the only operator stop for v1). **Do not route the heartbeat over the bridge command channel.**

### 3.5 Bringup (confirmed correct — preserve)
`start_drive_chain` enforces safety-up-first: spawn → `ping` (read-only `GetMode`, no motion) → require a real frame → `prep` → settle → `walk` → settle → `walking=True` gates velocity. YOLO loaded+verified before any walk. Any abort leaves the robot un-walked.

### 3.6 Observability (#10)
Today: K1F1 status byte + rich stderr. For `--drive` sign-off: `OK staleguard <ms>` in the handshake; one `STALE -> zeroed` line per trip; a flag-gated (`--safety-log`) timestamped JSONL `{t,state,track_state,walking,standing,last_vx,last_vyaw,clamped_vx,clamped_vyaw,frame_age,watchdog_trip}` captured on the app side (no Python on the runtime PC).

### 3.7 Field runbook (`_follow_autonomy/RUNBOOK.md`)
- **PRE-FLIGHT:** hardware e-stop exists/cuts loco power/reachable; bench-test app STOP kills process → robot stands; bridge handshake reports `staleguard` armed; `--preview` sees a real frame; clear zone; set `--max-follow-range`.
- **OPERATE:** `--preview` → confirm stable `track_id` + status → `--drive`; hand on hardware e-stop; software e-stop = app STOP.
- **SHUTDOWN — safety down LAST:** app STOP/quit (→ EOF → kPrepare) before powering loco; confirm STOOD before leaving.
- **POST-INCIDENT:** pull stderr + safety JSONL + bridge `STALE`/clamp lines; classify (track-stale / command-stale / geofence / operator-deadman / hardware); never re-`--drive` until explained.

---

## 4. Eval & regression suite

**Substrate: recorded-clip REPLAY** with per-frame ground-truth "which detection is the anchored person" labels — **not** a rendered sim (a sim's appearance gap would invalidate the HS/OSNet identity signal under test).

**Replay-ready:** in `--preview`, `_drive_vel` is inert and `_process_frame` emits decisions with no control side effects. The only live couplings are `CamNode` (`take_if_new`/`latest_depth`) and `rclpy.spin_once`. A `ReplayCamNode` duck-typing those two methods + direct `_process_frame` calls (construct `Follower` with preview=True, never call `run()`/`start_drive_chain`) injects clip frames through one seam.

### 4.1 Labeled clip families (A–E)
Under `_follow_autonomy/eval/clips/{A..E}/REG-*/` + `HELD-*/`, each with `frames/`, `depth/`, a **real ArUco marker in the seed segment**, and `labels.jsonl` (`gt_track_id`/`gt_det_idx`, `gt_visible`, `gt_isolated`). ~**100+ clips/family** for a tight Wilson bound. Fixed `REG-*` (apples-to-apples) + rotating `HELD-*` (catches tuning to the test). **Seed only from the replayed marker frames** — the adapter must NOT set `Seed` directly (violates identity-in-`_try_seed`).

### 4.2 Predicates (frozen BEFORE the run) + metrics
- **A** zero ID-switch AND failures degrade to safe-stop. **B** false-loss rate below the Stage-1 baseline lower-CI. **C** coast bridges occlusion AND re-confirms the **same** gt track. **D** newcomer never selected + only-isolated banking. **E (HS)** `RELOC-VOTE` lands on gt with precision LB above threshold AND **zero `AUTO-RELOCK`** AND **zero vx/vyaw**.
Scorecard reports per family: mostly-tracked, false-loss, false-stop, ID-switch, gallery-admission purity, distractor-bank purity, reloc precision/recall — **each with a Wilson 95% CI** (a scalar gate hides false-stop↔ID-switch trades). Stage-2 gate: **admission purity < 1.0 on REG → exit non-zero.**

### 4.3 Adapter + scorecard gate (wired per commit)
- **`eval/replay_adapter.py`** — `ReplayCamNode` reads frames (+ optional depth npy), exposes `take_if_new`/`latest_depth`, drives `_process_frame`, tees one record/frame. **Must replicate `latest_depth(max_age=0.5)`**; a clip with no depth must make `latest_depth()` return `None`.
- **`--eval-jsonl` tap** (additive, default off). **MUST-FIX (wire safety):** write to a **dedicated file only, NEVER `sys.stdout`** (stdout carries the binary K1F1 protocol); wrap in defensive try/except; prove strict no-op when unset.
- **`eval/freeze_dets.py`** — cache YOLO boxes once/clip so the FSM is scored on identical detections across versions (else ultralytics/CUDA drift swamps the signal). Keep a separate **live-YOLO** lane. This wrapper must be **unreachable from the robot entrypoint**.
- **`eval/scorecard.py`** — Wilson CIs + `--compare` Stage N vs N-1, **exits non-zero** on regression. Baselines are exact rollback flags on the same binary: `--no-track`, `--gallery-size 1`, `--no-auto-reacquire`. One invocation per commit.

### 4.4 HS → OSNet (Stage-4 gate): what replay CAN'T gate
On HS, `_try_reacquire` is audit-only. **HARD LIMITATION:** HS-vote precision is NOT a valid predictor of OSNet armed re-lock (a same-clothing stranger clears `reloc_floor 0.70` on HS). **Replay on HS CANNOT gate the Stage-4 armed path** — only on-robot with the embedding backend can. **Do NOT build an "armed-HS lane" that forces `_relock_armed=True`** (flips the sacred flag gating identity-rebind-from-vote). If ever needed, a separate replay-only driver never reachable from `main()`.

### 4.5 Reality-gap honesty
The suite is **open-loop**: it proves the perception+FSM decision *given a view* but cannot prove the closed-loop view a corrected vyaw would produce. **On-robot `--preview` then `--drive` remains mandatory.** Scorecard verdict states: *"replay PASS is an UPPER BOUND; requires on-robot spot-check before `--drive`."*

### 4.6 Build incrementally + run location
A/C/D taps buildable today (Stage 1+2 landed); E-reloc taps land with Stage 3 (now in the file). Runs on the **robot Jetson or another Linux Python box**, never the Windows UI host; avoid `rclpy.init` in the replay entrypoint. Resolve **ground-truth provenance** (hand-label vs validated bootstrap) before scoring.

---

## 5. How the three layers compose

| Concern | Owner | Must NEVER |
|---|---|---|
| **"Stop" — safe to keep moving?** | **OPS** (clamps + EOF safe + command-staleness deadman + `_stand`/`_hold` + watchdogs + max-range) | author a heading or re-find a target |
| **"What next" — which state/target?** | **PLANNER (FSM)** (states + per-tick anchor veto + COAST→give-up ladder) | re-implement the stop floor; change identity outside `_try_seed`; assume motion continues without a fresh `v` |
| **"Good enough" — gate the deploy** | **EVAL** (replay + frozen predicates + Wilson scorecard + `--compare` exit gate) | flip a sacred flag, write to K1F1 stdout, or claim green replay = drive-ready |

**Post-incident triage — classify into exactly one primary bucket:** (1) **behavior bug** = wrong state given a correct view; (2) **floor gap** = robot moved when it shouldn't regardless of FSM intent (e.g. the stale-`v` runaway); (3) **eval miss** = real, reproducible, uncaught before `--drive`. Record the primary + any secondary eval miss; fix the primary first.

---

## 6. Open decisions for Modd
1. **[behavior]** `S_REACQUIRE` timeout terminal: clean process-exit (recommended), park-until-`max_seconds`, or fall back to `S_SEARCH`?
2. **[behavior]** `S_SEARCH` unbounded wait: intended product behavior, or time-box to `S_PARKED`/exit?
3. **[behavior]** Anchor-veto debounce: confirm N/M for the N-of-M (the lower-exit-floor variant is rejected as identity-weakening); ship flag-gated, default = today.
4. **[ops]** Does the B1 loco service hold the last `MoveCommand` indefinitely or self-zero? Verify on-robot.
5. **[ops]** `STALE_MS`/`STALE_PREP_MS` — needs on-robot inter-`v` jitter measurement (~300ms zero / longer kPrepare starting point).
6. **[ops]** Operator-deadman transport (heartbeat-file mtime / second ssh channel / accept process-kill for v1)?
7. **[ops]** Verify `B1LocoClient::MoveCommand` thread-safety on-robot, or serialize behind a bridge mutex (recommended).
8. **[ops]** Max-follow-range: confirm depth coverage; pick defaults outside today's envelope (inf = today).
9. **[eval]** Ground-truth provenance: hand-label vs validated bootstrap?
10. **[eval]** Gate-threshold policy: block on `REG` only (HELD advisory) or both? Wilson LB pass bars?
11. **[eval]** Frozen-dets vs live-YOLO two-lane design; corpus host (likely the Jetson).
12. **[cross-cutting]** Orin module ID, YOLO end-to-end frame-time, OSNet/`tensorrt`+`pycuda` — gate Stage 4 (which makes A robust and HS-E trustworthy).
