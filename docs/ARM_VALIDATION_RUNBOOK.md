# ARM-VALIDATION RUNBOOK — F1 anti-lunge / relock gates
**Gate:** `Arm re-lock` (`--arm-reacquire`) stays **OFF** until every check below passes.
**Scope:** validates the *mechanisms* shipped 2026-06-26→07-01 (anti-lunge relock gates, slew,
min-safe floor, depth-fps floor, REID watchdog, DRIVE precondition). It is **not** a re-ID quality
benchmark — the distributional "picks YOU in a crowd" claim comes from the replay eval
(`OPS_BEHAVIOR_EVAL` harness, P2 #14). Physical trials here are mechanism checks + a smoke test.
**Honest stats:** 5/5 correct ⇒ 95% CI ≈ 57–100%. Do not read a clean Phase 2 as "proven safe in
crowds." It means "no gate failed to fire."

---
## Safety contract under test (trigger → fail-safe → owner)
| Mechanism | Trigger | Fail-safe state | Owner |
|---|---|---|---|
| Relock range gate | candidate range >5 m abs, >2.5 m jump vs fresh median, or no validated depth | HOLD (no relock, identity frozen), `RELOC-HOLD-RANGE` | node `_try_reacquire` |
| Post-relock one-shot | first TRACK/COAST frame after armed relock | forward vx = 0 that frame | node control law |
| Slew + re-zero | any commanded vx/vyaw step | ramp ≤0.06/0.10 per tick; forbidden forward re-zeroed **after** slew | node control law |
| Min-safe floor | validated depth ≤0.6 m | forward vx = 0 every frame; debounced geofence stand; release ≥0.9 m | node |
| Depth-fps floor | depth <5 fps sustained | TURN-ONLY latch, `DEPTH-STARVED` | node run-loop |
| REID watchdog | 5 consecutive all-None embeds, or CPU-EP active | `REID-DEGRADED`, armed relock → audit-only | node |
| DRIVE precondition | RGB or depth <8 fps for 2 s at bring-up | refuses to walk, `DRIVE-ABORT` | node `start_drive_chain` |
| Bridge watchdog + deadman | stale velocity / heartbeat loss | zero → kPrepare (50 Hz thread) | `loco_follow_bridge` |

**Failure taxonomy** (log every event as one of): `lunge` · `wrong-person-relock` · `chatter`
(stand↔resume oscillation) · `stand-fail` · `silent-degradation` (badge/log missed a real fault).

---
## Pre-flight (every session)
- [ ] Clear ~4×4 m area, tether on, **spotter within arm's reach of the robot**.
- [ ] Gamepad paired; **test LT+BACK → DAMP once at session start** (it's the only wireless backstop; range unmeasured — stay close).
- [ ] Launch app → Tracker tab → robot IP verified (`192.168.1.81` as of 07-01).
- [ ] App just synced `follow_person_k1.py` (start of a follow does this). Confirm the **new build is live**: log shows `RGB fps=` lines and the `REID:` badge populates.
- [ ] **REID badge = `OSNet(TRT)` green.** Amber `CPU-EP` or red `HIST/DEGRADED` → do NOT proceed to Phase 2 (arming is auto-refused anyway — verify you see `ARM-REFUSED` if you try).
- [ ] `DEPTH FRESH fps=` ≥ ~10 in the log. `DEPTH-STARVED` at rest → fix perception first (restart perception per camera-troubleshooting notes).
- [ ] Standoff 1.2 m; Speed **0.10** for any armed phase (0.18 allowed only in Phase 1).
- [ ] Marker printed & in hand (recovery path); gesture lock available.

## Phase 0 — Preview (no drive) — ~5 min
Toggle ON (preview), `Auto re-acq` ✓, `Arm re-lock` ✗.
- [ ] **P0-1** Badge goes `OSNet(TRT)` on start; `REID-ENGINE ok … Tensorrt…` line visible (not filtered).
- [ ] **P0-2** Cover the depth camera ~5 s → `DEPTH-STARVED … TURN-ONLY` (red); uncover → `DEPTH-STARVED cleared` (green). No stand, no crash.
- [ ] **P0-3** Lock on (gesture/marker), walk out of frame, return → `RELOC-` vote lines appear **past 6 s** of REACQUIRE (F3), including `RELOC-HOLD-RANGE`/`RELOC-VOTE-PASS … audit-only`. Robot never moves (preview).

## Phase 1 — DRIVE, audit-only (arm OFF) — ~15 min
`DRIVE (walk)` ✓ + `ARM MOTION` ✓ + typed confirm. `Auto re-acq` ✓, `Arm re-lock` ✗.
| # | Do | PASS iff |
|---|---|---|
| M1 | Start follow with lens briefly covered, then uncover | `DRIVE-WAIT …` lines, then `DRIVE-READY` → walks. Never walks blind |
| M2 | Start follow with depth unplugged/blocked throughout | `DRIVE-ABORT sensors not sustained-fresh` — refuses to walk |
| M3 | Walk follow normally 2 min | vx/vyaw visibly ramp (no jerk); no `chatter`; `RGB fps=`/`DEPTH` heartbeats present **at ~publish rate (DEPTH ≈10-15, not ~3-5)**; **zero spurious `DEPTH-STARVED` with a healthy sensor for the full 2 min** (re-validation of the fixed fps measurement — the pre-fix build false-latched here) |
| M4 | Step toward robot inside 0.6 m | forward stops (backs off); one more step → geofence stand after ~2 frames (no single-frame trip); back off past ~0.9 m → clean resume, ≤1 stand/resume cycle |
| M5 | Leave FOV; stay hidden | SEARCH yaw-scan (≤8 s) → stand + REACQUIRE (30 s, votes logging) → PARKED. Stable stand throughout |
| M6 | Leave FOV; return at ~2 m; also once with a 2nd person in frame | `AUTO-RELOCK` **does not fire motion** (audit-only): expect `RELOC-VOTE-PASS … audit-only` / `RELOC-HOLD-RANGE`; with 2nd person expect `byp=`/ambiguity rejects. Marker re-seed still recovers |

Any FAIL → stop, pull the log, fix before continuing.

## Phase 2 — ARMED, low speed — only after Phase 1 all-PASS
Speed **0.10**. `Arm re-lock` ✓ (red). Spotter ready on DAMP.
| # | Do | Trials | PASS iff |
|---|---|---|---|
| A1 | Leave FOV, return alone at ~2 m, stand still | 5 | `AUTO-RELOCK … -> TRACK` each time; **first motion is a gentle ramp** (post-relock one-shot + slew — zero forward on the relock frame); zero `lunge` |
| A2 | Same, but return **walking laterally** | 3 | relock only after the 2-frame in-range streak (`RELOC-HOLD-RANGE` may precede) — no snap-lunge mid-crossing |
| A3 | Return with a similar-height bystander 1–2 m from you | 5 | robot relocks **you** or HOLDs. **Any wrong-person relock = session FAIL** |
| A4 | During TRACK, spotter covers depth 3 s mid-follow | 2 | TURN-ONLY (no forward creep), resumes cleanly after `cleared` |

**Abort immediately** (session FAIL, keep the log): any `lunge` · any wrong-person armed relock ·
geofence `chatter` (>2 stand/resume cycles in 30 s) · badge turns red mid-run without a matching
log cause · robot ever moves while PARKED/stood.

## Go / No-Go
**GO** (= `Arm re-lock` may be used in **supervised** sessions; app default stays OFF): Phase 0–2
all PASS, zero abort events, A3 5/5.
**NO-GO:** anything else → file the failing log lines against the taxonomy, fix, re-run the failed
phase only. Untethered/unsupervised arming additionally requires the deadman rebuild
(`UNTETHERED_FOLLOW.md` gates) — this runbook does **not** clear that.

## Post-session (every run, pass or fail)
- [ ] Pull the log: `scp booster@<ip>:/home/booster/k1_follow.err runs/<date>_<phase>.err`
  (on a crash the app now auto-tails it into the Tracker log — still save the full file).
- [ ] Note trial outcomes against the taxonomy in this file's log table (below).
- [ ] **Save the session as a clip candidate** for the replay eval (#14): note timestamp ranges for
  any loss→relock sequence — these become the A/B/C/E regression clips.

| Date | Phase | Trials | Events (taxonomy) | Verdict |
|---|---|---|---|---|
| 2026-07-02 | informal armed field run | follow + 2 losses | Follow GOOD; DRIVE-READY at true rates; 1 armed AUTO-RELOCK OK (g=0.75, range gate held 1 frame then admitted); 1 reacquire FAIL: SEARCH swept past the operator (vote g=0.51-0.54 vs 0.55 floor, id churn, blur) -> stood. No lunge, no wrong-person, no chatter. | Fix shipped: `--search-dwell` (hold scan when a person is in frame). Re-test the exit-return scenario, then run Phases 0-2 formally. |
