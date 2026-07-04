# Environmental labeling → obstacle avoidance — plan

**Status:** design (2026-07-04). Offline-first, approved shape: build the labeling on recordings
(compute-free, thesis-aligned), prove which labels matter, then distill a minimal live reflex.
Governed by the loaded skills: embodied-ai-advisor (model-fit + honest thesis framing),
autonomy-planner (layer separation + the avoidance behavior), runtime-safety (loop budget + fail-safe),
autonomy-ops (bringup + the safety floor). Builds directly on the shipped Rerun observability
([[rerun-integration]], `RERUN_PLAN.md`) — the `.rrd` recordings ARE the labeling input.

## Why
The follow can see a person but is blind to the *room*. To ever avoid a chair/wall/person it has to
first **label** the environment. Two distinct projects hide under "labeling" — keep them apart:
- **(A) Live avoidance reflex** — the follow slows/stops for obstacles in real time. On-robot,
  real-time, fighting for compute.
- **(B) Environmental labeling + capture** — a labeled record of the spaces (class + 3D position).
  Offline-friendly, rich; this is *thesis* data (world-model-grade spatial+semantic), not a follow
  feature. Lead with (B); distill (A) from it.

## Three walls (do not pretend around them)
1. **Compute.** The 10 Hz follow loop runs *at budget* — measured 2026-07-04: total loop p90 ≈ 100 ms
   while following (with Rerun on). No room to add a segmentation model to the control loop. Anything
   live must be COCO-cheap, decimated, and on a **side thread** (the cam-spin pattern the Rerun images
   already use), loop-cost-gated exactly like `--rerun` was.
2. **Geometry.** No LiDAR, no SLAM, no odometry; one forward stereo depth cone (~13 fps, occasionally
   flaky). So "chair/person" is a **detection** problem (2D + depth for range); "wall/floor/drop-off"
   is a **geometry** problem (depth planes) — YOLO never outputs "wall." With no odometry it is an
   **instantaneous egocentric snapshot** — no memory beside/behind, no global map. Honest name:
   *forward-cone obstacle labels*, not a map.
3. **Behavior.** You can *slow/stop* for an obstacle but not *steer around* it without swinging the
   operator out of the ~70° FOV and losing them (the standing rule from `perception-expansion-plan`).
   Avoidance = graded braking in the forward corridor, yaw untouched, now optionally class-aware.

## Cheap wins already in reach
- **Multi-class COCO is nearly free.** `PersonDetector` runs the full 80-class YOLO11n and just
  filters to person (`follow_person_k1.py:749`, `classes=[PERSON_CLS]`). Chair (56), couch (57),
  dining-table (60), tv, bottle… come out of the SAME forward pass — unfilter the classes = semantic
  object labels at ~zero added compute.
- **Depth already gives walls/clearance** geometrically (a wall = a large coherent close plane) — the
  Phase-0/2 forward-clearance reflex is exactly this; no model needed.
- **Rerun makes offline labeling free** — record rgb+depth, run heavy models offline (segmentation,
  open-vocab) with zero Orin budget, scrub the labels, build the dataset.

## Richness — split by WHERE it runs (recommended)
| Track | What | Where | Cost |
|---|---|---|---|
| **Backbone** | COCO objects (person/chair/couch/table/…) fused with depth → 3D; RANSAC depth-plane fit → floor/walls/free-space/clearance | live-distillable + offline | ~free (COCO) + cheap numpy (planes) |
| **Enrichment** | Structural **segmentation** (wall/floor/ceiling/doorway per-pixel) — the ONLY way to *label* a wall semantically vs. just a geometric plane | **offline only** | heavy |
| **Breadth (later)** | Open-vocab detection (cart, backpack, glass door) by text prompt — YOLO-World / Grounding-DINO | **offline only** | heavy |

Rationale: **avoidance consumes geometry** (you avoid the plane, not the word "wall"); **the thesis
dataset consumes semantics** (COCO + segmentation). Splitting by where-it-runs keeps the live budget
tiny and puts the heavy models offline where they are free.

## Architecture — four layers, kept separate (autonomy-planner)
1. **Perception (labeling):** rgb+depth → labeled obstacles.
   - *Object track:* multi-class COCO YOLO → 2D boxes + class.
   - *Geometry track:* RANSAC floor + vertical-plane fit on the depth cloud → floor / walls /
     free-space / above-floor obstacle points.
   - *Fusion:* lift each 2D box to 3D via depth + the pinhole (`focal_px`, HFOV 70°) → labeled
     obstacle (class, 3D position in the robot base frame, extent).
2. **Representation:** a compact **egocentric, instantaneous** obstacle set / forward-cone occupancy
   in the base frame (NO global map — no SLAM). Logged to Rerun offline; a small ROS msg live.
3. **Behavior (avoidance) — REFLEX layer:** graded forward-corridor braking, yaw untouched, optionally
   class-aware. Sits BESIDE the existing control law; it *detects + requests* a vx cap. It does NOT
   choose goals and does NOT own the safety floor.
4. **Enforcement (autonomy-ops, unchanged):** the no-depth→no-forward keystone + the staleness
   watchdog stay where they are. Never move the safety floor into the labeling.

## Phases (offline-first, dependency-ordered)
### Phase 0 — Recording substrate — ✅ DONE
Rerun records rgb + depth to `.rrd` (`k1_rerun.py`, live `--rerun`). That is the labeling input.
*Tweak:* for labeling we want depth logged richly — consider a `--rerun-depth-every-n` (default =
image-every-n) so depth isn't over-decimated; depth is the cheaper stream.

### Phase 1 — Offline labeling pipeline (laptop, ZERO Orin budget) — effort M
`_follow_autonomy/eval/rrd_label.py` (sibling of `rrd_to_lerobot.py`): read a follow `.rrd` → run the
label stack → write a **scrubbable labeled Rerun view** + a structured per-frame annotation file.
- Reuse `rrd_to_lerobot.read_rrd` (chunk-stream) to pull rgb + depth per `frame_idx`.
- Object track: multi-class YOLO (ultralytics, laptop CPU/GPU — offline, no Orin).
- Geometry track: back-project depth → point cloud via the pinhole; RANSAC floor plane → residual
  vertical planes = walls; above-floor clusters in the forward cone = obstacles.
- Fusion: per box, sample robust depth in the box → (range, bearing) → 3D; tag class.
- Output: `Boxes2D`(class labels) on `/camera/rgb`, `Points3D`/`Boxes3D` in a top-down 3D view,
  the floor/wall planes, + `obstacles.jsonl` (per frame: class, xyz, extent, in_corridor).
- **BUILT + VALIDATED 2026-07-04** (`eval/rrd_label.py`) on the real `follow.rrd`:
  - Object+depth fusion: 380 detections, 100% got a 3D range (person/chair/refrigerator/oven,
    fridge at 1.1 m).
  - `--track` (ByteTrack dedup): **536 raw detections → 10 unique obstacles** (6 person / 3 chair /
    1 fridge); the `seen`-frame count cleanly separates real obstacles (100+ frames) from transient
    ID-splits (2–5) — the Phase-2 "what matters" signal falls out for free.
  - `--seg` (SegFormer-ADE20K): `/camera/seg` per-pixel structure + `/world/wall` & `/world/floor`
    back-projected to 3D — the semantic wall/floor LABEL the geometry track can't give.
  - Honest finding: with the 70° FOV, ~100% of detections are "in corridor" (the whole view IS the
    corridor) — that filter only earns its keep with a wider sensor or near-center weighting.

### Phase 2 — "What actually matters" + the representation — effort S — ✅ BUILT + VALIDATED 2026-07-04
`eval/rrd_obstacles.py` turns the raw tracked detections (`obstacles.jsonl`) into a TRUE obstacle set
and the avoidance signal, and freezes the egocentric representation. On the real run:
**536 raw detections → 6 unique obstacles** (filter transient tracks by `seen`-count; MERGE by two
rules — co-location for stationary objects across any gap, endpoint-stitch for a moving object handed
to a new id; the two 2.5 m chairs correctly merged).

**Egocentric obstacle representation (frozen — `obstacle_set.json`), base frame x=right y=fwd z=up:**
```
Obstacle = {id, cls, range_stable, suspect_iddrift, spread_m, xyz, range_m, range_min_m,
            bearing_deg, persistence, first, last, merged_tracks, source}
ReflexSnapshot(per frame) = {frame_idx, nearest_corridor_m, nearest_class, n_in_corridor, brake}
```
`nearest_corridor_m` is THE scalar a graded-brake reflex acts on (∞ when the corridor is clear).

**Three findings that steer Phase 3 (the real payoff):**
1. **`range-stable` ≠ world-static (no odometry).** The FOLLOWED operator reads range-stable only
   because the robot holds standoff. You cannot split object motion from robot ego-motion in the
   egocentric frame — the labels are descriptive, not a world model. (The "3 persons" here are
   operator@1.6m + 2 bystanders, NOT a split, precisely because standoff pins the operator's range.)
2. **ID-drift/depth-glitch is the false-brake risk.** The one SUSPECT obstacle (a "chair" spanning
   0.5–4.2 m = furniture-that-moves = ID drift) and its spurious 0.5 m reading are the ONLY thing that
   trips the brake. → the reflex must gate on an **aged-median** range (reuse the follow's F1 anti-glitch
   gate), never a raw min. Same failure class the Rerun work exposed (the relock lunge).
3. **Geometry triggers, semantics modulate.** Every corridor obstacle had a depth return, so
   depth-clearance ALONE would brake; COCO adds the CLASS (person vs chair) for class-aware braking,
   not the trigger. → the live reflex = cheap depth forward-clearance (always) + unfiltered COCO (class),
   both off the control loop.

### Phase 3 — Live minimal reflex (on-robot, MEASURED + GATED) — effort M
Distill the cheapest useful thing onto the robot, on the cam-spin/decimated pattern (never the control
loop), loop-cost-gated exactly like Rerun:
- Unfilter COCO classes in `PersonDetector` (near-free) → is a labeled obstacle in the forward cone?
- Depth forward-clearance (a cheap numpy reduction over the forward corridor) → nearest obstacle range.
- Fuse → **graded vx cap** as the corridor closes; **yaw untouched**; **fail-to-stop with no depth**
  (composes with the existing `forbid_forward` keystone — reuse it, don't add a parallel path).
- Class-aware option (brake earlier for a person than a static chair) — geometry-first; semantics are
  a modifier, never the sole trigger.
- Ships default-off; a loop-cost gate (p50/p99 of the reflex's added ms, SLOW-LOOP streak vs baseline)
  before `--drive`, with an auto-disable backstop — the exact discipline the Rerun gate established.

### Phase 4 — Capture tier (thesis) — pipeline-proof ONLY — effort S
`obstacles.jsonl` + rgb/depth → a structured environment-labeled dataset. **Honest scope:** single
forward POV + flaky head depth is NOT the dual-POV/LiDAR/skeletal moat rig on any axis — label it
**internal / pipeline-proof** (same caveat as `LEROBOT_EXPORT.md`). It proves the labeling→dataset
plumbing the real rig will need, and gives the QA/labeling viewer (Rerun) a real workout.

## Safety spine (runtime-safety + autonomy-ops) — non-negotiable
- The reflex **detects + requests** a vx cap; **enforcement** (no-depth→no-forward, staleness
  watchdog, e-stop) stays in ops. A planning/labeling bug must not be able to reason the floor away.
- **Labeling is NEVER a safety dependency:** a model crash / missing weights degrades to the existing
  depth-clearance (or stop) — the same contract as Rerun (`_NullRR`).
- **Never steer around** (swings the operator out of the ~70° FOV → loses the lock). Graded brake only.
- Compose with `forbid_forward` (the sacred vx≤0 keystone), never a second forward-authorizing path.

## Honest limits (keep stapled on — embodied-ai-advisor)
- Instantaneous egocentric, **no map/memory** (no SLAM/odometry).
- **Forward cone only** — blind to sides, thin/glass/overhead, and drop-offs/negative obstacles.
- Semantic labels **do not fix geometric blindness** — a mislabeled or unlabeled obstacle with a valid
  depth return still brakes; a real obstacle with no depth return (glass, thin, out-of-cone) is missed.
- Capture is **single-POV** — not the thesis moat data; pipeline-proof only.

## Thesis-fit (honest)
- **TRUE:** a genuine capability step (the robot starts to *understand* its space) + the first
  semantic+geometric environment labels flowing through the capture pipeline.
- **SELLABLE:** proves the environment-labeling → dataset plumbing the real dual-POV/LiDAR/skeletal rig
  needs, and that Rerun is the QA/labeling viewer for it.
- **CAVEAT (diligence-killer):** do NOT pitch this as "the robot maps/avoids obstacles" — it is a
  forward-cone braking reflex + an egocentric label stream, no map, no LiDAR. Sell the *pipeline*, not
  a mapping/nav claim.

## Open decisions (yours)
1. Live-reflex class set: geometry-only (depth clearance, class-agnostic) vs. COCO-class-aware braking?
2. Segmentation model for the offline structural layer (SegFormer-B0 vs FastSAM vs Mask2Former) —
   decide after Phase-1 numbers.
3. Do we want a live ROS obstacle message published for other consumers, or keep it internal to the
   reflex for now?
