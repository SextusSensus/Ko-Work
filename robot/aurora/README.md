# Aurora A1M1 integration -- capability map (2026-09-07)

SDK: slamtec-aurora-python-sdk 2.1.1, installed on both the robot (linux-aarch64) and the laptop
(win64). The device connects and serves its SDK **on the laptop** (192.168.11.1, reached via the
DHCP lease the Aurora hands the host).

## What works (proven on the laptop)
- `save_vslam.py`   -- download the device map -> map.vslam (got 41 MB / 8786-point map)
- `export_3d.py`    -- export the sparse 3D landmark cloud -> map_3d.ply (viewable in the app)
- `sfm_record.py`   -- record a **RAW** dataset (stereo images + IMU + timestamps). ~15 fps/cam.
- `aurora_pose_probe.py` / `aurora_check.py` -- connect + live pose / import + API-surface probes
- App "SLAM Map" tab -- renders .stcm/.vslam (2D grid) and .ply (3D cloud)

## What is BLOCKED (device/firmware, not the tools)
1. **COLMAP recording fails with error -2 (INVALID_ARGUMENT)** -- confirmed against the OFFICIAL SDK
   example, so it is the device, not our code. The dense-cloud path needs COLMAP:
   aurora_map_densifier.exe reads a COLMAP `sparse/cameras.bin` + `images/`, which only the COLMAP
   recorder produces. RAW recording works but is the wrong layout for the densifier. Net: no dense
   cloud from this device on the current firmware without a RAW->COLMAP converter (see below).
   Signals pointing at a firmware capability gap: get_device_info -> NOT_READY (-7),
   mapping_flags == 0, device status (0,0).
2. **The robot cannot reach the Aurora SDK.** On the robot's wired link the device answers ping+SSH
   at 192.168.127.10 but serves NO SDK port; the SDK server the laptop reaches at 192.168.11.1 is
   not exposed over the robot's static-IP link. Until this is solved, nothing SDK-based (pose,
   recording, upload/relocalize) runs on the robot. Likely fix to try WITH the device on the robot:
   set usb_eth0 to DHCP so it negotiates a 192.168.11.x lease like the laptop does.

## Not done, and why
- **Dense 3D cloud**: blocked by (1). Options: get COLMAP recording working (firmware/SLAMTEC
  support), or write a converter (map.vslam keyframe poses + RAW images -> COLMAP sparse/ -> densify)
  -- real work, coordinate-frame + timestamp matching, not built speculatively.
- **Follow-mode integration**: blocked by (2), and additionally would need live relocalization for a
  map prior. The honest first integration once the link works is the Aurora's live depth/LiDAR as an
  EGOCENTRIC obstacle input to the brake -- no localization required. Not wired into
  follow_person_k1.py; nothing touches velocity until proven on hardware.
