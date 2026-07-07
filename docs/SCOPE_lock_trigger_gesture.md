# SCOPE — Lock-Trigger Abstraction + Gesture Bind for the Booster K1 Follow Node

**Status:** Phases 1 + 2 **IMPLEMENTED (2026-06-29), default-off, preview-only** in `K1Finder/follow_person_k1.py` (backend = YOLO11n-pose per the §7 decision). Compiles clean; the gesture gates (G1–G4), `_marker_choice` replay, `_point_box_dist`, both-mode allocation, and the argparse refusals are unit-tested; an independent adversarial review confirmed the `--lock-trigger aruco` default is byte-identical, identity is still created only via `_try_seed`→`_seed_from`, and the audit is read-only. **NOT YET on-robot validated.** HARD precondition before any `--drive` gesture run: measure YOLO11n-pose per-frame ms on the actual Orin (§6/§7). Phase 4 (ArUco retirement) and Phase 3 (voice) are NOT started.

> **Implementation note (reviewer finding):** the gesture lock point is the owner-box lower-centre, derived from the owner the pose model already pinned by `track_id`. So **G1 (owner-authority by track_id) and G4 (bystander-adjacency) are the OPERATIVE gates**; G2 (containment) and G3 (geometry) are cheap defensive validity checks (degenerate boxes), not the primary wrong-person protection. The §2.1 G2/G3 framing below describes the gate intent; the code comments are annotated accordingly.

> **Skill-validation hardening (2026-06-29 — runtime-safety / policy-engineer / sim-eval passes):**
> - **runtime-safety:** gesture inference now runs ONLY in the STATIONARY acquisition states `{S_SEARCH, S_REACQUIRE, S_PARKED}` — **excluded from `S_SEARCHING`** (the yaw-scan) as well as `S_TRACK`. Never run a heavy model in the control path while driving. Gesture re-acquire after a loss defers to the stood `S_REACQUIRE`.
> - **policy-engineer:** `GestureTrigger` now **warms up** the pose model at load (3 passes, mirrors `ReidEngine`) so the first inference can't trip the overrun-disable, and logs a rolling **`GESTURE-MS p50/p99`** so the on-Orin latency precondition is satisfiable from a preview run. **Precondition reframed:** since pose runs in *no* driving state, the measurement is acquisition-responsiveness + auto-disable tuning, **not a control-loop deadline** — a lower safety bar than §6/§7 originally implied.
> - **sim-eval (the flip gate, §3.7):** the unit of analysis is the **acquisition EPISODE, not the frame**. A new **`GBIND-EPISODES`** line reports `acq_opps` / `acq_bystander` / `seed_agree` / `seed_disagree` — read THAT for the flip decision, not the per-frame `agree_pct`. **`N` for the rule-of-three is `acq_bystander` (bystander-present acquisition opportunities)**, and `N ≈ 3 / acceptable_stranger_rate` — state the rate the bound buys. A new **`GBIND-SEED-DISAGREE`** event covers the **first-seed** wrong-person case (no anchor yet, so the reseed-floor `stranger` metric is blind to it). `seed_disagree` AND `stranger` must both be 0. The corpus MUST sample the adversarial axis (similar-clothing adjacent bystander at seed time) or `stranger==0` is a vacuous pass, and the offline audit is **necessary-not-sufficient** — Phase 4b's supervised soak is the real closed-loop spot-check.

**Original scope (pre-commit):** Read this BEFORE writing any code. Supersedes the five raw design lenses and folds in the adversarial critique — every CRITICAL/HIGH hole from the critic is resolved here, not left open.
**Grounded against (live, not the `_opt_backup_20260623_172956` copy):** `K1Finder/follow_person_k1.py`, `K1Finder/loco_follow_bridge.cpp`, `K1Finder/K1Finder.ps1`, reconciled with `K1Finder/SCOPE_nl_command_layer.md` and `K1Finder/_follow_autonomy/PLAN.md`. All line anchors below were re-verified against the live file at scope time.

This document scopes a **lock-trigger abstraction** that lets a **raised-hand gesture** stand in for the ArUco marker as the *acquisition primitive* that seeds identity. It does NOT touch the control law, the clamps, or the C++ floor. The gesture is a **point/owner producer feeding the single unchanged identity funnel `_try_seed`** — it is never a velocity, never a command, never a second identity-creation path.

One thing up front, because two lenses contradicted each other on it and the critic was right: **`_try_seed` is NOT byte-identical for gesture.** The marker plausibility gate (`:1884-1891`, `fy>=0.25` = upper 75% of the bbox) plus the unconditional nearest-centroid fallback (`:1915-1920`) were tuned for a marker *taped to one person's lower-center torso*; fed a raised-hand-derived point they will admit an adjacent bystander. So `_try_seed` **gains a `point_kind` parameter** (default `"marker"`, preserving ArUco byte-for-byte) and a **stricter gesture branch**. The "byte-identical `_try_seed`, hint_box additive-and-unused, no edit needed" framing from Lens A is dropped. The marker path stays byte-identical; the gesture path gets the gate the marker never needed.

---

## 0. Architecture in words

```
            PERCEPTION (Orin, 10 Hz)                          |   IDENTITY (one funnel only)
                                                              |
  frame ──> det.detect(persons)  (:1721)                       |
        └─> tracker.update(persons)  -> p['track_id']  (:1729)  |
                |                                              |
                v                                              |
   LOCK TRIGGER (selected once at __init__ by --lock-trigger)   |
     ArucoTrigger   = marker_center(frame)            (:234)    |   point_kind="marker"
     GestureTrigger = YOLO11n-pose raised-hand        (NEW)     |   point_kind="gesture"
     CompositeTrigger = ArUco DRIVES, gesture AUDIT-ONLY        |
                |                                              |
                |  emits LockHint(point, owner_box, owner_tid,  |
                |                 source, point_kind) | None    |
                v                                              |
   trigger site (replaces :1734-1739 only)                      |
     mc / marker_streak / marker_locked  -- UNCHANGED math       |
                |                                              |
                v                                              |
   _try_seed(frame, persons, mc, w_img, h_img, point_kind,      |
             owner_box, owner_tid)            (:1878)            |
     · point_kind="marker": :1884-1968 BYTE-IDENTICAL           |
     · point_kind="gesture": STRICTER gate (§2)                 |
     · sticky-lock reseed_anchor_floor :1934-1939  (unchanged)  |
     · Seed(...) constructed ONLY here  (:1940)  <== sole creator|
                |                                              |
                v   (only ever via the unchanged control hook)  |
   target_point_from_box (:340) -> _drive_vel (drive&&walking)   |
   -> C++ HARD clamps + staleness watchdog -> MoveCommand        |
```

**The single load-bearing invariant (inherited from PLAN.md §6):** identity is created/replaced ONLY in `_try_seed` (`Seed(...)` at `:1940`). The gesture trigger supplies a *point and an owner*, exactly as ArUco supplies a point — it never constructs a `Seed`, never calls `feat_fn` for an anchor, never writes `self.seed`. Everything downstream of `_try_seed` — the control law, `target_point_from_box` (`:340`), the 3-layer clamps (`:92-109`, `HARD_VX_LIMIT 0.30` / `HARD_VYAW_LIMIT 0.40`), `_drive_vel` gating on `self.drive && self.walking`, stand-on-loss→kPrepare, the K1F1 wire format — is **textually untouched.** A trigger fault degrades to "no lock this frame," never to motion (fall risk stays perception-side).

---

## 1. The `LockTrigger` abstraction + gesture backend

### 1.1 The contract (already implicit at `:1734-1745`)

Today `_process_frame` produces a lock point `mc = marker_center(frame)` (`:1734`), debounces it into `marker_locked` via `self.marker_streak`/`--seed-frames` (`:1736-1739`), and passes `mc` to `_try_seed` (`:1745`, `:1770`, `:1831`, `:1861`). The whole acquisition surface is already one point. We generalize the *producer* of that point without changing the streak math.

```
LockHint = namedtuple("LockHint", "point owner_box owner_tid source point_kind")
#   point      : (mx, my)        -- the lock point, same role mc plays today
#   owner_box  : (x1,y1,x2,y2)|None -- which person the trigger points at (None for ArUco)
#   owner_tid  : track_id | None -- the tracker id of that person (None for ArUco)
#   source     : "aruco" | "gesture"  -- audit/log
#   point_kind : "marker" | "gesture" -- selects the _try_seed gate branch (§2)

class LockTrigger:
    name = "base"
    ok   = True                  # False => disabled (crash-safe load); caller falls back
    def detect(self, frame, persons, state, w_img, h_img) -> "LockHint | None": ...
```

**`owner_box` / `owner_tid` are authoritative for gesture, not decorative.** This is the critic's HIGH hole #3 fix: the pose model KNOWS which person raised a hand; collapsing that into an anonymous point and re-deriving the person via containment re-introduces exactly the bystander ambiguity the pose model already resolved. For gesture, `_try_seed` uses `owner_tid` as the authoritative candidate (§2.2). ArUco passes `owner_box=None, owner_tid=None` so the marker path re-derives the person exactly as today.

### 1.2 ArUco trigger = today's code, verbatim

```
class ArucoTrigger(LockTrigger):
    name = "aruco"
    ok   = True                              # marker_center is pure-OpenCV, no load step
    def detect(self, frame, persons, state, w_img, h_img):
        mc = marker_center(frame)            # EXACT call from :1734, unchanged
        return None if mc is None else LockHint(mc, None, None, "aruco", "marker")
```

`marker_center` (`:234`) is unchanged. This is the DEFAULT.

### 1.3 The generalized trigger site (replaces `:1734-1741` only)

Today (`:1734-1741`):
```
mc = marker_center(frame)
if mc is not None: self.marker_streak = min(self.marker_streak + 1, 9999)
else:              self.marker_streak = 0
marker_locked = mc is not None and self.marker_streak >= self.a.seed_frames
self._viz_persons = persons
self._viz_mc = mc
```

Generalized (semantically identical when `--lock-trigger aruco`):
```
hint = self._lock_trigger.detect(frame, persons, self.state, w_img, h_img)
mc   = hint.point if hint is not None else None
if mc is not None: self.marker_streak = min(self.marker_streak + 1, 9999)
else:              self.marker_streak = 0
marker_locked = mc is not None and self.marker_streak >= self.a.seed_frames
self._viz_persons = persons
self._viz_mc = mc
self._lock_hint = hint           # NEW: carries point_kind/owner to the _try_seed calls
self._viz_lock_src = hint.source if hint is not None else None   # viz/log only
```

The four `_try_seed` calls (`:1745`, `:1770`, `:1831`, `:1861`) become:
```
if self._try_seed(frame, persons, mc, w_img, h_img,
                  point_kind=(hint.point_kind if hint else "marker"),
                  owner_box=(hint.owner_box if hint else None),
                  owner_tid=(hint.owner_tid if hint else None)):
```

**Byte-identity proof for the default:** with `--lock-trigger aruco`, `self._lock_trigger` is an `ArucoTrigger` whose `detect` returns `LockHint(marker_center(frame), None, None, "aruco", "marker")`. `mc` is then exactly `marker_center(frame)`; the streak/lock lines are character-for-character the same; `point_kind="marker"` selects the byte-identical `_try_seed` branch (§2); `owner_box`/`owner_tid` are `None`. The only additions are a method-call indirection and viz/log strings. No behavior change. **UNMEASURED but argued negligible:** one namedtuple + one method dispatch per frame.

### 1.4 The chosen gesture backend (the one load-bearing OPEN DECISION — see §7)

| Option | Dep | Per-frame cost on the saturated Orin | Reliability | Verdict |
|---|---|---|---|---|
| **(a) YOLO11n-pose, wrist-above-shoulder** | same ultralytics/onnxruntime stack already loaded for YOLO11n-detect | **A SECOND full neural model per frame — UNMEASURED, HARD.** The Orin already logs SLOW-LOOP (`:1615`) and `STALE_PREP_MS` was raised 1000→3000 because the real loop is slower than guessed | Strong: explicit, per-person, low false-trigger | **RECOMMENDED**, compute-gated to acquisition states only (§4) |
| (b) bbox-heuristic (aspect/height change) | none | ~free | Poor: a raised arm barely changes a full-body bbox; cannot localize the limb; cannot honor refuse-on-ambiguity | **Rejected** as primary |
| (c) dedicated hand model (mediapipe) | NEW dep (unconfirmed in BoosterRos2; runtime PC has no Python/Node) | another model + new dep risk | Weak at 2–4 m follow distance | **Rejected** for v1 |

**Chosen: YOLO11n-pose, single raised hand, held N frames.** One wrist keypoint above the same-side shoulder keypoint (COCO-17: 5/6 shoulders, 9/10 wrists; image y grows downward so "above" = smaller y), per-keypoint confidence ≥ `--gesture-kp-conf` (0.5), vertical margin ≥ `MARGIN_PX` (~0.1·bbox_h), held `--gesture-hold` consecutive frames (default 8 ≈ 0.8 s @ 10 Hz). Single hand: most detectable at distance, least awkward, sufficient given the refuse-on-multiple gate.

**The honest compute story (critic CRITICAL hole #1 — the cited failsafe does NOT exist).** Three lenses claimed a "slow-loop auto-disable that mirrors the STAGE4_OSNET deep-reid failsafe." **There is no such failsafe in the live code.** The only slow-loop guard (`:1612-1620`) *logs* `SLOW-LOOP` and *resets* `self._overrun_streak` to 0 — it disables nothing. The STAGE4 auto-disable is a *design directive* in `PLAN.md §3 Stage 4` (line 132), unbuilt. Two consequences, both binding:

1. **The compute backstop for the second neural model is NET-NEW code, not a reuse** — specified in §4.3 with its own counter that must NOT share the `:1614` reset (which fires every 5 frames and can never accumulate to a larger K).
2. **The REAL existing backstop is the C++ bridge staleness watchdog** (`STALE_MS=800` / `STALE_PREP_MS=3000`, `loco_follow_bridge.cpp`): if a slow pose frame stalls the Python loop past the threshold, the bridge safes loco to zero velocity → kPrepare. That is a *stop*, not a graceful degrade. Pose inference is **synchronous on the loop thread** — no lens proposed async — so a slow pose frame is a *wall-clock-late control command*, the actual mechanism by which a second model hurts safety. This is why §4 keeps pose OFF the continuous follow loop entirely and §6 makes a measured per-frame pose-ms on the actual Orin a HARD `--drive` precondition.

### 1.5 The `both` mode (the A/B substrate, audit-only)

`both` runs ArUco as the DRIVING trigger and gesture AUDIT-ONLY: gesture computes its candidate every audited frame and LOGS, but **only ArUco's `mc` reaches `_try_seed`.** Gesture has zero path to identity or motion in `both` mode (mirrors the AUDIT-ONLY relock vote at `:2497-2500` which returns False so identity/drive never change). Detailed in §3.

### 1.6 Selection / construction (mirror `:1531` / the appearance backend bind)

```
_aruco = ArucoTrigger()
if self.a.lock_trigger == "aruco":
    self._lock_trigger = _aruco
else:
    _gest = GestureTrigger(self.a.gesture_model, self.a, self.tracker)   # crash-safe load, §4.1
    if self.a.lock_trigger == "gesture":
        self._lock_trigger = _gest if _gest.ok else _aruco
        if not _gest.ok:
            log("LOCK-TRIGGER gesture requested but model load failed -> ArUco fallback")
    else:  # both
        self._lock_trigger = CompositeTrigger(_aruco, _gest)
```

**The gesture model is constructed ONLY when `--lock-trigger != aruco`.** Under the default it is never loaded, never warmed, never on the 10 Hz path — the load-time half of the off-the-loop guarantee. ZERO new compute at the default.

---

## 2. Gesture → OSNet seed wiring behind `lock_trigger`; ArUco default (item 1)

ArUco default is the only real ArUco prerequisite: item 1 ships `--lock-trigger {aruco,gesture,both}` with `default="aruco"` reproducing today byte-for-byte, so item 4 is later a pure one-token default flip.

### 2.1 `_try_seed` gains `point_kind` — marker byte-identical, gesture stricter (resolves the Lens A vs Lens C CRITICAL contradiction)

New signature, defaults preserve every existing caller:
```
def _try_seed(self, frame, persons, mc, w_img, h_img,
              point_kind="marker", owner_box=None, owner_tid=None):
```

- **`point_kind="marker"` (ArUco, and every today-caller via the default): `:1879-1968` runs BYTE-IDENTICAL.** `_plausible` (`:1884-1891`), the containing-set logic (`:1893-1920`) including the SEED-AMBIGUOUS refuse at `:1909-1914` and the nearest-centroid fallback at `:1915-1920`, the sticky-lock floor (`:1934-1939`), and `Seed(...)` (`:1940`) are untouched. This is the safe-default-reproduces-today guarantee.

- **`point_kind="gesture"`: a stricter pre-selection runs BEFORE the marker logic, then the identity tail (`:1925` onward) is SHARED verbatim.** The gesture branch:

  **(G1) Owner is authoritative, not re-derived (critic HIGH #3).** The candidate is the person with `p['track_id'] == owner_tid`. If no person in `persons` has that id this frame → **NO-SEED** (the gesturer the pose model saw is not associable to a tracked detection; do not guess).

  **(G2) Single-owner containment, nearest-centroid fallback DISABLED (critic HIGH #4).** Require the gesture point to fall inside exactly ONE person box AND that box be the `owner_tid` person. If zero boxes contain the point (a raised-hand-derived lower-center can land above all bboxes when the box is tight) → **NO-SEED**. The `:1915-1920` nearest-centroid branch is **skipped entirely for gesture** — a hand point is far from any body centroid, so nearest-centroid can grab the wrong torso.

  **(G3) Gesture geometry gate replacing `_plausible` (critic CRITICAL #2).** The point is a re-projected *guess* about where the owner's torso is, derived as the owner box lower-center (`mx=(x1+x2)/2`, `my=y1+0.6·bh`). It must resolve to a full standing body: require `fy <= 0.6` and `0.05 <= fx <= 0.95` *within the owner box*, and require the owner box bottom to extend well below the point (a torso beneath the hand). The marker prior (`fy>=0.25`, upper 75%) is NOT reused — it does not discriminate a gesturer's own body from an adjacent bystander for a hand-derived point.

  **(G4) Min-separation-to-second-person gate.** Refuse if any *other* person box edge is within `--gesture-min-sep-frac · diag` of the gesture point — a wave at the boundary between two people is unresolvable → **NO-SEED**. The dedicated bystander-adjacency gate the marker path never needed.

  **(G5) Multi-raiser refuse, never guess (mirrors SEED-AMBIGUOUS `:1912`).** Disambiguation happens inside `GestureTrigger` (§2.3): if ≥2 people have held a raised hand to threshold, the trigger returns `None` (logs `GESTURE-AMBIGUOUS`) and `_try_seed` is never reached. This is the gesture analogue of the `:1909-1914` refuse; combined with G2/G4 there are independent refuse gates and identity is never guessed.

  Then the SHARED tail: `hist = self.feat_fn(frame, chosen["box"])` (`:1925`) records identity via the **same** `feat_fn` (HS or OSNet — "OSNet seed wiring" is automatic: a gesture lock feeds `_try_seed` which calls the configured `feat_fn`/`sim_fn` exactly as a marker lock does), the sticky-lock `reseed_anchor_floor` check (`:1934-1939`) runs **unchanged for gesture re-seeds**, and `Seed(...)` is constructed at `:1940`.

**Invariant preserved, honestly:** identity is still created/replaced ONLY in `_try_seed` at `:1940`. The invariant survives — but ONLY because `_try_seed` gains the `point_kind` branch. The "no edit needed" framing would leave the bystander-adjacency hole wide open and is rejected.

### 2.2 The gesture hold rides the EXISTING tracker id (critic MEDIUM hole #9)

Do NOT build a second per-person association map. Lens A proposed "a small local IoU map, NOT the MultiTracker" — that is a parallel, unhardened association mechanism that can transfer a hold count between two crossing people, manufacturing a "held gesture" for someone who only briefly raised a hand. Instead, **key the gesture hold on the existing `p['track_id']`** already stamped by `tracker.update` at `:1729` (the validated, fault-tolerant Stage-1 association). `GestureTrigger` keeps a `{track_id: hold_count}` dict, incremented while that id's person passes `_raised`, decayed/reset otherwise. **Under `--no-track`, gesture seeding is REFUSED** (require `--track` for gesture) — without a real id the hold cannot be trusted and Lens C's same-track streak debounce is unimplementable. Mid-hold occlusion: if the owner id disappears, its hold count resets (safe, frustrating) rather than coasting (which the IoU-map approach would let a different person inherit). Argparse refuses `--lock-trigger gesture|both` together with `--no-track`.

### 2.3 Debounce — two composed layers, both reusing existing patterns

1. **Per-owner gesture hold** (inside `GestureTrigger`, keyed on track_id per §2.2): a person becomes a "raiser" only at `hold_count >= --gesture-hold`. Filters momentary arm motion (waving past, scratching).
2. **The existing seed streak** (`self.marker_streak` / `--seed-frames`, `:1736-1739`): once the trigger returns a `LockHint`, the SAME streak machinery runs unchanged. A gesture lock therefore requires `--gesture-hold` frames of held hand AND `--seed-frames` frames of a stable returned point. Deliberately conservative — acquisition is rare and deliberate; over-debouncing costs nothing and prevents accidental locks. `--gesture-hold` is kept separate from `--seed-frames` so the two tune independently on the floor.

---

## 3. The gesture-vs-ArUco A/B audit harness + the FLIP criterion (item 2)

Item 2 is a pure observability layer. With `--lock-trigger both`, ArUco DRIVES (`mc` from `marker_center` is the only value reaching `_try_seed`); gesture runs AUDIT-ONLY, computing its candidate every audited frame and logging an A/B comparison. This mirrors the repo's AUDIT-ONLY disciplines verbatim: the `--audit-cosine` per-frame `AUDIT` line and the `RELOC-VOTE-PASS … (audit-only, NOT re-locking)` vote at `:2497-2500` that returns False so identity/drive never change.

### 3.1 Hard dependency on item 1

Item 2 calls the `GestureTrigger.detect` item 1 owns; it cannot be built or produce data before item 1 lands. Its PR can be written against the contract and merged behind `--lock-trigger both` (inert at the `aruco` default) before the detector is performant.

### 3.2 The audit hook (`_process_frame`, after the trigger site, before FSM dispatch at `:1743`)

```
if self.a.lock_trigger == "both":
    self._gesture_audit(frame, persons, mc, w_img, h_img)
```
Gated on `both` so it is inert at the default/`aruco`/`gesture` configs. `persons`, `track_id`s, and `mc` are already computed here; the audit needs exactly these. `_gesture_audit` NEVER calls `_try_seed`, never writes `self.seed`, never calls `_drive_vel` — read-only by construction.

### 3.3 DUPLICATE the selection logic, do NOT refactor `_try_seed` (critic MEDIUM hole #5)

Lens B proposed factoring `:1884-1923` into a shared `_select_person` helper. That touches the sole identity-creation function (the `_plausible` closure reads `mx/my` from the enclosing scope; the SEED-AMBIGUOUS log lives inside it) for an *audit-only* feature — poor risk/reward. **Decision: DUPLICATE the marker selection math into `_gesture_audit` (Lens B's own stated fallback).** Drift risk in a read-only path is far cheaper than any behavior change to the sole Seed creator. If a shared helper is ever wanted, it is a separate, independently validated refactor gated by a recorded-frame diff of `LOCKED`/`SEED-*` lines — never bundled with this feature.

### 3.4 The audit must REPLAY the reseed-floor (critic HIGH hole #6 — the flip gate was measuring the wrong quantity)

The flip gate's headline metric is "stranger == 0." But in `both` mode the gesture point never reaches `_try_seed`, so the **sticky-lock `reseed_anchor_floor` check (`:1934-1939`) — the production stranger defense — is never exercised on a gesture candidate.** A raw track_id disagreement is NOT a stranger-seed: a different-id gesture pick might be REJECTED by the floor in production (benign). The audit therefore systematically mis-estimates the real safety property.

**Fix:** when ArUco has a live `Seed`, the audit computes `sim_fn(self.seed.anchor_hist, feat_fn(frame, gesture_owner_box))` and logs whether the gesture candidate **WOULD PASS or FAIL** the `:1934-1939` floor. Count a **stranger ONLY when gesture picks a different identity AND that identity WOULD PASS the floor** (i.e. the floor fails to catch it). A different-id that the floor would reject is not a stranger-seed. This makes the audit measure the production-relevant event. (This adds one `feat_fn` call per audited frame when a Seed is live — folded into the `--gesture-every-n` decimation, §3.6.)

### 3.5 Metrics + log format (house style: one flushed `log()` line, UPPERCASE tag, key=value)

Per-frame, emitted only when `gp is not None or mc is not None` and throttled to ≥0.5 s (mirror the SEARCH/PARKED heartbeats), so idle SEARCH/PARKED stretches do not flood the SSH log channel `K1Finder.ps1` reads:
```
GBIND g=Y gtid=17 greason=contain1 gstreak=3 | a=Y atid=17 areason=contain1 astreak=5 \
      | agree=Y ftrig=n miss=n stranger=n floorpass=n tid=on
```
- `agree` = same track_id when both chose (centroid-distance fallback tagged `tid=off` under `--no-track`; flip-gate sessions must use `--track`).
- `ftrig` = gesture chose while ArUco point absent and not stable-locked (false-acquire risk).
- `miss` = ArUco stable-locked but gesture produced no owner this frame (benign).
- `stranger` = gesture chose a different identity **that would pass the reseed-floor** (§3.4) — the safety-critical event.

Per-session rollup at clean shutdown (hook the `run()` finally / `_cleanup` path, same place PARKED clean-exit logs):
```
GBIND-SESSION frames=4120 active=512 agree=489 disagree=23 g_only=11 a_only=8 \
  stranger=0 agree_pct=95.5 ftrig_pct=2.1 miss_pct=1.6 g_ttl=0.42s a_ttl=0.38s \
  bystander_present=Y track=on
```
`stranger` is an **absolute count, not a rate.** This single grep-able line (`grep GBIND-SESSION`) is what the operator and the flip gate read.

### 3.6 Decimation (critic compute defense)

`--gesture-every-n` (default 3, mirror `--reid-every-n`) so even a moderately expensive pose model in audit mode cannot starve the live ArUco control loop — in `both` mode ArUco is still the one actually driving. The cadence is recorded in `GBIND-SESSION` so agreement rates are interpreted against it.

### 3.7 The FLIP criterion (the precise gate for item 4)

Flipping `--lock-trigger` default from `aruco` to `gesture` is authorized **only** when, across the floor-session corpus, ALL hold:

1. **N sessions with `--lock-trigger both` spanning the operating envelope** — ≥2 operators, indoor + intended outdoor/lighting, and ≥3 sessions with a bystander in frame (the stranger metric has no power without distractors present). **N is set by the rule-of-three (critic missing-consideration #6), not picked arbitrarily:** zero stranger events in N opportunities bounds the true rate only to ~3/N at 95%. N=10 bounds it to ~30% *per opportunity* — wildly insufficient for a wrong-identity event. The corpus must be sized so the upper confidence bound on the stranger rate is acceptable for an identity-safety event; this is a Modd decision (§7) and the bound, not a round number, is the gate.
2. **`stranger == 0` across ALL sessions** — absolute, non-negotiable, not subject to calibration. A single frame where gesture would have seeded a different identity that the floor would NOT catch, with a bystander present, is a HARD fail. Matches the repo's "zero impostor admissions" bar (PLAN.md §2 Stage 2 preview validation).
3. **`agree_pct >= 90%`** median, no session < 80% (measured only on frames where both chose).
4. **`ftrig_pct <= 5%`** median, no session > 10%.
5. **`miss_pct <= 15%`** median (a gesture miss is benign — hold the pose longer or fall back to ArUco).
6. **`g_ttl <= a_ttl + 1.0s`** median (gesture may be slightly slower to stable-lock, not workflow-changingly so).

**Thresholds 3–6 are PROVISIONAL and UNMEASURED** — there is no gesture data yet. Calibrate against the first 2–3 sessions, then freeze, exactly as `STALE_PREP_MS` was raised 1000→3000 once the real loop proved slower than guessed and `--anchor-floor` is tuned via `--audit-cosine`. **`stranger == 0` and the rule-of-three corpus sizing are fixed from day one.** The gate is a manual review of `GBIND-SESSION` lines, not an automatic flip.

---

## 4. The voice suite reconciliation (item 3 — independent, parallelizable; defer to `SCOPE_nl_command_layer.md`)

**Item 3 needs NO new design. It is the front-end half of the Tier-1 NL command layer already scoped in `SCOPE_nl_command_layer.md`.** Voice/ASR replaces only the parser's *input modality*: `audio → ASR text → the existing deterministic keyword grammar → the SAME frozen 6-member enum (STOP/HOLD/RESUME/FOLLOW/PARK/STATUS) → the SAME ARM/confirm gate → the SAME watched-file channel /tmp/k1_cmd → the same per-tick non-blocking drain.` Build it per that doc, do not redesign it.

**Why it is orthogonal to items 1/2/4 (different channel, different invariant, no shared mutable state):**
- **lock_trigger (items 1/2/4)** changes WHICH trigger feeds `_try_seed` — it touches identity creation. Its invariant: identity created/replaced only in `_try_seed` (`:1940`).
- **voice/command (item 3)** changes nothing about identity. Per `SCOPE_nl_command_layer.md §4 invariant 2`: "NL never creates identity. `_try_seed` (marker) is the ONLY Seed creator. The enum carries no target descriptor; 'follow that person' is impossible."  Its invariant (`§4.1`): the brain never emits velocity.
- No collision: item 3 reads/writes `/tmp/k1_cmd`, `commanded_hold`, FSM state, and the ARM credential; it never calls `marker_center`, `GestureTrigger`, `_try_seed`, or `feat_fn`. Items 1/2/4 never read `/tmp/k1_cmd` or `commanded_hold`.

**The honest caveat (critic LOW hole #10):** the "zero shared state" claim is slightly overstated — the commanded-HOLD latch shares `_stand()`/`_hold()` and `self.state` with the FSM, and `SCOPE_nl_command_layer.md` cites the drain seam as "top of `run()`, after `take_if_new` (~`:1417`), before `_process_frame`." That `:1417` anchor is from the SCOPE doc's grounding pass, not re-verified in this pass against the live `run()` (whose loop tail sits at `:1605-1623`). **Action for item 3 (in its own PR, not this one):** verify the actual `run()` drain seam and confirm `commanded_hold` is checked by the SAME code path that gates `_stand`/`_hold` in REACQUIRE/PARKED, so a commanded HOLD and an FSM-driven stand cannot fight. This is Tier-1 scope per the NL SCOPE doc and genuinely low-risk; it does not block items 1/2/4.

**The one reconciliation touch-point — FOLLOW stays trigger-agnostic.** `SCOPE_nl_command_layer.md §2.1` says FOLLOW "re-arms the marker hunt" (forces `S_SEARCH`, resets `marker_streak`). Once gesture lands, this reads: **FOLLOW re-arms whatever trigger is the active `--lock-trigger` default; it does not name or select a trigger.** FOLLOW must carry no trigger arg in the enum, never call `_try_seed` directly (it only sets state), and the active `--lock-trigger` default decides the producer of `mc`. A one-line doc reconciliation in the NL SCOPE, not a redesign. This keeps the command surface saying "go hunt for your lock" while lock_trigger owns "which hunt."

**Stop-keyword pre-filter (the one voice-specific safety rule, restating `SCOPE_nl_command_layer.md §4.4`):** the de-escalation pre-filter runs on the ASR *transcript text*, NEVER on ASR confidence — a low-confidence recognized "stop" must still stop. Identical text-precedence to the typed grammar.

**Compute:** item 3 adds ZERO new per-frame Orin work (one non-blocking file stat+read+truncate per tick, microseconds); ASR runs OFF-robot, episodic, host-side (the runtime PC has no Python/Node, so ASR is a deferred external service or OS-level dictation into the existing K1Finder typed box).

---

## 5. ArUco retirement gate + staged deprecation (item 4)

ArUco retirement is a **default flip plus a soak, not a deletion.** ArUco contributes exactly one thing today: a lock point feeding `_try_seed`. "Retire" = stop using the marker as the *default* lock-point producer, while the lock-point contract and everything downstream stay unchanged.

### 5.1 The gate (G-FLIP) = the §3.7 FLIP criterion passed, plus integration preconditions

In addition to §3.7 (1–6), all conjunctive:
- **Re-seed identity floor proven for gesture.** A gesture re-seed onto a different person must be rejected by `:1934-1939` exactly as a marker re-seed is. Add a regression demonstrating the `SEED-REJECT identity-mismatch` log fires for a gesture re-seed onto a different person. **NOTE the §4.2 scope correction below:** because gesture does NOT run in S_TRACK, there is no gesture re-seed of an *active* lock — this floor is exercised only when gesture re-seeds from an acquisition state, and the audit replay (§3.4) is what proves the floor's behavior pre-flip.
- **Compute headroom confirmed.** The measured per-frame pose-ms (item 1 precondition, §6) does not push the loop into sustained overrun alongside YOLO11n + OSNet. UNMEASURED until item 1 lands; a precondition of even reaching G-FLIP.
- **Fallback proven live.** An explicit `--lock-trigger aruco` run on the flip build reproduces today byte-for-byte before the default depends on gesture.

### 5.2 Staged deprecation (each sub-phase independently revertible)

**Phase 4a — flip the default, keep ArUco as explicit fallback.** The ONLY change: `default="aruco"` → `default="gesture"` on the `--lock-trigger` arg. `marker_center` (`:234`) and the four marker branches (S_SEARCH `:1744`, S_SEARCHING `:1770`, S_REACQUIRE `:1831`, S_PARKED `:1861`) stay live as an always-available recovery/manual re-seed. **Critic MEDIUM hole #7 — the flip must NOT be a silent field-wide modality change:** before 4a, enumerate every launch path in `K1Finder.ps1` (+ any `.vbs`/`.bat` shims) and make the GUI pass `--lock-trigger` **EXPLICITLY on every launch**, so there is no implicit default in the field at all and the active trigger is always operator-visible. Then "flipping the default" is moot for live operators. The flip must not be conflated with relaxing the untethered/deadman safety gate (which remains FORBIDDEN per memory `untethered-follow` / `k1-hardware-backstop`).

**Phase 4b — soak (gesture default, ArUco one keystroke away).** A pre-declared window (size set with the rule-of-three discipline, §3.7/§7). Exit criteria: zero gesture false-seeds; no gesture-caused operator reverts; SLOW-LOOP overrun rate with gesture-default not worse than the ArUco-default baseline. A single wrong-identity seed resets the soak.

**Phase 4c — demote ArUco from default-trigger to permanent degraded-mode/emergency re-seed fallback.** Mostly docs + flag-semantics: update PLAN.md, this SCOPE, and `K1Finder.ps1` help. **DELETE NOTHING load-bearing.** Do not remove `cv2.aruco`, `marker_center`, or the four marker branches — they are the only zero-ML, zero-GPU seed path, exactly the recovery you need when gesture/OSNet/the saturated Orin is the thing that failed. Keep marker recovery ACTIVE by default under gesture (defense-in-depth); add a `--no-marker-recovery` opt-out, not an opt-in.

**Critic missing-consideration #3 (logistics, stated honestly):** "Keep `marker_center` forever" is only a real fallback if operators still **carry a marker** after the flip. That is a training/ops commitment, not a code fact — the field runbook must require operators to carry a marker even when gesture is default, or the zero-ML recovery is unavailable in exactly the degraded conditions it exists for.

**Critic missing-consideration #4 (degraded-state honesty):** if `--lock-trigger gesture` is set, the pose model fails to load, AND no marker is in scene, the honest state is **NO SEED POSSIBLE** — the robot cannot be commanded to follow. Log this loudly at startup (`ARM-REFUSED`-style), do not silently "fall back to ArUco" and pretend a marker path exists when there is no marker.

### 5.3 Reversibility

One-line `default=` change, OR (no redeploy) `--lock-trigger aruco` per launch, OR — once §5.2 4a makes the GUI pass the flag explicitly — a one-click K1Finder GUI toggle. No state migration: both triggers produce the same `(mx,my)` and call the same `_try_seed`; a live `Seed`/anchor is identical regardless of trigger, so flipping back between sessions is safe.

---

## 6. Phased implementation plan (dependency order 1 → 2 → 4; 3 parallel)

```
ITEM 1  Gesture trigger behind --lock-trigger, ArUco DEFAULT
   |  HARD precondition: measure YOLO11n-pose per-frame ms on the ACTUAL Orin
   |  (/proc/device-tree/model; nvpmodel -m 0 + jetson_clocks) alongside YOLO11n+OSNet.
   |  GATE G1: --preview then --drive; aruco-default reproduces today byte-for-byte;
   |           gesture seeds correctly in isolation; loop does not sustain-overrun.
   v
ITEM 2  A/B audit (both-mode, audit-only; replays the reseed-floor per §3.4)
   |  GATE G-FLIP (== §3.7 + §5.1): authorizes Phase 4a.
   v
ITEM 4a Flip default to gesture; ArUco kept as explicit fallback; GUI passes trigger explicitly
   |  GATE G-SOAK-ENTER: fallback proven; revert one-click.
   v
ITEM 4b SOAK -> GATE G-RETIRE -> ITEM 4c Demote ArUco (docs only; delete nothing load-bearing)

ITEM 3  VOICE SUITE -- INDEPENDENT, fully parallel. Feeds the frozen command enum
        (SCOPE_nl_command_layer.md), never _try_seed. Build per that doc; do not redesign.
```

### Phase 1 — `LockTrigger` abstraction + `GestureTrigger`, ArUco default

**New argparse flags (SAFE defaults reproduce today exactly):**
| Flag | Default | Effect |
|---|---|---|
| `--lock-trigger {aruco,gesture,both}` | **`aruco`** | unset = today byte-identical; gesture object not even constructed |
| `--gesture-model PATH` | `yolo11n-pose.onnx` | only loaded when `--lock-trigger != aruco`; load fail → ArUco fallback |
| `--gesture-hold N` | `8` | consecutive frames a raised hand must persist (keyed on track_id) |
| `--gesture-kp-conf F` | `0.5` | min per-keypoint confidence for the wrist/shoulder test |
| `--gesture-min-sep-frac F` | tune | min separation (·diag) to a second person; G4 refuse |
| `--gesture-overrun-frames K` | `≥5` | NEW auto-disable threshold (§4.3); independent of the `:1614` counter |
| `--gesture-every-n N` | `3` (audit only) | decimate pose inference so it cannot starve the control loop |

Argparse REFUSALS (mirror the ARM-refused discipline): refuse `--lock-trigger gesture|both` with `--no-track` (§2.2); refuse `--lock-trigger gesture|both` with `--reseed-anchor-floor 0` (would disable the sticky-lock stranger defense).

**Files / functions / line-anchors touched (`follow_person_k1.py`):**
- **NEW** classes `LockTrigger`/`ArucoTrigger`/`GestureTrigger`/`CompositeTrigger` + `LockHint` namedtuple, placed near `marker_center` (`:234`).
- **`Follower.__init__`** (near the detector/appearance bind ~`:1531`): construct `self._lock_trigger` per §1.6. Gesture model built ONLY when `--lock-trigger != aruco`.
- **`_process_frame`** `:1734-1741`: replace the marker-block with the generalized trigger site (§1.3); stash `self._lock_hint`.
- **`_process_frame`** `:1745`, `:1770`, `:1831`, `:1861`: pass `point_kind`/`owner_box`/`owner_tid` from `self._lock_hint`.
- **`_try_seed`** `:1878`: add the three params (defaults preserve marker callers); add the gesture pre-selection branch G1–G5 (§2.1) BEFORE `:1893`; the identity tail `:1925-1968` is SHARED and the marker path is byte-identical.
- **`run()`** `:1609-1623`: add the NET-NEW gesture overrun auto-disable beside (not sharing) the SLOW-LOOP guard (§4.3).
- **`_stream_frame`** `:1641-1714`: add `self._viz_lock_src` to the banner only (K1F1 magic/status/len/jpeg unchanged).

**§4.1 crash-safe load (mirror `PersonDetector` / `ReidEngine`):** `GestureTrigger.__init__` wraps `YOLO(model_path, task="pose")` in try/except → `ok=False` on failure → §1.6 falls back to ArUco + logs. `detect()` is fully try/except-wrapped → returns `None` on any per-frame fault (fail-closed). The outer `_process_frame` try/except is the final backstop.

**§4.2 off the 10 Hz path — gesture does NOT run in S_TRACK (resolves critic MEDIUM hole #8).** Two layers: (a) the model is not even constructed under `aruco`; (b) even when gesture is selected, `GestureTrigger.detect` runs ONLY in the acquisition states `S_SEARCH/S_SEARCHING/S_REACQUIRE/S_PARKED` (the four that call `_try_seed`), NEVER in `S_TRACK`. The lenses wanted it both ways (gesture re-seed during an active lock vs. compute); **the resolution: gesture does NOT run in S_TRACK, therefore there is NO gesture re-seed of an active lock by construction.** The "bystander steals an active lock via gesture" case is closed by the state-gate, NOT by the reseed-floor — so PLAN.md/this doc cite the **state-gate** as that defense, not the floor. To gesture-re-seed, the operator must HOLD or lose the lock first, re-entering an acquisition state. `marker_center` may still run cheaply every frame; gesture does not.

**§4.3 compute auto-disable (NET-NEW, not a mirror — critic CRITICAL hole #1).** Add `self._gesture_overrun_streak`, incremented when `dt > period` WITH gesture inference active in an acquisition state, independent of and NOT resetting on the `:1614` reset. At `K = --gesture-overrun-frames`, set `self._lock_trigger.ok = False` → ArUco fallback (or NO-SEED if `--lock-trigger gesture` and no marker) and log `GESTURE-DISABLED-SLOW`. Bounds the worst case to today's compute, not to a missed deadline. The REAL hard backstop remains the C++ bridge staleness watchdog (§1.4) — this auto-disable exists so we degrade *before* the watchdog has to safe the robot.

**Preview-before-drive validation (G1):** `--preview --stream --lock-trigger gesture --track`. Confirm: a single raised hand held ~0.8 s seeds the gesturer (not a neighbor); two raisers → `GESTURE-AMBIGUOUS` refuse; a hand point above all bboxes → NO-SEED; a wave at a two-person boundary → NO-SEED; `--lock-trigger aruco` reproduces today's `LOCKED`/`SEARCH` logs byte-for-byte; watch `SLOW-LOOP` and `GESTURE-DISABLED-SLOW`. Only after clean `--preview` and a measured pose-ms within budget does `--drive` run.

### Phase 2 — A/B audit harness (depends on Phase 1; gates Phase 4)

**New flags:** reuses `--lock-trigger both` and `--gesture-every-n` from Phase 1.
**Files / anchors:** **NEW** `_gesture_audit` (duplicated selection math per §3.3 + reseed-floor replay per §3.4); audit hook in `_process_frame` after the trigger site, before `:1743`; `GBIND` per-frame line (throttled) and `GBIND-SESSION` rollup hooked into the `run()` finally / `_cleanup` path (`:1628-1638`). No change to `_try_seed`, `_drive_vel`, or any control surface.
**Preview-before-drive validation:** run `--lock-trigger both` in `--preview` first to confirm `GBIND` lines populate and ArUco still drives unchanged; only then `both --drive --track` for the corpus (gesture stays audit-only even under `--drive`). Gate the flip on a manual `GBIND-SESSION` review against §3.7.

### Phase 4 — ArUco retirement (depends on Phase 2 G-FLIP)

**Flag change (4a):** `--lock-trigger` `default="aruco"` → `default="gesture"`; make `K1Finder.ps1` pass `--lock-trigger` explicitly on every launch (`§5.2`). **4c:** docs/flag-semantics only; `--no-marker-recovery` opt-out added; nothing load-bearing deleted.
**Preview-before-drive validation:** before 4a, an explicit `--lock-trigger aruco --preview` run on the flip build reproduces today byte-for-byte (the fallback you are about to depend on must be exercised). 4b soak is itself the staged `--drive` validation with one-click revert.

### Phase 3 — Voice suite (independent, any time)

Per `SCOPE_nl_command_layer.md §5 Phase 1` ordering: STATUS+HOLD typed first, then PARK/STOP, then ARM-gated RESUME/FOLLOW, then the K1Finder typed box + grammar; voice/ASR is its Phase 2 host-side front-end. Reconcile the one FOLLOW line (§4). **Validation:** the NL SCOPE's own per-command ACK round-trip (`CMD <word> applied`) over SSH, before any motion token. No trigger dependency.

---

## 7. The single load-bearing OPEN DECISION (for Modd)

**Which gesture-detection backend, and does its measured per-frame cost fit the saturated-Orin 10 Hz budget in acquisition states?**

This is the one decision everything else hinges on, and it is HARD and UNMEASURED. There is NO pose/keypoint model loaded anywhere in `follow_person_k1.py` today (only YOLO11n-detect + optional OSNet ReID). A YOLO11n-pose model is a **second full neural inference per frame**; the Orin already logs `SLOW-LOOP` overruns (`:1615`) and `STALE_PREP_MS` was raised 1000→3000 because the real loop is slower than the blind guess. The synchronous pose call delays the whole tick — a 200 ms pose frame is a 200 ms-late control command, and past the C++ watchdog it becomes a zero-velocity stop.

**Recommendation:** YOLO11n-pose (option (a)), single raised hand, held 8 frames, keyed on the existing tracker id — **but kept `--lock-trigger aruco` (preview-only for gesture) until measured.** As a HARD precondition of any `--drive` gesture run (mirroring PLAN.md §4 / §7 for OSNet): confirm `/proc/device-tree/model`, then measure YOLO11n-pose per-frame ms on the actual Orin under `nvpmodel -m 0 + jetson_clocks`, alongside YOLO11n + OSNet, in the four acquisition states only. Export to ONNX-CUDA first to validate behavior; TRT FP16 only if the budget demands it. If pose does not fit even in acquisition states, gesture stays preview-only and ArUco remains the default indefinitely — that is an acceptable outcome, not a failure.

**Secondary decisions (recommendations stated, but downstream of the above):**
- **A/B corpus size N (the rule-of-three).** `stranger == 0` over N opportunities only bounds the true stranger rate to ~3/N. Pick N (and the bystander-session count) from the upper confidence bound you will accept for a wrong-identity-seed event, not a round number. Recommend sizing so the 95% bound is well below any plausible operational tolerance; any single wrong-identity seed resets the clock.
- **Flip thresholds 3–6 (§3.7).** Ship as PROVISIONAL defaults, calibrate against the first 2–3 sessions, freeze. `stranger == 0` and the rule-of-three corpus are fixed from day one.
- **Marker recovery under gesture default.** Recommend ACTIVE by default (`--no-marker-recovery` opt-out), and a field-runbook commitment that operators carry a marker even when gesture is default — otherwise the zero-ML fallback is unavailable in the degraded conditions it exists for.

---

**Sacred invariants — confirmed untouched (re-verified against live code):** `_try_seed` remains the SOLE identity creator (`Seed(...)` at `:1940`); the marker path through `_try_seed` is byte-identical via `point_kind="marker"`; control law + 3-layer clamps (`:92-109`, `HARD_VX 0.30` / `HARD_VYAW 0.40`), `target_point_from_box` (`:340`), `_drive_vel` gating on `drive && walking`, stand-on-loss→kPrepare, K1F1 wire format, the C++ HARD clamps + staleness watchdog — none are in any code path this design touches. The trigger runs in perception only; a trigger fault degrades to "no lock," never to motion. The untethered/deadman safety gates (memory: `untethered-follow`, `k1-hardware-backstop`) are orthogonal and unchanged — the default flip must never be conflated with relaxing them.
