# Aurora A1M1 integration -- capability map (updated 2026-09-07 pm)

SDK: slamtec-aurora-python-sdk 2.1.1, on both the robot (linux-aarch64) and the laptop (win64). The
Aurora serves its SDK over ethernet at **192.168.11.1**. The robot reaches it via its RJ45
(`enP9p1s0`) with a non-destructive secondary IP (keeps the 192.168.10.102 control net):
`sudo ip addr add 192.168.11.50/24 dev enP9p1s0`. The K1 has no free USB port; ethernet is the link.

## What works (PROVEN)
- Robot <-> Aurora SDK over ethernet: `192.168.11.1:1445` open from the robot, `connect()` OK.
- `aurora_upload_reloc.py` -- upload map.vslam -> pure-localization mode -> relocalize -> pose. On the
  laptop (device handheld + moving) this RELOCALIZED: real pose (0.66, 0.89), ~95 Hz, map frame z-up.
- `aurora_odom_bridge.py` -- republishes the relocalized pose as `booster_interface/msg/Odometer` on
  `/aurora_odom`, publishing ONLY while relocalization == SUCCEED + a pose-jump guard (fail-closed).
- `aurora_offload_map.py` -- **offload any run's map into the robot's local map (3D + 2D).** Pulls
  get_map_data (enable_map_data_syncing + wait), writes `localmap_3d.ply` (sparse landmarks) and
  `localmap_2d.npy/.png` (x,y projection of the height-band points), downloads the full `.vslam`,
  timestamped under `~/localmap/run_<ts>/` + `~/localmap/latest_*`. Repeatable, one command per run.
- `save_vslam.py` / `export_3d.py` / `sfm_record.py` (RAW) / `aurora_pose_probe.py` / `aurora_check.py`.
- App "SLAM Map" tab renders .stcm/.vslam (2D grid) and .ply (3D cloud).
- The pre-built map is already offloaded to the robot at `~/localmap/` (2D grid 994x1151 @ 5cm =
  49.7x57.6 m; 3D cloud 8528 pts). Verified API + z-up + connection recipe in
  `captures/AURORA_CAPTURE_SUMMARY_2026-09-07.txt` (laptop) and DECISIONS.md.

## THE BLOCKER for follow/avoidance integration: head-mounted pose DIVERGES
On the robot the Aurora is **head-mounted**. A robot run relocalized (status SUCCEED) but the pose
then blew up to garbage (x/y/z into the millions) within seconds. Root cause, most likely: the map was
built HANDHELD, so its viewpoint does not match the head-mounted camera; add the walking gait +
head pan/tilt motion and the visual-inertial track diverges. Two consequences:
1. A divergent pose CANNOT feed the obstacle brake -- it would place PHANTOM WALLS (the map's
   obstacles at wildly wrong positions) -> the robot brakes on nothing / freezes / steers off open
   space. This violates the standing rule: the Aurora map must never reach the brake without a
   trustworthy live pose. (The bridge's SUCCEED-gate + jump-guard already refuse these frames, so it
   degrades to live-depth-only -- safe, but non-functional.)
2. The head-mount also means the reported pose is the HEAD's, not the body's; the localmap needs the
   BODY pose, so the head pose must be composed with the live head-pan/tilt -- a moving extrinsic,
   not the single `--yaw-offset-deg` scalar that a rigid body-mount would allow.

## THE FIX that unblocks integration: re-map FROM THE HEAD
Drive the robot once through the space with the Aurora on the head in MAPPING mode, so the map is
built from the exact viewpoint + motion profile it will relocalize against. Then relocalization should
lock with a SANE, stable pose (no divergence). `aurora_offload_map.py` captures that run's map.
Only after a sane pose is proven does map-into-avoidance become safe.

## Integration plan (in strict order; do not skip ahead)
- **A. pose -> localmap** (safe first step): once a sane relocalized pose exists, run the bridge and
  `follow ... --odom-topic /aurora_odom --localmap on`. The localmap's invariant (memory may only
  REDUCE clearance) bounds the worst case to over-braking. Prereq: sane pose from a head-built map.
- **B. map -> avoidance**: seed the localmap from the offloaded 2D grid, transformed by the live pose,
  reloc-confidence-gated + purge-on-loss. NOT started -- builds on a proven A. Never a phantom wall.

## Not done, and why
- **Dense 3D cloud**: COLMAP recording is device-blocked (error -2); the SDK gives sparse visual
  landmarks only. A RAW->COLMAP converter is the path, not built speculatively.
- **Map-into-avoidance (B)**: gated on a sane head-mounted pose (re-map from the head), then A proven.
