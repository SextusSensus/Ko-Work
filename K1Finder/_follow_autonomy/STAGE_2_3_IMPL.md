# K1 Follow Autonomy — Stage 2 + Stage 3 Implementation Plan

**Target file:** `K1Finder/follow_person_k1.py` (real, **1649 lines**, post-Stage-1)
**Authority note:** All line anchors below are against the **real post-Stage-1 file**, not `PLAN.md` (whose anchors predate Stage 1). Produced by a 3-lens workflow (gallery / reloc / integration) → adversarial correctness+safety critique → synthesis; every must-fix from the critics is folded in.

---

## 1. Overview & dependency on Stage 1

### What exists now (verified in the real file)
- **`MultiTracker`/`Track`** (L377–490): pixel-space KF, monotonic ids that can never inherit a retired id (L434–435, L480–482). `iou_xyxy` at **L347**.
- **`color_hist`** (L496) returns a 30×32 HS hist or **None**; **`hist_similarity`** (L515) returns `[0,1]`, `0.0` on `None`.
- **`Seed`** (L739–756): `hist` (EMA working copy), `anchor_hist` (immutable frozen anchor, L747), plus Stage-1 `track_id/track_state/confidence/miss_streak/pred_centroid`.
- **`_associate`** (L1246–1296): 6-tuple `(best, cost, sim, second_cost, best_hist, track_locked)`. Anchor veto at **L1270** (`use_anchor = anchor_hist is not None and anchor_floor > 0.0`, L1262). Track-id preference block L1287–1293. Cost color term uses **`seed.hist`** (the EMA copy), L1273.
- **`_track`** (L1298–1418): coast attempt L1323, hard-loss→`S_REACQUIRE` L1328–1339, accept path from L1354, EMA block **L1379–1389** where `diag`/`jump` are defined **only inside `if sim >= hiconf`**.
- **`_process_frame`** (L1105): persons+tracker+marker; `S_REACQUIRE` branch **L1143–1161** (`_stand()`/`_hold()` L1147–1150, marker block L1151–1154, timeout log L1155–1160).
- **`_try_coast`** ends L1482; `_range_for` at L1485; `_stream_frame` REACQUIRE viz **L1084–1086**; `parse_args` L1521+ (Stage-1 flags L1581–1612).

### What these stages add
- **Stage 2 (gallery + distractor bank):** turns the single frozen `anchor_hist` veto into a **slot-0 AND-veto** plus a bounded same-person gallery that *only lowers cost* and a bounded distractor bank that *only raises cost*. Closes **failure mode B** (turnaround/lighting). No new model.
- **Stage 3 (passive markerless re-acquire):** in `S_REACQUIRE`, while STOOD, run a strict read-only vote to re-lock the same person without a marker. **Ships OFF** on HS and **lands with re-lock HARD-DISABLED (log-only audit) on HS**; the re-lock arm enables only when Stage-4 embeddings exist.

### Build/ship order (hard sequence)
1. **Stage 2 split into commits 2a/2b/2c**, gallery-first then distractor-bank (§6). Fully `--preview` validated **before** any `--drive`.
2. **Stage 3 lands last**, only after Stage 2 is preview-clean, and **only as a passive, log-only audit harness on HS** (re-lock arm gated behind an internal `_relock_armed` flag that is `False` on HS).

Rationale: Stage 3's only safe HS configuration is OFF; its sole identity-rebinding path is the falling-robot-critical one. Collecting audit data with zero behavioral change is the high-value subset.

---

## 2. Stage 2 — gallery + distractor bank  ✅ IMPLEMENTED (2026-06-25)

> **Status:** landed in `follow_person_k1.py`. All §2 items done — `import collections`, `max_iou_other`, `TargetGallery` (slot-0 AND-veto, isolation-gated `admit`/`add_distractor`, crash-safe), `Seed.gallery`, `Follower.feat_fn/sim_fn/_frame_idx`, `_frame_idx++`, `_try_seed` gallery init, the `_associate` slot-0 AND-veto + bounded cost bonus, the hoisted `diag`/`jump` + isolation-gated banking in `_track`, and flags `--gallery-size/--distractor-size/--bank-floor/--admit-conf/--admit-spacing/--bbox-iou-isolate/--w-gallery/--w-distractor`. `--gallery-size 1` reproduces Stage 1. **Not yet preview-validated on the robot.**

### 2.1 `import collections`
Add to the stdlib block (~L62–70): `import collections`.

### 2.2 `max_iou_other` — bbox-isolation helper (NEW, module-level near `iou_xyxy` ~L361)
**MUST-FIX:** exclude self by **index**, not tuple identity (a copied tuple silently yields IoU=1.0 self-match and disables isolation).

```python
def max_iou_other(persons, idx, self_track_id=None):
    """Max IoU of persons[idx]['box'] vs every OTHER person's box. Self-excluded
    by INDEX. When self_track_id given, also skip persons sharing it (split/dup
    detections of the SAME person). 0.0 with no others. Fault -> 1.0 (NOT isolated)."""
    try:
        box = persons[idx]["box"]
        m = 0.0
        for j, q in enumerate(persons):
            if j == idx:
                continue
            if self_track_id is not None and q.get("track_id") == self_track_id:
                continue
            m = max(m, iou_xyxy(box, q["box"]))
        return m
    except Exception:
        return 1.0   # fail safe: treat as NOT isolated -> no admit/bank
```
A candidate is **isolated** iff `max_iou_other(...) < a.bbox_iou_isolate`.

### 2.3 `TargetGallery` class (NEW, after `MultiTracker.get` ~L491, before `color_hist` L496)
Slot-0 = the FROZEN anchor, held **separately** from the bounded deque so it can never be evicted. `gallery` only LOWERS cost; `distractors` only RAISE cost. `feat_fn`/`sim_fn` are the ONLY Stage-4 swap point. Every method is crash-safe.

```python
class TargetGallery:
    def __init__(self, anchor_feat, feat_fn, sim_fn,
                 gallery_size, distractor_size,
                 anchor_floor, bank_floor, admit_conf, admit_spacing):
        self.anchor_feat = anchor_feat          # may be None; tolerated
        self.feat_fn = feat_fn
        self.sim_fn = sim_fn
        self.gallery = collections.deque(maxlen=max(0, gallery_size - 1))
        self.distractors = collections.deque(maxlen=max(0, distractor_size))
        self._anchor_floor = float(anchor_floor)
        self._bank_floor = float(bank_floor)
        self._admit_conf = float(admit_conf)
        self._admit_spacing = int(admit_spacing)
        self.last_admit_frame = -10**9

    def anchor_pass(self, feat):
        """Slot-0 HARD gate. Replicates L1262 disable path: True when veto
        disabled or anchor missing."""
        try:
            if self.anchor_feat is None or self._anchor_floor <= 0.0:
                return True
            return self.sim_fn(self.anchor_feat, feat) >= self._anchor_floor
        except Exception:
            return False   # feature fault -> caller vetoes -> degrade safe

    def score(self, feat):
        """Return (anchor_sim, g_sim, d_sim). anchor_sim FIRST and SEPARATE so the
        caller applies the AND-veto on anchor_sim ALONE (never on g_sim).
        g_sim = max(anchor_sim, gallery sims) >= anchor_sim."""
        try:
            a = self.sim_fn(self.anchor_feat, feat) if self.anchor_feat is not None else 0.0
            g = a
            for v in self.gallery:
                s = self.sim_fn(v, feat)
                if s > g:
                    g = s
            d = 0.0
            for v in self.distractors:
                s = self.sim_fn(v, feat)
                if s > d:
                    d = s
            return a, g, d
        except Exception:
            return 0.0, 0.0, 0.0

    def admit(self, feat, conf, jump, ema_max_jump, isolated, frame_idx):
        """Append a same-person view. NEVER touches anchor_feat. Isolation is
        necessary, NOT sufficient."""
        try:
            if feat is None or not isolated:
                return False
            if conf < self._admit_conf:
                return False
            if jump > ema_max_jump:
                return False
            if (frame_idx - self.last_admit_frame) < self._admit_spacing:
                return False
            if self.anchor_feat is not None and self.sim_fn(self.anchor_feat, feat) < self._bank_floor:
                return False
            if self.gallery.maxlen == 0:      # --gallery-size 1 -> anchor-only
                return False
            self.gallery.append(feat)
            self.last_admit_frame = frame_idx
            return True
        except Exception:
            return False

    def add_distractor(self, feat, isolated):
        """Bank a confidently-other-person view. MUST require sim < anchor_floor
        (NOT just < bank_floor) so a color-shifted target view still clearing the
        follow-veto can never be banked against itself. Caller ALSO guards on
        track_id != seed.track_id."""
        try:
            if feat is None or not isolated:
                return False
            if self.distractors.maxlen == 0:
                return False
            if self.anchor_feat is not None:
                s = self.sim_fn(self.anchor_feat, feat)
                if s >= self._bank_floor or s >= self._anchor_floor:
                    return False
            self.distractors.append(feat)
            return True
        except Exception:
            return False
```

### 2.4 `Seed` addition (extend `Seed.__init__`, L739–756)
Add one field: `self.gallery = None` (a `TargetGallery|None`, built in `_try_seed`). `hist`/`anchor_hist` unchanged.

### 2.5 `Follower.__init__` additions (~L766–800)
- `self.feat_fn = color_hist`
- `self.sim_fn = hist_similarity`  (the **single** Stage-4 swap site)
- `self._frame_idx = 0`  (monotonic processed-frame counter)

### 2.6 `_process_frame` top (~L1109, after `persons = self.det.detect(frame)`)
`self._frame_idx += 1`. Persons/tracker/marker logic unchanged.

### 2.7 `_try_seed` slot-0 freeze (after L1228–1229, the frozen-anchor preservation block)
```python
if self.a.gallery_size > 1:
    self.seed.gallery = TargetGallery(
        self.seed.anchor_hist, self.feat_fn, self.sim_fn,
        self.a.gallery_size, self.a.distractor_size,
        self.a.anchor_floor, self.a.bank_floor,
        self.a.admit_conf, self.a.admit_spacing)
else:
    self.seed.gallery = None     # ROLLBACK: --gallery-size 1 == Stage-1
```
Slot-0 is the SAME frozen anchor across re-seeds → identity still created/replaced ONLY here. **sacred_safe.**

### 2.8 `_associate` edit — slot-0 AND-veto, gallery LOWERS cost (L1262–1279)
**MUST-FIX (rollback equivalence):** the cost color term **stays on `seed.hist`** (the existing L1273 EMA self-sim). The gallery contributes only a **bounded subtracted bonus**. `--gallery-size 1` is then byte-equivalent to Stage-1.

```python
gal = self.seed.gallery
for p in persons:
    ph = self.feat_fn(frame, p["box"])
    p["_ph"] = ph                         # cache for _track banking reuse

    if gal is not None:
        if use_anchor and not gal.anchor_pass(ph):   # slot-0 AND-veto, anchor_sim ONLY
            continue
        anchor_sim, g_sim, d_sim = gal.score(ph)
    else:
        if use_anchor and hist_similarity(self.seed.anchor_hist, ph) < self.a.anchor_floor:
            continue
        anchor_sim = g_sim = d_sim = 0.0

    d = math.hypot(p["cx"] - sx, p["cy"] - sy) / max(diag, 1.0)
    sim = hist_similarity(self.seed.hist, ph)         # UNCHANGED: EMA self-sim
    area = max(p["w"] * p["h"], 1.0)
    size_term = clamp(abs(math.log(area / s_area)) / 2.0, 0.0, 1.0)
    cost = (self.a.w_centroid * d
            + self.a.w_color * (1.0 - sim)
            + self.a.w_size * size_term)
    if gal is not None:
        cost -= self.a.w_gallery * max(0.0, g_sim - anchor_sim)     # gallery LOWERS
        cost += min(self.a.w_distractor * max(0.0, d_sim - g_sim),  # distractor RAISES
                    self.a.w_distractor)                            # clamped
    scored.append((cost, sim, p, ph))
```
- **6-tuple return UNCHANGED.** Returned `sim` stays the **seed.hist self-sim** (do NOT return `g_sim` — it would inflate the L1379 hiconf EMA gate). Track-id preference block L1287–1293 unchanged.
- **The single load-bearing review line:** `if use_anchor and not gal.anchor_pass(ph): continue` is the ONLY admission gate. Never gate `continue` on `g_sim`. **sacred_safe.**

### 2.9 `_track` accept path — hoist jump/diag, then admit + isolation-gated banking (L1377–1389)
**MUST-FIX (showstopper):** `diag`/`jump` are defined only inside `if sim >= hiconf` (L1380–1383). The gallery admit needs `jump` on **sim<hiconf** frames (exactly the back-pose/shadow frames Stage 2 targets) → `NameError` → caught as a fault → Stage 2 silently no-ops. **Hoist them above the hiconf gate.**

```python
# Hoisted ONCE so both the EMA gate and gallery.admit share it.
diag = math.hypot(w_img, h_img)
jump = math.hypot(cx - self.seed_prev_centroid[0],
                  cy - self.seed_prev_centroid[1]) / max(diag, 1.0)

# Color EMA: UNCHANGED gate on seed.hist self-sim (NOT g_sim).
if sim >= self.a.hiconf and jump <= self.a.ema_max_jump:
    new_hist = best_hist
    if new_hist is not None and self.seed.hist is not None:
        self.seed.hist = ((1.0 - self.a.color_ema) * self.seed.hist
                          + self.a.color_ema * new_hist)
    elif new_hist is not None:
        self.seed.hist = new_hist

# Stage 2 gallery maintenance (additive; crash-safe; never blocks control).
if self.seed.gallery is not None:
    try:
        gal = self.seed.gallery
        bi = next((i for i, q in enumerate(persons) if q is best), None)
        iso_best = (bi is not None and
                    max_iou_other(persons, bi, self.seed.track_id) < self.a.bbox_iou_isolate)
        admitted = gal.admit(best_hist, self.seed.confidence, jump,
                             self.a.ema_max_jump, iso_best, self._frame_idx)
        if admitted:
            a_s, g_s, d_s = gal.score(best_hist)
            log("GALLERY-ADMIT f=%d id=%s anchor_sim=%.2f conf=%.2f jump=%.3f g=%d"
                % (self._frame_idx, self.seed.track_id, a_s,
                   self.seed.confidence, jump, len(gal.gallery)))
        for j, q in enumerate(persons):
            if q is best:
                continue
            if q.get("track_id") == self.seed.track_id:   # self-veto guard
                continue
            iso_q = max_iou_other(persons, j) < self.a.bbox_iou_isolate
            qh = q.get("_ph")                              # reuse cached hist
            if qh is None:
                qh = self.feat_fn(frame, q["box"])
            banked = gal.add_distractor(qh, iso_q)
            if banked:
                a_q, _, _ = gal.score(qh)
                log("GALLERY-BANK f=%d id=%s anchor_sim=%.2f iso=%s d=%d"
                    % (self._frame_idx, q.get("track_id"), a_q, iso_q, len(gal.distractors)))
    except Exception as e:
        log("GALLERY-ERR %s (anchor-only this frame)" % e)
```
- **Self-veto closure:** banking requires `track_id != seed.track_id` AND `add_distractor` internally requires `sim < anchor_floor`.
- **Compute honesty:** banking reuses the per-person `_ph` cached in `_associate`, not a fresh `calcHist`. **sacred_safe.**

### 2.10 New flags + defaults (`parse_args`, after Stage-1 block ~L1612)
| Flag | Default | Notes |
|---|---|---|
| `--gallery-size` (int) | **8** | `1` ⇒ `gallery=None` ⇒ exact Stage-1 rollback |
| `--distractor-size` (int) | **32** | bounded negative bank |
| `--bank-floor` (float) | **0.55** | gallery-admissible iff `anchor_sim >= this` |
| `--admit-conf` (float) | **0.6** | min `seed.confidence` to admit |
| `--admit-spacing` (int) | **5** | min processed-frames between admits |
| `--bbox-iou-isolate` (float) | **0.1** | isolation gate (admit + bank + Stage-3 vote) |
| `--w-gallery` (float) | **0.3** | bounded cost discount; can never overpower slot-0 |
| `--w-distractor` (float) | **0.5** | cost penalty, **clamped** |

### 2.11 Compute (Stage 2)
`_associate`: cache `_ph` (1 `calcHist`/person — already paid by Stage 1) + `score()` over anchor-passers ≤ `1+8+32=41` `compareHist`. For K≤3 anchor-passers ≈ **0.08–0.15 ms**. Banking reuses `_ph` (the feared per-person recompute is **eliminated**). `max_iou_other` O(N²), N≤8, <0.02 ms. **Net Stage 2 < 0.2 ms/frame.**

### 2.12 / 2.13 Validation + rollback
Audit fields per `GALLERY-ADMIT`/`GALLERY-BANK`. **Gate to `--drive`:** zero `GALLERY-ADMIT` with `id != seed.track_id`; zero `GALLERY-BANK` on `seed.track_id`; no admit when not isolated. `--gallery-size 1` → `gallery=None` → byte-identical Stage-1. Any `TargetGallery` exception → `GALLERY-ERR` + anchor-only this frame.

---

## 3. Stage 3 — passive markerless re-acquire

**Ship posture:** `--auto-reacquire` defaults **OFF**, and on HS the re-lock arm is **HARD-DISABLED** — `_try_reacquire` runs the full vote and logs every decision but **returns False after logging** (audit-only). The re-lock body executes only behind `self._relock_armed`, `False` on HS, `True` only when `feat_fn` is an embedding backend (Stage 4).

### 3.1 New constant + fields
- `TS_RELOCALIZING = "RELOCALIZING"` beside the other TS constants (~L733). Viz/log only; node state stays `S_REACQUIRE`.
- `Follower.__init__`: `self._reloc_streak = 0`, `self._reloc_track_id = None`, `self.reloc_since = 0.0`, `self._relock_armed = False`.

### 3.2 `_track` hard-loss entry (L1334–1339) — open the window + reset
```python
self.reloc_since = time.monotonic()
self._reloc_streak = 0
self._reloc_track_id = None
```

### 3.3 `_process_frame` S_REACQUIRE branch (L1143–1161) — wire BEFORE the marker block
**MUST-FIX:** add `and not marker_locked` so the marker truly wins. Insert AFTER `_stand()`/`_hold()` (robot already STOOD) and BEFORE `if marker_locked:`:
```python
if (self.a.auto_reacquire and self.seed is not None and self.seed.gallery is not None
        and not marker_locked
        and (time.monotonic() - self.reloc_since) <= self.a.reloc_seconds):
    if self._try_reacquire(frame, persons, w_img, h_img):
        log("AUTO-RELOCK -> TRACK")
        return
elif (time.monotonic() - self.reloc_since) > self.a.reloc_seconds:
    self._reloc_streak = 0
    self._reloc_track_id = None
```
Marker block + timeout log remain **verbatim** as the fallback. **sacred_safe.**

### 3.4 `_try_reacquire` (NEW method near `_try_coast` ~L1483) — PASSIVE, commands NO motion
```python
def _try_reacquire(self, frame, persons, w_img, h_img):
    """STAGE 3 passive vote. Commands NO motion. On HS _relock_armed is False ->
    votes + logs but NEVER re-locks. Identity NEVER created here; re-lock
    REFRESHES box/centroid/size/track_id only."""
    try:
        gal = self.seed.gallery
        if gal is None or not persons:
            self._reloc_streak = 0; self._reloc_track_id = None
            return False
        if self.seed.anchor_hist is None or self.a.anchor_floor <= 0.0:
            return False   # never relock without a frozen anchor

        survivors = []
        for i, p in enumerate(persons):
            ph = self.feat_fn(frame, p["box"])
            a_s, g_s, d_s = gal.score(ph)
            iso = max_iou_other(persons, i, p.get("track_id")) < self.a.bbox_iou_isolate
            ok = (a_s >= self.a.anchor_floor          # slot-0 HARD gate (anchor, not g)
                  and g_s >= self.a.reloc_floor
                  and (g_s - d_s) >= self.a.reloc_margin
                  and (g_s - d_s) >= self.a.reloc_sep
                  and iso)
            if ok:
                survivors.append((g_s, a_s, d_s, i, p))
            else:
                log("RELOC-REJECT id=%s anchor=%.2f g=%.2f d=%.2f margin=%.2f iso=%s"
                    % (p.get("track_id"), a_s, g_s, d_s, g_s - d_s, iso))

        if not survivors:
            self._reloc_streak = 0; self._reloc_track_id = None
            return False
        survivors.sort(reverse=True)
        if len(survivors) >= 2 and (survivors[0][0] - survivors[1][0]) < self.a.reloc_sep:
            self._reloc_streak = 0; self._reloc_track_id = None
            return False

        best = survivors[0][4]
        bid = best.get("track_id")
        if bid == self._reloc_track_id:
            self._reloc_streak += 1
        else:
            self._reloc_track_id = bid
            self._reloc_streak = 1

        if self._reloc_streak < self.a.reloc_streak:
            return False
        if not self._relock_armed:
            log("RELOC-VOTE-PASS id=%s streak=%d (HS audit-only, NOT re-locking)"
                % (bid, self._reloc_streak))
            return False     # HS: log-only, zero behavioral/identity change

        # ---- ARMED re-lock (Stage-4 embeddings only). Resume-THEN-commit. ----
        if self.drive and self.standing:
            if not self._resume_walk():          # NOTE: blocks ~5s; see §3.7
                self._hold()
                return False                     # stay standing, stay S_REACQUIRE
        self.seed.box = best["box"]
        self.seed.centroid = (best["cx"], best["cy"])
        self.seed.size = max(best["w"] * best["h"], 1.0)
        self.seed.track_id = bid
        self.seed.track_state = TS_LOCKED
        self.seed.miss_streak = 0
        self.seed_prev_centroid = self.seed.centroid
        self.state = S_TRACK
        self.lost_count = 0
        self.marker_streak = 0
        self._reloc_streak = 0; self._reloc_track_id = None
        log("AUTO-RELOCK id=%s g_sim=%.2f -> TRACK (anchor kept)" % (bid, survivors[0][0]))
        return True
    except Exception as e:
        self._reloc_streak = 0; self._reloc_track_id = None
        log("RELOC-ERR %s -> marker-only" % e)
        return False
```

### 3.5–3.8 Guarantees
- **STOOD / zero-motion:** runs only in `S_REACQUIRE` after `_stand()`; `_drive_vel` gated on `self.walking` (which `_stand()` cleared); the method issues no `_drive_vel`/yaw/walk.
- **Identity-stays-on-anchor:** re-lock refreshes box/centroid/size/track_id only; `anchor_hist`/`anchor_feat`/`gallery`/`distractors`/`hist` untouched → a *re-association*, not a re-identification. The marker (`_try_seed`) is the sole identity-change mechanism.
- **Blocking `_resume_walk` hazard:** `_resume_walk` (L843–865) stalls the loop ~5 s (robot safely standing). Invoked only in the **armed** path (Stage-4); HS audit-only never triggers it. **Enabling armed HS auto-reacquire authorizes autonomous pursuit of a vote winner — keep OFF until embeddings.**
- **Timeout → marker:** past `reloc_seconds` the vote is skipped (streak reset once) → byte-identical to today's marker + ASK-FOR-MARKER path.

### 3.9 Flags (OFF) + why HS ships OFF
| Flag | Default | |
|---|---|---|
| `--auto-reacquire/--no-auto-reacquire` | **OFF** | OFF = today's marker-only |
| `--reloc-floor` | **0.70** | `> anchor_floor 0.5` |
| `--reloc-margin` | **0.20** | min `g_sim - d_sim` |
| `--reloc-sep` | **0.15** | min separation vs runner-up survivor |
| `--reloc-streak` | **5** | consecutive same-id qualifying frames |
| `--reloc-seconds` | **6.0** | vote window from REACQUIRE entry |

**Why OFF:** on 30×32 HS, `reloc_floor 0.70` is clearable by a same-clothing stranger under matched lighting. The vote reduces but cannot eliminate a markerless mis-relock; trustworthy only on Stage-4 OSNet embeddings. **Hard prerequisite:** Stage 2 (`max_iou_other`, `--bbox-iou-isolate`, the distractor bank) — the isolation gate and the `sep` bar are uncomputable without it.

### 3.10–3.13 Viz / compute / rollback
Banner `"RELOCALIZING - AUTO RE-ACQUIRING (streak n/N)"`; **status byte stays 3; K1F1 unchanged.** Compute: 0 in TRACK/SEARCH; in RELOCALIZING (stood) ≈ **0.3–0.5 ms** (robot stationary → latency safety-irrelevant). `--no-auto-reacquire` → `_try_reacquire` never called → marker-only recovery byte-for-byte.

---

## 4. Correctness & safety review (closures)

| Critic finding | Closure |
|---|---|
| **AND-not-OR** (load-bearing) | `score()` returns `anchor_sim` separately; `_associate` vetoes on `anchor_pass`/`anchor_sim` **alone**; `g_sim` enters only the bounded cost discount. `_try_reacquire` floor-gates on `a_s`, never `g_s`. |
| **jump/diag scope (showstopper)** | `diag`/`jump` hoisted **above** the `if sim>=hiconf` gate; shared by EMA and `gallery.admit`. |
| **EMA gate input** | hiconf EMA gate keeps `sim` = `seed.hist` self-sim; `g_sim` never fed in. |
| **Rollback equivalence** | cost color term stays on `seed.hist`; gallery only subtracts → `--gallery-size 1` byte-identical. |
| **Self-veto via banking** | `add_distractor` requires `sim < anchor_floor` AND caller requires `track_id != seed.track_id` AND `feat is not None`. |
| **anchor_floor==0 disable** | `anchor_pass` True when `anchor_floor<=0` / `anchor_feat None`. |
| **max_iou_other brittleness** | self-excluded by index; fault → 1.0 (not isolated). |
| **No base motion in RELOCALIZING** | no `_drive_vel`/yaw/walk; runs after `_stand()`. |
| **Identity only in `_try_seed`** | re-lock refreshes box/centroid/size/track_id only; review-enforced. |
| **Deque bounds** | slot-0 separate (never evicted); `gallery maxlen=size-1`; `--gallery-size 1` → maxlen 0 → no admits. |
| **resume-then-commit** | `S_TRACK` set only after `_resume_walk()` succeeds; else `_hold()`, stay `S_REACQUIRE`. |
| **marker precedence** | `and not marker_locked` guard. |
| **streak reset single-site** | reset on hard-loss entry, window-expiry, every no-survivor frame. |
| **compute "banking is free" error** | banking reuses cached `_ph`. |

**Residual (accepted):** HS auto-reacquire is stranger-clearable → ships OFF / audit-only. Crowded scenes degrade to anchor-only (safe direction). A turnaround entirely during occlusion (empty back-pose gallery) still needs the marker.

---

## 5. Preview validation protocol (all `--preview --stream`, before any `--drive`)

| # | Scenario | Command | Pass/fail |
|---|---|---|---|
| **A** | look-alike enters | `--track --gallery-size 8` | FAIL if any `GALLERY-ADMIT id != seed.track_id`; near-tie must degrade to safe-stop, not ID-switch |
| **B** | turn-360 + lit↔shadow | `--gallery-size 8` | PASS = zero false losses across the turnaround; gallery grows on isolated back-pose views |
| **B-rollback** | baseline | `--gallery-size 1` | PASS = byte-equivalent TRACK/COAST/REACQUIRE |
| **C** | crossing/overlap | passer-by occludes | FAIL if any admit/bank while not isolated |
| **D** | near-stander | different person at last centroid | FAIL if followed or admitted; banked only when isolated |
| **Stranger-admission audit** | multi-session | `--gallery-size 8` | GATE to `--drive`: zero impostor admissions, zero `GALLERY-BANK` on `seed.track_id` |
| **E** | occlusion → reacquire | `--gallery-size 8 --auto-reacquire --reloc-streak 5 --reloc-seconds 6` | PASS (HS) = passive vote logged (`RELOC-VOTE-PASS ... audit-only`), **zero `AUTO-RELOCK`**, zero `vx`/`vyaw` |
| **decoy stranger** | E with same-clothing look-alike | as E | PASS = decisions auditable via `RELOC-REJECT`; HS never re-locks |
| **timeout** | target absent past window | as E | PASS = byte-identical to today's marker path after the window |

**Identity-immutability check (E):** print a hash of `seed.anchor_hist` before loss and after any (armed) re-lock — must be identical.

---

## 6. Build & commit sequence

1. **Commit 2a — gallery scaffold (no behavior change):** `import collections`, `max_iou_other`, `TargetGallery`, `Follower.feat_fn/sim_fn/_frame_idx`, `Seed.gallery=None`, `_try_seed` init, `_frame_idx++`. Guard: `--no-track` reproduces Stage-0; `--gallery-size 1` reproduces Stage-1 byte-for-byte. No `GALLERY-*` lines yet.
2. **Commit 2b — wire gallery into `_associate` + `_track`:** slot-0 AND-veto, `_ph` cache, bounded cost discount, hoisted jump/diag, `gallery.admit`, `GALLERY-ADMIT` audit. Guard: `--gallery-size 1` byte-equivalent; mode-B win demonstrated; stranger-admission audit clean.
3. **Commit 2c — distractor bank:** `add_distractor` + `GALLERY-BANK` audit + clamped `--w-distractor`. Guard: zero `GALLERY-BANK` on `seed.track_id`; no bank during overlap.
4. **Commit 3 — Stage 3 passive vote (audit-only on HS):** constant/fields, hard-loss reset, `_try_reacquire` (re-lock body behind `_relock_armed=False`), S_REACQUIRE wiring with `and not marker_locked`, banner. Guard: `--no-auto-reacquire` reproduces marker-only; with `--auto-reacquire` on HS, never `AUTO-RELOCK`, never `vx`/`vyaw` in RELOCALIZING.

**Regression guards at every commit:** `--no-track` (pre-Stage-1), `--gallery-size 1` (Stage-1), `--no-auto-reacquire` (marker-only).

---

## 7. Compute budget (per frame, 10 Hz = 100 ms, alongside YOLO11n + Stage-1 tracker)

| Component | State | Est. ms |
|---|---|---|
| YOLO11n detect | every frame | **UNMEASURED long pole** |
| Stage-1 MultiTracker | every frame | 0.3–0.8 |
| `color_hist` per person (cached `_ph`) | every frame | already paid |
| Stage 2 `score()` over anchor-passers | every frame | 0.08–0.15 |
| Stage 2 isolation + banking (cached feats) | every frame | <0.05 |
| Stage 3 vote | **RELOCALIZING only (stood)** | 0.3–0.5 (0 in TRACK/SEARCH) |

**Net Stages 2+3 ≈ <0.3 ms/frame in TRACK.** The `run()` SLOW-LOOP guard surfaces any overrun in `--preview`. **Measure YOLO end-to-end before Stage 4.**

---

## 8. What stays sacred (NOT touched)
- Control law (L1400–1407) + 3-layer clamps (L92–96, L797–800) — byte-identical.
- `target_point_from_box` (L321) — single control hook.
- Stand-on-loss → kPrepare; **RELOCALIZING is PASSIVE — no open-loop motion, no `_drive_vel`**.
- COASTING yaw-only + depth-validated; coast and reloc are time-disjoint.
- Ambiguity→stop guard + `assoc_margin` unchanged.
- `_stand`/`_hold`/`_resume_walk`, ping-before-walk, watchdogs, `--preview` default, K1F1 wire format — unchanged; only banner/stderr text gains state info.
- **Identity-immutability:** `anchor_hist`/`anchor_feat` created/replaced ONLY in `_try_seed`. Gallery mutates only its deques; Stage-3 re-lock refreshes box/centroid/size/track_id only.
- Fresh monotonic track ids — a returning/new target can never inherit the anchored id.
