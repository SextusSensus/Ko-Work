# Council review — full adversarial audit (2026-09-11)

Multi-agent adversarial review of the live Ko-Work tree after import of
`SextusSensus/Ko-Work` `main` @ `b386fea`. **Six council members** (Safety,
Pipeline Alignment, Desktop, Perception & Control, Eval/Recon/Cluster,
Security/Secrets) → independent live-code verification → synthesis.

**Raised → adjudicated:** 30+ candidate findings → **22 CONFIRMED**, several
**REFUTED** (prior bugs fixed or claims wrong), a handful **NEEDS_VERIFY**
(on-robot only).

---

## The reassuring headline

The **safety spine still holds**:

| Invariant | Status |
|---|---|
| Preview never spawns the loco bridge / never sends `v` | **HOLD** |
| Drive requires deliberate ARM (UI confirm + per-cmd token) | **HOLD** (Tracker path) |
| Hard velocity clamps in Python ⊂ C++ bridge | **HOLD** |
| Stop/safe on loss, camera stall, max-seconds, signals, EOF | **HOLD** |
| Heartbeat deadman fail-closed when armed | **HOLD** (software; laptop-tethered) |
| **No shadow / learned-policy → loco bridge path** (absent, not flagged off) | **HOLD** |
| Map-assist / localmap may only **reduce** clearance, never raise it | **HOLD** |
| One loco driver / one follow node (app mutex + launcher `pgrep`) | **HOLD** |
| Untethered outdoor follow | **STILL FORBIDDEN** |

Prior council (2026-07-14) HIGH #1 — `/cmd/vx,/cmd/vyaw` logged only on TRACK —
is **REFUTED (fixed)**. Commands now emit from the `_drive_vel` chokepoint.

---

## Council roster & charges

| Member | Charge | Top verdict |
|---|---|---|
| **Safety** | Locomotion, deadman, stop paths, untethered gate | Spine holds; untethered still closed; HB auto-rewalk gap |
| **Pipeline Alignment** | Robot Rerun log ↔ train/label consumers | Cmd channel fixed; **obs channel still TRACK-only** |
| **Desktop** | K1Finder mutex, launch paths, stop chain, Autotune yield | Mutex/one-node solid; Offload password undermines key-only app |
| **Perception & Control** | Identity, gap steer, map-assist, Aurora | Prior claims re-verify; Aurora/map-assist under-tested |
| **Eval / Recon / Cluster** | Silent wrongness, OOM, hangs, prior F4–F8 | F4–F8 still in tree; **new cluster lease/heartbeat race** |
| **Security / Secrets** | Passwords, askpass, `.rrd` secret leakage | Offload/Pull still hardcode `123456`; UI leaks fixed |

---

## Prior council (2026-07-14) scorecard

| # | Finding | 2026-09-11 verdict |
|---|---|---|
| 1 | `/cmd/vx,/cmd/vyaw` TRACK-only → teaches drive-while-standing | **REFUTED (fixed)** — emit from `_drive_vel` (`robot/follow_person_k1.py:711-729`) |
| 2 | `/reid/sim`, `/track/conf`, `/follow/range` TRACK-only → stale lock obs | **CONFIRMED (still open)** (`:5115-5125`) |
| 3 | RGBD stamped with `time.time()`, not `msg.header.stamp` | **CONFIRMED (still open)** (`robot/perception.py:398`, `:420`) |
| 4 | Untethered still FORBIDDEN | **CONFIRMED** |
| F4 | PAIR-WARN / duplicate-RGB pairs | **IN TREE** (`desktop/recon/geom.py:66-78`) |
| F5 | Refuse non-`mp4` train episodes | **IN TREE** (`eval/train_act.py:170-213`) |
| F7 | `depth_only` ingest + crash→manifest | **IN TREE** |
| F8 | CoACD pre-clean + `K1_JOB_TIMEOUT_S` | **IN TREE** (`cluster/worker.py:140-153`) |

---

## CONFIRMED findings (ranked)

### HIGH

1. **Offload/Pull still hardcode robot password `123456` + force password SSH**  
   `desktop/Offload-Run.ps1:13-25`, `desktop/Pull-Run.ps1:22-38`. Askpass written to
   `%TEMP%` and never deleted. K1Finder correctly dropped `-Pass` from `Invoke-Offload`
   (`K1Finder.ps1:1888+`) so the child **falls back to the hardcoded default**, not
   `$env:K1PW` / key-only. UI/log password leaks are fixed; this lane is not.  
   **Fix:** key-only Offload/Pull (mirror Autotune `BatchMode=yes`); delete askpass; rotate robot password.

2. **Pipeline obs still TRACK-only (prior #2)**  
   `/follow/range|bearing`, `/reid/sim`, `/track/conf`, `/cmd/forbid_forward` only on
   accepted TRACK (`follow_person_k1.py:5113-5125`). Coast/grace/SEARCHING/REACQUIRE
   update cmd but not obs; `eval/rrd_to_lerobot.py` `_ffill` then copies a stale
   “confident lock” into standstill rows.  
   **Fix:** emit real obs / NaN every tick beside `/fsm/state_id` (same pattern as `_drive_vel`).

3. **RGBD arrival-time stamps (prior #3)**  
   `perception.py` never reads `msg.header.stamp`. Label path wall-joins ≤0.5s
   (mitigation, not a capture fix). Ghosted depth → wrong obstacle geometry into
   labels/recon.  
   **Fix:** stamp both RGB and depth from `msg.header.stamp`.

4. **Cluster lease heartbeat gap vs 90‑min job timeout (NEW)**  
   `docker_run_job` heartbeats **once** then blocks on `docker run`
   (`cluster/worker.py:245-247`). Scheduler default lease **120s**
   (`cluster/scheduler.py:31`); `requeue_stale` reaps silent leases. Any
   recon/train/splat >~2 min is reaped while the container still runs → duplicate
   lease / name collision / ignored `report_result`. F8’s wall-clock kill does
   **not** keep the lease alive.  
   **Fix:** mid-run heartbeat thread (or docker events watcher) for the lease TTL.

5. **Untethered still FORBIDDEN — and the doc body is stale**  
   Software HB exists and matches the 2026-07-02 update banner in
   `docs/UNTETHERED_FOLLOW.md`, but the body still claims “deadman does not exist”
   (`:18-19`). Remaining blockers are real: HW e-stop unverified, auto-rewalk on
   HB restore / post-`kPrepare` without re-ARM (`loco_follow_bridge.cpp:362-363`;
   node `HB-OK` resumes velocity), depth-DOWN is turn-only not hard stand,
   session caps (app 300s / launcher default 2000s) far above “tens of seconds.”  
   **Fix:** rewrite the “Why FORBIDDEN” body; keep the gate closed; on HB-restore stay disarmed until explicit ARM.

### MEDIUM

6. **Control-tab Follow DRIVE cannot satisfy the P1.2 deadman gate**  
   `Start-Follow` launches bare `run_follow.sh drive` with no `--require-heartbeat`
   (`K1Finder.ps1:2081`). Node refuses. Fail-closed is correct; UI may imply Control
   DRIVE works. Tracker is the armed path.

7. **Control-tab missing `ServerAliveCountMax=1`**  
   Tracker has it (`:1807-1809`); Control follow uses default CountMax ≈ 3 → ~15s
   half-open walk window on link-drop. Stop-while-UI-alive is OK (Ctrl-C → Kill → pkill).

8. **DRIVE confirm dialogs cite wrong session length**  
   Effective `K1SessionCapSec=300` (`:538`); dialogs still advertise TrackMaxSec/FollowMaxSec
   (2060 / 1260).

9. **`splits.json` ↔ `stats.json` unbound** (prior deferred)  
   `train_act` checks `stats_hash` vs card but not a hash of train run-ids. Hand-edited
   splits can train under stale TRAIN-only norms.

10. **F4 deeper odom degeneracy still open**  
    PAIR-WARN is loud; photometric odom still steps every paired frame including
    duplicate-RGB depths (`desktop/recon/geom.py:180-197`).

11. **F7 residual OOM** — ingest still materializes all depth arrays before median subsample.

12. **Map-assist / Aurora under-tested**  
    Map-change observer has selftests; `_map_assist_confirm` tertiary gate and Aurora
    SUCCEED-only + max-step drop have **no** dedicated `eval/` selftest.

13. **`replay_eval compare` drops depth injection** — `--depth-range` / `--depth-glitch`
    silently no-op in compare mode (`eval/replay_eval.py:359-376`).

14. **Parquet `episode_index` all zeros** — card/`episodes.jsonl` correct; column-trusting
    LeRobot consumers wrong (`eval/batch_ingest.py:136`).

15. **NPU detect has no NMS** — duplicate/overlapping boxes can silently poison masks
    (`npu/infer.py:106-139`).

16. **Mint can exit 0 with `video_backend=png-fallback`** — F5 closes `train_act` only.

17. **Referenced security runbook missing** — `K1Finder.ps1` cites
    `docs/SECURITY_CREDENTIALS.md` (or similar); file absent.

### LOW

18. Sector map-assist can tighten open sectors without the corridor’s secondary
    “live already braking” posture (reduce-only invariant still holds).
19. `LOOP_FITNESS_MIN = 0.30` unused vs live `info[0,0] < 1.0` gate.
20. `camera_gaps.py` hardcodes a Windows `DEFAULT_REPO`.
21. Stale comments (`FollowMaxSec` / `--max-seconds 2000` vs field-night 300).
22. Doc drift: `RUN_ARTIFACTS.md` claims follow/cmd scalars are “per tick” (false for
    TRACK-only obs); `TRAIN_CONTRACT.md` still says S5 DEFERRED while `train_act.py`
    implements it; `LEROBOT_EXPORT.md` path wrong.

---

## REFUTED / holding (positive)

| Claim | Verdict |
|---|---|
| `/cmd/*` TRACK-only teaches drive-while-standing | **REFUTED** — fixed at `_drive_vel` |
| K1Finder UI/logs still show the robot password | **REFUTED** — `abfa425` holds |
| Map assist default-ON aborts DRIVE | **REFUTED** — default OFF; args built at launch only |
| Gap-steer sign still inverted | **REFUTED** — live law + `gap_selftest` / `depth_replay` invariants |
| Map-assist can brake alone or raise clearance | **REFUTED** — confirm-only + `min(live, map)` + tertiary `_lm_fired` |
| Aurora publishes without RELOC SUCCEED | **REFUTED** — gate + max-step drop in code |
| `_try_seed` / `_seed_from` is not the sole `Seed` creator | **REFUTED** — single construction site |
| Operator-session mutex misses Live / Ctrl / Tracker / Control-follow | **REFUTED** — all four own it; Autotune yields |
| One-node rule bypassed by app launchers | **REFUTED** — UI + `run_follow.sh` exit 5 |
| Autotune does robot work during a live K1Finder session | **REFUTED** — loop/stage/pull gates + tests |
| F4/F5/F7/F8 regresssed | **REFUTED** — all four still in tree |
| Secrets leak into `.rrd` / `SUMMARY.txt` | **REFUTED** |
| Autotune carries the robot password | **REFUTED** — key-only |
| API keys / `.env` / private keys committed | **REFUTED** — none found |
| p7 ↔ p8 pin fight | **REFUTED** — shared pins consistent |

---

## Remediation progress (2026-09-11 follow-up)

| # | Item | Status |
|---|---|---|
| — | Offload/Pull password defaults | **DEFERRED** (operator: leave alone) |
| 2 | Every-tick follow/reid/track obs (+ NaN when unlocked; train keeps finite-range only) | **DONE** — `robot/follow_person_k1.py`, `eval/rrd_to_lerobot.py` |
| 3 | RGBD `msg.header.stamp` timeline | **DONE** — `robot/perception.py` `_stamp_wall` |
| 4 | Cluster mid-run lease heartbeat | **DONE** — `cluster/worker.py` (+ selftest) |
| 5 | `UNTETHERED_FOLLOW.md` body rewrite (gate stays closed) | **DONE** |

## Still open (ranked by leverage)

1. **HB-restore / post-kPrepare re-ARM** — stay disarmed until explicit ARM (untethered blocker).
2. **Wire Control DRIVE to the HB path or disable it in UI**; add `ServerAliveCountMax=1`; fix DRIVE dialog caps.
3. **Selftests for Aurora bridge + `_map_assist_confirm`**; bind `splits`→`stats`; odom-on-RGB-distinct keyframes (F4 deeper).

---

## Method note

Council members worked read-only against the live tree. Adjudication preferred
code over docs. On-robot items (live `.rrd` completeness under field profiles,
NTP skew, HW e-stop, Aurora reloc stability on the walking robot) remain
**NEEDS_VERIFY** and were not elevated to CONFIRMED.
