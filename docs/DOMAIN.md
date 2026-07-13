# DOMAIN.md — captured-space domains: artifact contract + the Phases 9–12 handoff

**What a "domain" is:** a reconstructed real scene + a randomization distribution around it
(PHASE_6-8_PLAN P8.5). The scene is the *real garage* (later: any customer space) captured by the
robot itself — the C2 thesis dogfooded: captured space → training/eval world. This doc is the
contract a fresh Phase 9–12 session imports a domain against, without asking clarifying questions
(P6G.5 gate). Written 2026-07-13 on the desktop; owners: sim-eval (spec) + xr-engineer (artifacts).

## 1. The artifact contract — what `runs/<run_id>/recon/` carries

Produced by the recon container (`k1recon`, digest = `recon_version` in `recon_manifest.json`;
frozen schema in `docs/RECON_CONTRACT.md`). Frames per `docs/FRAMES.md`: **metres, right-handed;
`run_local` = that run's odom-origin frame until P8.3 aligns runs into the persistent `map` frame.**
Every artifact names its frame in the manifest; an untagged artifact is a bug.

| artifact | stage | frame | role in the twin |
|---|---|---|---|
| `recon_manifest.json` | always | — | authoritative record: stages, metrics, digest, seed |
| `trajectory.jsonl` | odom | run_local | camera path (`wall_t` + `T_run_local_camera` 4×4 row-major) |
| `pose_graph.json` | odom | run_local | nodes + loop-closure edges (debug/re-optimization) |
| `mesh_visual.ply` | tsdf | run_local | full-res colored TSDF mesh (source of truth geometry) |
| `mesh_visual_decimated.ply` | simexport | run_local | quadric-decimated **visual** layer |
| `mesh_collision/part_*.obj` | simexport | run_local | CoACD convex parts — **each watertight by construction**; the **collision** layer (raw TSDF meshes are NOT sim-ready; use these, never mesh_visual) |
| `apriltag_observations.json` | align | camera (per-tag `anchor_<id>`) | ground-truth anchors: per-tag `T_cam_tag` + reproj error |
| `point_cloud.ply` (3DGS) | splat (image v2, `k1splat`) | run_local → map | photoreal **visual/render** layer; standard 3DGS ply (xyz, f_dc_*, opacity, scale_*, rot_*) |

**Trust rules:** only consume artifacts whose stage is `status:"ok"` with a **pinned** (non-`unset`)
`image_digest`; metric stages must show `intrinsics_source: calibrated` for metric-grade geometry
(approximate-intrinsics runs are development-grade — usable, flagged, not customer-facing).
GPU-stochastic artifacts (the splat) are metric-gated (held-out PSNR/SSIM, seed + digest recorded),
never hash-gated.

## 2. Layer assignments — who consumes what (name the interface now, build it then)

- **Phase 10 — the sim twin's scene:** `mesh_collision/part_*.obj` = the collision layer (import
  each convex part as its own collision geom); `mesh_visual_decimated.ply` (or the splat, where the
  renderer supports 3DGS) = the visual layer. Mesh + splat are **co-registered by construction**
  (same poses, no COLMAP) — one frame, two layers. MuJoCo import: one body, N `mesh` geoms
  (convex parts) + 1 visual mesh with `contype=0 conaffinity=0`. Isaac: same split via USD;
  convex parts as collision approximations `none` (they are already convex).
  *Gate (P8.5):* the mesh loads in the target sim and a K1-sized capsule (r≈0.25 m, h≈1.1 m)
  traverses the floor without collision artifacts.
- **Phase 11 — nav substrate:** the persistent `map` frame (P8.3) feeds goto/patrol. **Interface
  (named now, built then): a 2D occupancy grid projected from the collision mesh over the K1
  footprint height band `z ∈ [0.05, 1.20] m`, resolution 0.05 m/cell, origin = `map` origin,
  free/occupied/unknown ternary, PGM+YAML (ROS map_server convention).** Consumers: polygon
  geofence (P8.4b) and the mission planner.
- **Phase 12 — demo/render:** the splat `.ply` + a web viewer preview (investor asset). Any
  gsplat-class viewer renders the standard 3DGS ply; keep the map-frame transform sidecar with it.
- **Phase 13 (if ever) — photoreal domains at scale:** this substrate is the foundation; see the
  parked track below.

## 3. Randomization axes (the P8.5 spec around the fixed scene)

The domain distribution = the captured scene ⊗ these axes (defaults for the twin initiative;
tune per experiment, record per eval):

| axis | distribution (v1 defaults) | rationale |
|---|---|---|
| lighting | intensity ×[0.5, 2.0], color temp [3000, 6500] K, 0–2 extra point lights | garage lighting varies; splat bakes one exposure |
| obstacle placement | 0–5 props from a fixed asset set, poses uniform over free floor (occupancy-checked) | follow must generalize to clutter |
| human agents | 1 operator (always) + 0–2 bystanders, scripted walk cycles ≤ 1.5 m/s | the P8.2a bystander gap, exercised in sim first |
| start pose | uniform over free space, yaw uniform | episode diversity |
| sensor noise | depth: zero-mean σ=0.01·z, dropout 2%; RGB: exposure jitter ±0.5 EV | matches the rig's real depth flakiness |
| dynamics | friction ×[0.8, 1.2], K1 mass ±5% | sim-to-real humility |

**Not in scope here:** building the twin or the randomization suite (the separate C2 program);
this doc only fixes the interfaces + distributions they code against.

## 4. Substrate portability + parked tracks

- **Portable by construction:** every consumer runs the pinned images (`k1recon` /`k1splat` by
  digest) — the same jobs run unchanged on this desktop, a DGX-Spark-class box, or a cloud GPU;
  the pool (docs/CLUSTER_PLAN.md) treats them all as workers. The laptop stays source of truth.
- **Parked — Cosmos Transfer:** the offload bundles already carry exactly what Cosmos-class
  models consume (RGB + depth + poses + trajectory) to mint photoreal synthetic variants of real
  runs — same capture, second consumer. Revisit when a Phase 13 materializes; nothing else
  currently blocks on it.

## 5. Status honesty (what exists today vs data-blocked)

- **Real now (synthetic-validated, VERIFY ON DESKTOP for real-garage accuracy):** the full recon
  stage pipeline (odom/tsdf/simexport/align, P6G.2), the splat pipeline (P6G.4), the pool that
  runs them (P6G.3), this contract.
- **Data-blocked:** the `map` frame + cross-run merge (P8.3 needs ≥ 2 real runs, different days),
  real-garage meshes/splats, the occupancy projection, the P8.5 sim-import gate. Unblocks the day
  a room-coverage odom-enabled capture run lands in `runs/`.
