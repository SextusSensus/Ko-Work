# K1 Follow Mode — Perception Autonomy Hardening Plan

**File under change:** `K1Finder/follow_person_k1.py` (1308 lines at design time; **1649 after Stage 1 landed** — line anchors in this doc predate Stage 1; see `STAGE_2_3_IMPL.md` for anchors against the current file)
**Goal:** (1) never disengage from the originally-anchored person when others enter frame; (2) recover autonomously with minimal marker/operator dependence.
**Discipline:** every stage independently shippable, preview-validatable, flag-gated to today's exact behavior, and degrades to the HS-histogram path on any failure.

Produced by a 4-lens design workflow (deep-reid / motion-mot / autonomy-policy / integration-safety), each adversarially critiqued for 10 Hz feasibility and fall risk, then synthesized — leveraging the `policy-engineer` (Orin/TRT), `xr-engineer` (MOT/frames), and `embodied-ai-advisor` (control/safety) skills.

Failure modes: **A** look-alike · **B** turnaround/lighting · **C** crossing/occlusion · **D** near-stander · **E** autonomous marker-free re-acquire.

---

## 0. ROS2 architecture decision (from the embodied-ai-advisor consult)

**Keep follow mode a single node; do NOT split perception/control into separate ROS2 nodes for this change.** The advisor's house pattern (perception node + control node, follow-as-an-action, safety-stop preemptor) is the right *long-term* architecture, but pushing it here would move the sacred velocity clamps across a process/DDS boundary and create a new failure surface (perception dies, control drives on a stale target) — a much bigger blast radius than a perception-only enhancement warrants. Instead, **enforce the separation logically inside the node**: `target_point_from_box()` stays the single control hook, and a `TargetEstimator` (tracker + gallery + state) returns only `(target_point, range, tracking_state, confidence)` to the unchanged control code.

**Trigger condition for a real node split later:** when perception must run at a different rate than control, or when a model too heavy for 10 Hz (VLA-class) is introduced, or when moving to an action-based `follow-until` API. That's its own project with its own safety re-validation — not a rider on this work.

---

## 1. Root cause

Today's identity decision lives in `_associate` (L1032) and `_track` (L1067) and rests on **one frozen 30×32 HS histogram** (`Seed.anchor_hist`, L576, immutable) used as a **hard veto** (L1050: `if hist_similarity(anchor_hist, ph) < anchor_floor: continue`), plus a **stateless, memoryless** per-frame argmin over `cost = w_centroid·d + w_color·(1-sim) + w_size·size_term` (L1056-1058), plus an **ambiguity→stop** guard (L1074-1075), with `lost_count` as the only temporal memory. There is **no notion of "the same person across frames"** and **no motion model**. Consequences:

- **A (look-alike):** a similar-clothing bystander clears the loose HS floor, lands near the target in centroid+color cost, and the runner-up gap collapses below `assoc_margin` → **ambiguity guard fires → STOP/drift**. A 30×32 HS histogram cannot separate two people in similar clothing.
- **B (turnaround/lighting):** color flips front↔back or moves through shadow → `hist_similarity(anchor_hist, ph)` drops below `anchor_floor` → the **frozen veto deletes the real target** → false loss → REACQUIRE-needs-marker. The biggest operator-pain item.
- **C (crossing):** a passer-by occludes the target for even one frame → `best is None` → `lost_count += 1` immediately. No coast; the crosser can win on stale centroid.
- **D (near-stander):** target leaves, a different person stands at the last centroid → low `d` + passing HS match lets **"closest body" win**. No persistent ID forbids a newcomer inheriting the target.
- **E (recovery):** the only exit from `S_REACQUIRE` is the marker (L934-945). Zero autonomous re-acquisition.

Through-line: **identity is one brittle frozen color signature with no temporal or motion continuity.** Fix needs (i) motion continuity, (ii) a richer-but-still-anchored appearance model, (iii) a first-class, time-bounded recovery state strict enough that a stranger is never silently adopted.

---

## 2. Target architecture (end state)

Four cooperating layers **upstream of an unchanged control boundary**. `target_point_from_box` (L321) stays the only control hook; the control law + 3-layer clamps (L91-96, 616-619, 1144-1154), `_stand`/`_hold`/`_resume_walk`, stand-on-loss→kPrepare, and the K1F1 stream protocol are **byte-identical**.

### 2.1 Pixel-space motion model (per-track Kalman) — every frame, top of `_process_frame`
A `MultiTracker` of constant-velocity `Track`s in **image space only** (monocular, no IMU → metric is untrustworthy; SACRED constraint honored). State `x = [cx, cy, s, r, vcx, vcy, vs]` (s=area, r=aspect), numpy-only (no scipy/filterpy hard dep; greedy fallback). YOLO boxes associate to tracks by **IoU + Mahalanobis gate**; matched tracks update; unmatched detections spawn **fresh monotonic IDs that can never inherit the anchor**; stale tracks age out. Stamps `p['track_id']`; exposes predicted boxes for coasting. **C/D fix:** identity rides motion continuity, not nearest-stale-centroid.

### 2.2 Appearance model — anchored gallery + negative bank, on gated candidates
The single frozen hist grows into a **bounded gallery + distractor bank** scored against an **immutable anchor**, behind one `sim_fn`/`feat_fn` indirection chosen at startup:
- **Stage 1/2 (CPU, no model):** HS-histogram features; gallery = accepted same-person views; negative bank = confidently-not-target people.
- **Stage 3+ (optional GPU):** OSNet-x0.25 512-d L2-normed embeddings (TRT FP16), identical structure, `sim_fn=cosine`. **A becomes tractable here.**

**Critical: the gallery veto is an AND, not an OR.** The frozen anchor (slot-0) must pass `anchor_floor` as a HARD necessary condition every frame; gallery slots and distractor margin may only *reduce cost / break ties*, never *grant admission* a slot-0 failure would deny. A `max`-over-slots OR-veto would monotonically widen identity toward an impostor — forbidden. Every gallery admission and distractor banking is **gated on bbox isolation** (IoU with every other person < ~0.1) so a crossing can never poison the gallery or bank the *target* as a negative.

### 2.3 Fusion + state policy — sub-state machine inside `S_TRACK`
First-class `Seed.track_state ∈ {LOCKED, COASTING, RELOCALIZING}` (node-level `S_REACQUIRE`/marker stays the fallback), with numeric `confidence` carried **beside** the estimate every frame (SACRED xr rule: never act on an estimate without its state/confidence).
- **LOCKED:** anchor track present, slot-0 passes, gate passes → follow via unchanged control law. A look-alike is logged as a challenger and **can never steal the lock**.
- **COASTING:** anchor track unseen this frame but `time_since_update ≤ coast_frames` → follow the Kalman-predicted centroid through the **unchanged** clamps, **vx forced to 0** (yaw-only). Does not increment `lost_count`. Re-locks only after an **on-separation slot-0 re-confirm** so a crossing cannot swap IDs.
- **RELOCALIZING:** coast exhausted → **passive** autonomous re-acquire while STOOD (kPrepare, zero motion — no blind yaw sweep). Re-lock only on a strict sustained vote (high floor + positive margin + separation over N consecutive frames on the same bbox-isolated track). Timeout → today's marker path, verbatim.

### 2.4 Data structures and where each runs

| Structure | Augments/replaces | Runs |
|---|---|---|
| `Track` / `MultiTracker` | NEW; upstream of everything | every frame, top of `_process_frame` (after `det.detect`, L909) |
| `Seed.track_id/track_state/confidence/miss_streak/vel/pred_centroid` | NEW fields on Seed (L568) | set in `_track`/`_try_seed` |
| `TargetGallery` (immutable `anchor_feat`, `gallery`, `distractors`) | replaces single frozen `anchor_hist` as the identity signal; `Seed.hist` kept as cheap pre-filter/fallback | `score` in `_associate`; `admit`/`add_distractor` in `_track` (isolation-gated) |
| `sim_fn`/`feat_fn` indirection | HS vs OSNet, chosen once | binds in `Follower.__init__` |

**Identity-immutability invariant:** `anchor_feat`/`anchor_hist` is created/replaced **only** in `_try_seed` (the marker path). Nothing else may set identity from a non-anchor candidate.

---

## 3. Staged rollout

### Stage 1 — Motion-coast + state machine + tuned gates (NO new model dependency)  ✅ IMPLEMENTED (2026-06-25)
The real Stage-1 win all four critiques converge on. Pixel-space tracker + anchor-ID binding + bounded coast + first-class state. Compute-neutral-to-negative.

> **Status:** landed in `follow_person_k1.py`. New: `Track`/`MultiTracker` (pixel-space KF + IoU assoc, numpy-only, scipy-optional), `iou_xyxy`/`_box_to_z`/`_x_to_box` helpers, `Seed.{track_id,track_state,confidence,miss_streak,pred_centroid}`, `TS_LOCKED`/`TS_COASTING`, `_try_coast()`, a 6-tuple `_associate` with track-id preference + ambiguity-bypass, COASTING insertion in `_track`, a slow-loop guard in `run()`, COASTING viz in `_stream_frame`, and flags `--track/--no-track --coast-frames --coast-vx-scale --iou-min --max-coast`. Defaults: `--track` ON, `--coast-frames 0` (coast opt-in), `--coast-vx-scale 0.0` (yaw-only). `--no-track` reproduces the pre-Stage-1 path. Validate in `--preview` before `--drive`.

- **NEW** `Track`/`MultiTracker` near `target_point_from_box` (~L320), numpy-only, try/except → any failure leaves `persons` untouched (today's path). `linear_sum_assignment` if scipy present, else greedy.
- **`_process_frame`** (after `persons = self.det.detect`, L909): `self.tracker.update(persons, frame)` — annotation only; stamps `p['track_id']`.
- **`Seed`** (L568): add `track_id`, `track_state='LOCKED'`, `confidence`, `miss_streak=0`, `vel`, `pred_centroid`.
- **`_try_seed`** (~L1017): bind `seed.track_id = chosen['track_id']`; `track_state=LOCKED`. Marker selection / `_plausible` / `reseed_anchor_floor` **unchanged**.
- **`_associate`** (L1032): **prefer** candidate with `p['track_id']==seed.track_id`; keep existing `anchor_floor` veto + cost + ambiguity guard. A fresh id is **never** eligible to become the seed id. Compute-saver: only `color_hist` the bound track + (in RELOCALIZING) others.
- **`_track`** no-match branch (L1077-1106): insert COASTING **before** the `lost_grace` escalation — follow Kalman-predicted centroid through the unchanged control block, **force vx=0**, do not increment `lost_count`, re-confirm slot-0 on re-detection. Only on coast exhaustion does the **existing** `lost_grace → _stand()/_hold() → S_REACQUIRE` run byte-for-byte.
- **`_stream_frame`** (L856) + `log()`: append `track_state`/`track_id`/`confidence` to the human banner only. K1F1 magic/status/len/jpeg **unchanged**.
- **NEW slow-frame guard:** log measured loop rate; if processing overruns the period for K consecutive frames, log it (observable in preview).

**New flags (SAFE defaults reproduce today exactly):** `--track/--no-track` (ON, but `--coast-frames 0` ⇒ today's behavior) · `--coast-frames` (default **0**; opt-in 6-8 ≈ 0.6-0.8 s) · `--coast-vx-scale` (default **0.0**, yaw-only — non-negotiable) · `--iou-min` (0.2) · `--max-coast` (10) · gate/margin/anchor-floor passthrough.

**Orin budget:** numpy KF predict+update ~5-20 µs/track; IoU + assignment for N≤8 < 0.2 ms; total **~0.3-0.8 ms/frame**. Net compute-neutral-to-negative.

**Preview validation:** `--preview --stream --track --coast-frames 8` (note: `--reloc-seconds` does not exist until Stage 3 — do not pass it against the Stage-1 build). Watch stable `track_id` (no increment as the person walks); **C** → `COASTING id=N` then `LOCKED` on the *same* id after separation, crosser gets a different id; **D** → newcomer gets a fresh id, never selected; coast bounding into existing REACQUIRE; printed vx=0 while coasting; `--no-track` reproduces today's logs byte-for-byte.

**Rollback:** `--no-track` (or `--coast-frames 0`) = today's exact path. Any tracker exception → no-op annotation, HS path runs.

### Stage 2 — Anchored gallery + distractor bank (CPU, slot-0 hard-gated)  ✅ IMPLEMENTED (2026-06-25)
Hardens identity for **B** (partial A/D), no new model, after Stage 1 is trusted. Landed per `STAGE_2_3_IMPL.md` §2; `--gallery-size 1` = Stage-1 rollback. Pending on-robot preview validation.
- **NEW** `TargetGallery` (HS mode). `Seed.gallery` deque (slot-0 = frozen anchor, pinned), `Seed.distractors` deque.
- **`_associate`** (L1050): replace single-anchor veto with the **AND** rule (slot-0 HARD gate; gallery only lowers cost).
- **`_track`** accept path (replaces L1124-1134 EMA): `gallery.admit(...)` only when high-conf + non-teleport (reuse `ema_max_jump`) + spaced + **bbox-isolated**; `add_distractor` for every other in-frame person, isolated only. `seed.hist` EMA kept as cheap pre-filter.
- **`_try_seed`:** init `gallery=[anchor]`, slot-0 frozen across re-seeds.

**New flags:** `--gallery-size` (8) · `--distractor-size` (32) · `--bank-floor` (0.55) · `--admit-conf` (0.6)/`--admit-spacing` (5) · `--bbox-iou-isolate` (0.1).

**Orin budget:** `max` over K≤8 cheap `compareHist` < 0.1 ms; banking ~free. **< 0.5 ms/frame.**

**Preview validation:** **B** → target turns 360° / walks lit↔shadow, stays LOCKED where today's frozen floor logged a loss. **Stranger guard** → audit log every admission with slot-0 sim + runner-up sim + reason; require multi-session preview showing **zero impostor admissions** before `--drive`. **D** → near-stander banked, not followed.

**Rollback:** `--gallery-size 1` ⇒ anchor-only ⇒ Stage-1 behavior.

### Stage 3 — Autonomous markerless re-acquisition (E), PASSIVE
The autonomy headline, gated behind Stages 1-2, **passive (no yaw sweep)**.
- **`_process_frame`** S_REACQUIRE branch (L934-952): before the marker block, if `--auto-reacquire` and within `reloc_seconds`, run a strict gallery+distractor vote **while STOOD** (kPrepare, zero motion). Re-lock only on `g_sim ≥ reloc_floor` AND `margin ≥ reloc_margin` AND separation over `reloc_streak` consecutive frames on the same bbox-isolated track. Keep `anchor_feat`; refresh box/centroid; resume via existing `_resume_walk`. Timeout → today's marker path. Log every rejected candidate's sim.
- Marker re-seed remains the **only** mechanism allowed to change anchor identity.

**New flags (default OFF):** `--auto-reacquire/--no-auto-reacquire` (**OFF**) · `--reloc-floor` (0.70, > anchor_floor) · `--reloc-margin` (0.20) · `--reloc-sep` (0.15) · `--reloc-streak` (5) · `--reloc-seconds` (6.0).

**Honest scoping:** on **HS histograms**, `reloc_floor 0.70` is clearable by a same-clothing stranger under matched lighting, so **passive HS auto-reacquire is the weakest, most stranger-risky link — it ships OFF.** It becomes trustworthy only on **OSNet embeddings (Stage 4)**. Until then, the marker remains required for re-acquire.

**Orin budget:** only while stood, reuses existing features: **< 0.5 ms/frame in RELOCALIZING, zero in normal TRACK.**

**Rollback:** `--no-auto-reacquire` ⇒ today's marker-only recovery.

### Stage 4 — stronger appearance via `feat_fn`/`sim_fn` swap

**Implemented MODEL-FREE (2026-06-25):** per the directive "without changing to a new model," Stage 4 shipped as a **part-based (vertical-band) HS descriptor** (`striped_feat`/`striped_sim`) selected by `--appearance striped` (default `global` = Stages 1-3 verbatim), plus an opt-in `--arm-reacquire` (default OFF) that lets `--auto-reacquire` actually re-lock. It splits the bbox into 3 vertical bands, keeps a central slab (drops side-background), histograms each (16×16 HS), L1-normalizes into one vector, cosine sim. This separates people with different **color layout** that the single global histogram tied (failure mode A) — OpenCV/numpy only, no new model. Re-tune `--anchor-floor`/`--bank-floor`/`--reloc-floor`/`--hiconf` for striped (cosine vs correlation) via the replay eval before trusting it. **Honest limit:** still classical — identical-uniform crowds remain hard, so `--arm-reacquire` stays the operator's risk call (validate on the A/E clip families first; never in uniform crowds).

**OSNet embedding (TRT FP16) — IMPLEMENTED (2026-06-25):** `--appearance osnet --reid-engine PATH`. A `ReidEngine` runs a person-ReID ONNX (e.g. `osnet_x0_25_msmt17.onnx`) through **onnxruntime's TensorRT execution provider at FP16** (TRT FP16, reusing the confirmed onnxruntime dep — no pycuda/raw-TRT), CUDA/CPU fallback, engine built+cached on the Orin on first run. `feat_fn=ReidEngine.embed`, `sim_fn=cosine`. Crash-safe: any load failure → hard fallback to the histogram. Same gallery/state-machine/re-acquire — strictly stronger similarity, the backend that makes A robust and armed E trustworthy in crowds. See `STAGE4_OSNET.md` for the build/deploy guide. Open: get/export the ONNX, measure 10 Hz on the actual Orin (batching/every-N if needed), re-tune the cosine thresholds.

**Hard preconditions:**
- Confirm the **exact Orin module** and **measure end-to-end** per-frame ms under `nvpmodel -m 0 + jetson_clocks` **before** writing the class.
- Confirm **tensorrt + pycuda import** in the BoosterRos2 env (only onnxruntime is confirmed for YOLO). If not → Stage 4 disabled; Stages 1-3 still ship.
- **Engine built OFFLINE on the Orin** (`trtexec --onnx=osnet_x025.onnx --saveEngine=…trt --fp16 --useCudaGraph`). Never lazy-build inside the control process. Missing/stale `.trt` → log "ReID disabled, HS fallback" and continue.
- Load-once / allocate-once / warmup-N / CUDA-graph / NaN-and-exception → `ok=False` → **byte-identical HS fallback** (proven by a logged regression run).
- **Compute crop once** (one slice feeds both HS pre-filter and embedding input; resize before normalize; batch gated crops into one infer).
- **Slow-frame failsafe:** if frame time exceeds the 10 Hz period for K consecutive frames with deep-reid on, **auto-disable deep-reid and log it.**

**New flags (default OFF):** `--deep-reid/--no-deep-reid` (OFF) · `--reid-engine PATH` · `--reid-every-n` (2; REACQUIRE always every frame) · `--w-distractor` (0.5).

---

## 4. Compute budget (per frame, 10 Hz = 100 ms, alongside YOLO11n)

> **The baseline is UNMEASURED.** No Orin frame-time / GFLOPs / module ID exists in any record (a "~58 ms confirmed" claim from one lens was fabricated and rejected). Numbers below are estimates pending on-robot measurement, which is a hard precondition for Stage 4.

| Op | Stage | Est. ms | Cadence | Notes |
|---|---|---|---|---|
| YOLO11n detect (onnxruntime CUDA EP) | baseline | **MUST MEASURE** (long pole) | every frame | the gating item; if >60-70 ms, convert YOLO→TRT FP16 *first* |
| to_bgr / preprocess | baseline | already paid | every frame | include in measurement |
| `color_hist` per person | baseline | already paid | every frame | Stage 1 *reduces* this (bound track only) |
| MultiTracker (KF + IoU + assign) | 1 | 0.3-0.8 | every frame | numpy-only |
| Gallery veto | 2 | < 0.1 | every frame | hists already computed |
| Distractor banking | 2 | ~0 | every frame (isolated) | features already computed |
| Passive re-acquire vote | 3 | < 0.5 (0 in TRACK) | RELOCALIZING only, stood | |
| OSNet embed (TRT FP16, batched) | 4 | ~1-2.5 amortized | `--reid-every-n 2` | sub-ms GPU infer; CPU crop+resize+normalize is the honest cost |

Stages 1-3 add **< ~1.5 ms/frame** total and are safe to ship before any measurement. Discipline (SACRED): build engine on the Orin, deserialize/allocate once, warm up, CUDA-graph, fail safe to HS. **Open:** Orin module (NX 8/16GB vs AGX) is unconfirmed and caps everything — read `/proc/device-tree/model` before Stage 4.

---

## 5. Failure-mode coverage

| Mode | Closed by | How | Residual gap |
|---|---|---|---|
| **A** look-alike | **Stage 4** (robust); Stage 1 partial | Embeddings separate identities HS cannot. Stage 1 gives the look-alike a *different* id but HS lock can still go ambiguous→safe-stop. | Identical uniforms shrink the margin → degrades to **safe-stop, not ID-switch**. |
| **B** turnaround/lighting | **Stage 2** (full); Stage 1 partial | Gallery accrues same-person views (slot-0 gated). Stage 1 coast bridges momentary turnarounds. | Turnaround *during* occlusion with empty back-pose gallery may still need the marker. |
| **C** crossing | **Stage 1** (full) | Motion-coast bridges occlusion; on-separation slot-0 re-confirm prevents swap; crosser gets a fresh id. | Tight same-appearance+same-motion crossing → reduced not eliminated; degrades to safe-stop. |
| **D** near-stander | **Stage 1** (full) | Fresh id can't inherit the anchor; predicted-centroid + isolation-gated banking stop "closest body" winning. | Co-located same-clothing stander at exact predicted bearing on HS can tie → safe-stop. |
| **E** auto re-acquire | **Stage 3 on embeddings (Stage 4)** | Passive strict gallery vote re-locks the *same* person while stood, marker-free. | **On HS alone, E is OFF by default** — only embedding-backed E is trustworthy; marker is the fallback. |

**Net:** Stage 1 fully closes **C, D** + partial **B**; Stage 2 fully closes **B**; Stage 4 closes **A** and unlocks trustworthy **E** (Stage 3). Honest residual: HS-only A and E are **safe-stop, not solved** — they need OSNet.

---

## 6. What stays sacred (NOT touched)

- **Control law:** `vyaw = clamp(-k_yaw·bearing, …)` and the vx-from-range-vs-standoff-deadband law (L1144-1154) — byte-identical.
- **3-layer clamps:** `DEF_*` (L92-93), `HARD_VX_LIMIT 0.30`/`HARD_VYAW_LIMIT 0.40` (L95-96), `__init__` binding (L616-619). Control acts only on clamped estimates.
- **`target_point_from_box` (L321)** — single control hook. Coasting builds a synthetic person-dict and passes it through this same hook.
- **Stand-on-loss → kPrepare** (L938-939, 1087-1088): hard loss → zero motion. **RELOCALIZING is PASSIVE — no open-loop yaw sweep.** (The autonomy-policy lens tried to trade this away; rejected.)
- **COASTING is yaw-only:** `--coast-vx-scale` defaults 0.0; vx forced to 0 while coasting. **Extra gate:** if `_range_for` returns `rsrc != 'depth'` during any non-LOCKED state, force vx=0 — forward drive tied to *validated* range only.
- **Degraded ambiguity→stop kept:** incumbent below keep-bar AND challenger within margin → prefer coast-decay-to-zero, but cap consecutive ambiguous frames before forcing `_stand()`.
- **`_stand`/`_hold`/`_resume_walk`, ping-before-walk, stall/no-frame/error defaults, watchdogs, `--preview` default, K1F1 wire format** — unchanged. Only banner/stderr text gains state info.
- **Identity-immutability:** `anchor_feat`/`anchor_hist` created/replaced **only** in `_try_seed`.

Fall risk stays **perception-side only:** a 10 Hz miss just slows the loop (sleep-to-period, no crash); a poisoned gallery degrades to **safe-stop, not garbage motion**.

---

## 7. Open decisions for Modd

1. **Confirm the Orin module** (`/proc/device-tree/model` on the K1). Caps the Stage-4 budget.
2. **Measure the current loop first** — end-to-end per-frame ms over a few hundred frames under `nvpmodel -m 0 + jetson_clocks`. If YOLO-via-onnxruntime already eats >60-70 ms, the cheapest high-value move is **converting YOLO11n→TRT FP16 first**.
3. **Ship a ReID ONNX, or stay histogram-only?** Stages 1-2 deliver C/D/B with zero new dependency. A and trustworthy E only arrive with OSNet (Stage 4).
4. **Confirm `tensorrt`/`pycuda` import** in BoosterRos2. If absent, Stage 4 is blocked (1-3 unaffected).
5. **How aggressive should auto-reacquire (E) be?** Recommendation: OFF on HS, ON only on embeddings with the strict streak+margin+separation+isolation vote. Auto-reacquire *is* a relaxation of "identity changes only via marker" — approve it explicitly, or require a one-time marker confirmation per auto-relock.
6. **Coast aggressiveness:** pick `--coast-frames` (start 6-8 ≈ 0.6-0.8 s); confirm `--coast-vx-scale 0` (yaw-only coast) as the permanent default.

---

## 8. Deferred (parked) — head-coordinated gaze tracking

Decoupling gaze from locomotion (fast head loop centers the target, slow body loop unwinds the head) is a strong future add-on, **parked** for now. Findings already gathered: the K1 has a 2-DOF head exposed via the loco CLI tokens `hl`/`hr` (pan), `hu`/`hd` (tilt), `ho` (center); `loco_follow_bridge.cpp` does **not** yet expose the head (locomotion-only); the camera is **on** the head so `body_bearing = head_pan + image_bearing` (a frame composition that must be handled carefully); and the exact `B1LocoClient` head method signature, joint limits, and whether it works in `kPrepare` are verify-on-robot items in `b1_loco_client.hpp`. Slot as a future Stage 5 once the perception stages are validated.
