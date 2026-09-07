#!/usr/bin/env python3
"""Build a FRESH map from the head-mounted Aurora, then offload it into the local map -- one session.

The pre-built map was made HANDHELD, so its viewpoint does not match the head-mounted camera and
relocalization diverges. This builds the map from the exact viewpoint + motion it will run against:
reset -> mapping mode -> you DRIVE the robot through the space while the map grows -> offload (3D +
2D + full .vslam). Everything in one SDK session, because a device map does not persist across a
disconnect -- it must be downloaded before we drop the link.

  python3 aurora_mapping_run.py [--conn 192.168.11.1] [--out ~/localmap] [--map-s 180] [--res 0.10]

DRIVE the whole --map-s window: cover the space smoothly, revisit a spot or two (loop closure), keep
motion gentle (head-mounted VIO hates jerks). Watch the MP/KF counts climb -- that is the map building.
Output: ~/localmap/headmap_<ts>/ with localmap_3d.ply, localmap_2d.npy/.png, map_<ts>.vslam; and
~/localmap/latest_* copies. Then test it with aurora_upload_reloc.py map_<ts>.vslam and confirm a SANE,
stable pose before anything touches the follow's obstacle brake.
"""
import argparse
import os
import sys
import time

import numpy as np
import slamtec_aurora_sdk as sdk

# reuse the offload helpers (same dir on the robot)
try:
    from aurora_offload_map import xyz, write_ply, project_2d, save_png
except Exception:  # noqa: BLE001 -- allow --help without the sibling present
    xyz = write_ply = project_2d = save_png = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="192.168.11.1")
    ap.add_argument("--out", default=os.path.expanduser("~/localmap"))
    ap.add_argument("--map-s", type=float, default=180.0, help="seconds to drive/build before offload")
    ap.add_argument("--res", type=float, default=0.10)
    ap.add_argument("--height-min", type=float, default=0.15)
    ap.add_argument("--height-max", type=float, default=2.0)
    a = ap.parse_args()
    if xyz is None:
        print("aurora_offload_map.py must sit next to this script (offload helpers).")
        return 2

    s = sdk.AuroraSDK()
    try:
        s.connect(connection_string=a.conn)
    except Exception as e:  # noqa: BLE001
        print("CONNECT-FAIL:", type(e).__name__, e)
        return 1
    print("connected:", s.is_connected())
    ctl, dp, mm = s.controller, s.data_provider, s.map_manager

    # fresh map from scratch
    try:
        ctl.require_map_reset(timeout_ms=8000)
        print("map reset")
    except Exception as e:  # noqa: BLE001
        print("map-reset warn:", type(e).__name__, e)
    try:
        ctl.require_mapping_mode(timeout_ms=8000)
    except Exception:  # noqa: BLE001
        try:
            s.require_mapping_mode(timeout_ms=8000)
        except Exception as e:  # noqa: BLE001
            print("MAPPING-MODE-FAIL:", type(e).__name__, e)
            s.disconnect()
            return 1
    try:
        s.enable_map_data_syncing(True)
    except Exception:  # noqa: BLE001
        pass
    print("MAPPING MODE -- DRIVE THE ROBOT through the space now for %.0fs (smooth, cover it, revisit)"
          % a.map_s)

    t0 = time.monotonic()
    last = 0
    while time.monotonic() - t0 < a.map_s:
        try:
            gi = dp.get_global_mapping_info()
            mpc = int(gi.get("totalMPCount", 0))
            kfc = int(gi.get("total_kf_count", gi.get("totalKFCount", 0)))
            el = time.monotonic() - t0
            if mpc != last:
                print("  t=%3ds  map points=%d  keyframes=%d" % (el, mpc, kfc), flush=True)
                last = mpc
        except Exception as e:  # noqa: BLE001
            print("  info err:", e)
        time.sleep(2.0)

    # OFFLOAD in the same session (map would clear on disconnect)
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(a.out, "headmap_" + ts)
    os.makedirs(run_dir, exist_ok=True)
    try:
        d = s.get_map_data()
        mp = [p for p in (xyz(x) for x in d.get("map_points", [])) if p]
        kf = [p for p in (xyz(x) for x in d.get("keyframes", [])) if p]
    except Exception as e:  # noqa: BLE001
        mp, kf = [], []
        print("get_map_data err:", type(e).__name__, e)
    if mp:
        pts = np.array(mp, dtype=np.float64)
        kfp = np.array(kf, dtype=np.float64) if kf else np.empty((0, 3))
        write_ply(os.path.join(run_dir, "localmap_3d.ply"), pts, kfp)
        ext = pts.max(0) - pts.min(0)
        print("3D: %d landmarks + %d keyframes (extent %.1f x %.1f x %.1f m)"
              % (len(pts), len(kfp), ext[0], ext[1], ext[2]))
        grid, meta = project_2d(pts, a.res, a.height_min, a.height_max, 1)
        if grid is not None:
            np.save(os.path.join(run_dir, "localmap_2d.npy"), grid)
            save_png(grid, os.path.join(run_dir, "localmap_2d.png"))
            print("2D: %dx%d @ %.2fm, %d occupied cells" % (meta["w"], meta["h"], meta["res"],
                                                            int((grid == 255).sum())))
    else:
        print("NO map points captured -- did the robot move / did mapping run?")
    vslam = os.path.join(run_dir, "map_%s.vslam" % ts)
    try:
        print("downloading map -> %s" % vslam)
        mm.start_download_session(vslam)
        while mm.is_session_active():
            time.sleep(1.0)
        print("download-ok")
    except Exception as e:  # noqa: BLE001
        print("download failed:", type(e).__name__, e)
    try:
        import shutil
        for name in ("localmap_3d.ply", "localmap_2d.npy", "localmap_2d.png"):
            src = os.path.join(run_dir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(a.out, "latest_" + name))
    except Exception:  # noqa: BLE001
        pass
    s.disconnect()
    print("MAPPING-RUN DONE -> %s" % run_dir)
    print("NEXT: aurora_upload_reloc.py %s  and confirm a SANE, stable pose before any brake use." % vslam)
    return 0


if __name__ == "__main__":
    sys.exit(main())
