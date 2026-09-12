# Local Map ↔ Rerun exact placement

Pose-correct pipeline from capture `.rrd` / labeling into Local Map domain assets.
Mesh choice stays with the free ontology (`docs/localmap-assets.md`); this doc owns **frame math + Rerun I/O**.

## Architecture

```
.rrd / run folder / synthetic fixture
        │
        ▼
eval/localmap_rerun_ingest.py
  • prefer /map/{x,y,theta}, else /odom/{x,y,theta}
  • write domains/<id>/runs/<run_id>/manifest.json + poses.jsonl
  • optional depth.npz + labels.jsonl copy
        │
        ▼
eval/localmap_asset_placer.py   ← eval/localmap_frames.py (pure SE(3)/pinhole)
  • 3D label     → T_map_cam / identity   (placement_method: transform_3d)
  • 2D + depth   → K + ROI median Z       (placement_method: depth_backproject)
  • 2D no depth  → ONLY with explicit --allow-ground-raycast or label
                   needs_ground_raycast    (placement_method: ground_raycast)
                   else REFUSE (no silent guess)
  • merge idempotent by run_id + detection_id → instances.json
        │
        ▼
desktop/localmap-viewer/asset-placer.js
  • pose_map + planar (x,y,yaw); importExactInstances / registerDetections
```

| Module | Role |
|--------|------|
| `eval/localmap_frames.py` | `T_map_base`, `T_base_optical`, `T_map_cam`, back-project, ground raycast, quats |
| `eval/localmap_asset_placer.py` | Labels → instances; CLI merge into domain |
| `eval/localmap_rerun_ingest.py` | `.rrd` / fixture → run provenance + optional place |
| `eval/localmap_rerun_selftest.py` | 1e-6 golden tests (no rerun SDK) |
| `desktop/localmap-data/fixtures/rerun_exact_place/` | Tiny synthetic golden |
| `desktop/Import-RerunToDomain.ps1` | Desktop hook calling the Python CLI |

Existing helpers reused (not rewritten): `eval/rrd_to_lerobot.read_rrd`, `eval/rrd_poses.py`, `eval/rrd_map.py` transform conventions.

## Frame convention

| Frame | Axis | Notes |
|-------|------|-------|
| **map / odom / base** | **FLU** — X fwd, Y left, Z up | ROS REP-103; units **meters**; yaw about +Z |
| **camera optical** | **RDF** — X right, Y down, Z fwd | Pinhole |
| **Viewer (Three.js)** | Y-up | map `(x,y)` → Three `(x,z)`; `rotation.y = -yaw_map` |

Homogeneous composition (column vectors):

```
T_map_cam = T_map_base(x, y, θ, z=0) @ T_base_optical(cam_h, pitch_deg)
p_map     = T_map_cam @ p_cam
```

`T_base_optical` matches `eval/rrd_map.T_base_optical`: mount height `cam_h`, downward pitch in degrees.

**Asset yaw:** prefer `detection_yaw` when present (already map-frame); else align asset +X to robot heading `θ`. Stored as `pose_map.{qw,qx,qy,qz}` (pure yaw) plus planar `yaw` for the viewer.

**Footprint meshes** sit on the floor at `(x,y)`; measured `pose_map.z` is kept for provenance. Lift to measured Z only with `use_measured_z` / `placement: free`.

Run manifest fields (`domains/<id>/runs/<run_id>/manifest.json`):

```json
{
  "frame": "odom",
  "units": "meters",
  "axis": "FLU",
  "camera_optical_axis": "RDF",
  "cam_h_m": 1.35,
  "cam_pitch_deg": 8.0,
  "source_path": "/abs/path/to/capture.rrd",
  "pose_count": 1234
}
```

## Commands

### Synthetic fixture (cloud / CI — no `.rrd`)

```bash
python3 eval/localmap_rerun_selftest.py

python3 eval/localmap_rerun_ingest.py \
  --domain exact-lab \
  --fixture desktop/localmap-data/fixtures/rerun_exact_place \
  --allow-ground-raycast
```

### Real capture `.rrd`

```bash
# 1) poses only (also: python eval/rrd_poses.py capture.rrd --out poses.jsonl)
python3 eval/localmap_rerun_ingest.py \
  --domain warehouse-bay-a \
  --rrd /path/to/capture.rrd \
  --cam-h 1.35 --cam-pitch 8

# 2) ingest + place from Batch-Label / rrd_label obstacles.jsonl-shaped labels
python3 eval/localmap_rerun_ingest.py \
  --domain warehouse-bay-a \
  --rrd /path/to/capture.rrd \
  --labels /path/to/labels.jsonl \
  --place \
  --allow-ground-raycast   # only if you accept ground-plane fallback

# Or place into an already-ingested run:
python3 eval/localmap_asset_placer.py \
  --domain warehouse-bay-a --run-id <run_id> \
  --labels domains/.../runs/<run_id>/labels.jsonl \
  --poses  domains/.../runs/<run_id>/poses.jsonl \
  --depth  domains/.../runs/<run_id>/depth.npz \
  --data-root desktop/localmap-data
```

### Desktop (Windows)

```powershell
cd desktop
.\Import-RerunToDomain.ps1 -Domain warehouse-bay-a -Rrd "D:\runs\capture.rrd" -Labels "D:\runs\labels.jsonl" -Place
# Fixture:
.\Import-RerunToDomain.ps1 -Domain exact-lab -Fixture
```

### Viewer

```bash
cd desktop && python3 -m http.server 8765
# http://127.0.0.1:8765/localmap-viewer/index.html?domain=exact-lab
```

```js
// Exact Lab ships empty — Import -Fixture (or a real .rrd) writes instances.json.
// Live / re-merge after a place:
k1LocalMap.importExactInstances([/* placer JSON rows */]);
```

## Label → asset (exact)

Instance fields written by the placer:

| Field | Meaning |
|-------|---------|
| `label` / `label_class` | Detection class (ontology resolves `asset_id`) |
| `pose_map` | `{x,y,z,qw,qx,qy,qz}` in map/odom FLU |
| `x,y,yaw` | Planar viewer-compat |
| `T_source` | `"map"` or `"odom"` |
| `run_id`, `detection_id`, `t_ns` | Provenance; merge key = `run_id::detection_id` |
| `placement_method` | `transform_3d` \| `depth_backproject` \| `ground_raycast` |
| `covariance` / `confidence` | Raycast carries incidence-based covariance |

Re-running the placer after better labels **replaces** the same key — no duplicates.

## Limitations (fail-closed)

- **No depth + no `--allow-ground-raycast`:** 2D boxes are refused. Furniture will not float at a guessed range.
- **Ground raycast:** needs known `cam_h` + pitch (logged pose / CLI). Grazing / behind-camera rays refuse. Marked `placement_method: "ground_raycast"` with residual.
- **No `/odom` or `/map` in `.rrd`:** ingest exits loud (same as `rrd_poses.py`). Re-capture with `--odom-topic`.
- **Cloud VM:** typically no real `.rrd` / `rerun-sdk`. Use the fixture + selftest.
- **Planar odom:** flat-floor SE(2); legged drift; no loop closure — local map, not global SLAM.
- **`cam_h` / pitch:** not yet logged in every `.rrd`; CLI defaults (override per rig). Prefer values beside the recording when present (`intrinsics.json`).
