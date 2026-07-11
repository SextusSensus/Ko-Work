# FRAMES.md — coordinate frames & calibration for P8 (map / reconstruction)

P8.1 groundwork (skill: xr-engineer). **Every P8 artifact must state which frame it is in.** Getting
handedness/units/scale wrong here silently corrupts every downstream mesh (the classic 1000× garage
from a mm-vs-m slip), so this is load-bearing, not boilerplate.

Conventions follow **ROS REP-103**: right-handed, **metres**, **radians**. Two families:
- **Body/world frames** (odom, base, map, anchor): **X-forward, Y-left, Z-up**.
- **Camera optical frame**: **Z-forward (into the scene), X-right, Y-down** (the standard vision optical
  convention — note it differs from the body convention; the static camera↔base transform bridges them).

---

## 1. Frame catalog

| Frame | Definition | Source |
|---|---|---|
| `map` | The persistent multi-run garage frame (P8.3). One canonical origin all merged runs align into. | Defined by P8.3 pose-graph (first anchored run seeds it) |
| `anchor_i` | An AprilTag's frame, `i` = its built-in tag ID (e.g. `DICT_APRILTAG_36h11`). Fixed on a wall. | Ground-truth reference for P8.3 (not a merge dependency) |
| `odom` | Planar base odometry frame. Continuous, **drifts** (legged), accumulates across sessions (reset via `/zero_pose_setReq`). | `/odometer_state` = `booster_interface/msg/Odometer {x,y,theta}` (P6.1a, recorded when captured) |
| `base` | The robot trunk (`trunk_link`), on the floor plane. Planar SE(2) in `odom` + constant trunk height in Z. | odom (x,y,θ) + fixed trunk height |
| `camera` | The head **color optical** frame (`head_color_optical_frame`). RGB + depth are expressed here. | intrinsics (P6.1 / P8.1) + the base→camera chain below |

**Camera-in-world compose** (per-frame, for stitching egocentric RGBD into `map`):

```
T_map_camera = T_map_odom ∘ T_odom_base(x,y,θ; z=trunk_h) ∘ T_base_head ∘ T_head_camera
```
- `T_map_odom` — from P8.3 cross-run alignment (identity within a single anchored run's local frame).
- `T_odom_base` — planar SE(2) from `/odometer_state`, lifted to SE(3) with constant trunk height Z
  (**flat-floor assumption** — breaks on stairs/slopes; P8.2 RGBD odometry is the real pose source,
  odom is a prior/consistency check only).
- `T_base_head` — trunk→head, from `/head_pose_stamped` (`geometry_msgs/PoseStamped`, head in trunk
  frame, z≈0.88 m) or the URDF/tf chain trunk→Head_1→head_pitch_link→…→head_color_optical_frame.
- `T_head_camera` — static optical transform (body→optical axis swap), from tf/URDF.

**Do NOT integrate commanded velocity for pose** (slip + gait make it fiction — P8.2). Odometry is a
prior; RGBD odometry + loop closure is the pose of record.

> **`/tf` caveat (from on-robot verification):** the K1's `/tf` is **body-relative only**, rooted at
> `trunk_link` — it carries trunk→head but **no odom/world frame**. World pose comes from
> `/odometer_state`, not tf. Don't expect a `map`/`odom` frame in tf.

---

## 2. Calibration file

P8.2/P8.3 need metric intrinsics. The recorder already emits an **approximate** seed; P8.1 upgrades it.

### 2.1 What P6.1 records (the seed) — `intrinsics.json`, per capture run

```json
{ "model": "pinhole_from_hfov", "approximate": true,
  "width": W, "height": H, "hfov_deg": HFOV,
  "fx": F, "fy": F, "cx": W/2, "cy": H/2, "note": "..." }
```
`approximate: true` — derived from `--hfov-deg` (`fy:=fx`, principal point assumed centre, **no
distortion**). Good enough to document a run; **not** good enough for metric TSDF to trust.

### 2.2 What P8.1 produces — `models/calibration.json` (the real intrinsic, checked into `models/`)

A single, run-independent calibration for the head camera. Format (superset of the seed; downstream
prefers this when `approximate:false`):

```json
{ "model": "pinhole",
  "approximate": false,
  "source": "checkerboard_2026-07-xx" | "factory",
  "width": W, "height": H,
  "fx": FX, "fy": FY, "cx": CX, "cy": CY,
  "distortion_model": "plumb_bob",
  "distortion": [k1, k2, p1, p2, k3],
  "depth_scale_m_per_unit": 0.001,      // depth raw-unit -> metres; VALIDATE against a known distance
  "reproj_error_px": <rms>,
  "resolution_note": "pin the capture resolution -- 448x544 vs 480x640 differ across runs" }
```

**How to produce it (DESKTOP / OpenCV — `VERIFY ON WORKSTATION`):** capture a handful of checkerboard
frames held in front of the head camera at the capture resolution, run `cv2.calibrateCamera`, write the
result here. (Factory intrinsics, if obtainable from the Booster SDK, are an acceptable substitute —
record `source:"factory"`.) This is **separate from and unrelated to** the wall AprilTags (those are
map ground-truth, not lens calibration).

### 2.3 Gate

`calibration.json` validates against a recorded run (resolution matches; fx/fy plausible for the HFOV),
**and the depth scale/units are validated against a known distance** — measure a tag or use a tape
measure and confirm the depth read in metres matches. This is where the silent mm-vs-m bug dies, not as
a 1000× mesh in P8.3.

---

## 3. Artifact frame-tagging rule

Every P8 output names its frame in its metadata: a per-run mesh/trajectory (P8.2) is in that run's
**local** frame (seed = its odom origin); the merged garage mesh + persistent map (P8.3) are in `map`;
AprilTag poses are `anchor_i` (and their `map` pose once aligned). A P8 artifact with no stated frame is
a bug.
