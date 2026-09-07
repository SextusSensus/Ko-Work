#!/usr/bin/env python3
"""Offload the Aurora's current map off the device into the robot's LOCAL MAP, as 3D + 2D layers.

Run ON THE ROBOT after a scan/run (the Aurora is head-mounted and connected at 192.168.11.1 via the
enP9p1s0 192.168.11.50 alias). One command per run -- repeatable, so every future run's data lands in
the same local-map folder.

  python3 aurora_offload_map.py [--conn 192.168.11.1] [--out ~/localmap]
                                [--seed-map ~/map.vslam] [--res 0.10]
                                [--height-min 0.15] [--height-max 2.0] [--min-pts 1] [--sync-s 45]

Produces, timestamped under <out>/run_<ts>/ and copied to <out>/latest_*:
  localmap_3d.ply   sparse 3D landmark cloud (get_map_data map_points, + keyframe camera path in red)
  localmap_2d.npy   2D occupancy grid: map_points in the [height-min,height-max] band binned at --res
  localmap_2d.png   3-class preview (occupied / free-ish / unknown)
  map_<ts>.vslam    full device map (download) -- for re-upload / relocalization of THIS run

The map frame is z-up (ground plane = x,y), confirmed on-device -- so 2D is a straight x,y projection
with a height-band filter (floor + ceiling drop out, wall/object-height structure remains). The cloud
is the Aurora's SPARSE visual landmarks, not a dense surface, so the 2D grid is a sparse obstacle
prior, not a dense wall map. It is the map DATA on the robot; wiring it into the follow's obstacle
brake still needs a trustworthy live pose (head-mounted VIO is currently diverging -- see DECISIONS).

Reuses export_3d.py's map-data sync + xyz() approach; adds the 2D projection, timestamping, and the
map download so a run is captured completely.
"""
import argparse
import os
import sys
import time

import numpy as np
import slamtec_aurora_sdk as sdk


def xyz(dpt):
    """(x,y,z) from a map_point/keyframe dict, however it names them (verbatim from export_3d.py)."""
    if not isinstance(dpt, dict):
        return None
    for kx, ky, kz in (("x", "y", "z"), ("px", "py", "pz")):
        if kx in dpt:
            return (float(dpt[kx]), float(dpt[ky]), float(dpt[kz]))
    for kp in ("position", "world_position", "translation", "pose"):
        v = dpt.get(kp)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return (float(v[0]), float(v[1]), float(v[2]))
        if isinstance(v, dict) and "x" in v:
            return (float(v["x"]), float(v["y"]), float(v["z"]))
    return None


def write_ply(path, pts, kfp):
    with open(path, "w") as f:
        f.write("ply\nformat ascii 1.0\nelement vertex %d\n" % (len(pts) + len(kfp)))
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
        for p in pts:
            f.write("%.4f %.4f %.4f 220 220 220\n" % (p[0], p[1], p[2]))
        for p in kfp:
            f.write("%.4f %.4f %.4f 230 60 40\n" % (p[0], p[1], p[2]))


def project_2d(pts, res, hmin, hmax, min_pts):
    """Sparse map_points -> a 2D occupancy grid. Keep the wall/object height band (z up), bin x,y,
    a cell is occupied when >= min_pts landmarks fall in it. Returns (grid uint8, meta) or (None,None).
    Grid values: 0 unknown, 127 seen-but-empty (any point column), 255 occupied."""
    band = pts[(pts[:, 2] >= hmin) & (pts[:, 2] <= hmax)]
    if len(band) == 0:
        return None, None
    ox, oy = pts[:, 0].min(), pts[:, 1].min()
    gx = np.floor((band[:, 0] - ox) / res).astype(np.int64)
    gy = np.floor((band[:, 1] - oy) / res).astype(np.int64)
    W = int(np.ceil((pts[:, 0].max() - ox) / res)) + 1
    H = int(np.ceil((pts[:, 1].max() - oy) / res)) + 1
    W, H = max(1, W), max(1, H)
    counts = np.zeros((H, W), dtype=np.int32)
    np.add.at(counts, (gy, gx), 1)
    grid = np.zeros((H, W), dtype=np.uint8)
    grid[counts >= 1] = 127
    grid[counts >= max(1, min_pts)] = 255
    return grid, {"res": res, "origin": (float(ox), float(oy)), "w": W, "h": H, "band_pts": int(len(band))}


def save_png(grid, path):
    try:
        from PIL import Image
        img = np.full(grid.shape, 40, np.uint8)     # unknown dark
        img[grid == 127] = 150                       # seen-ish
        img[grid == 255] = 245                       # occupied bright
        Image.fromarray(img).save(path)
        return True
    except Exception as e:  # noqa: BLE001
        print("  (png skipped: %s)" % e)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="192.168.11.1")
    ap.add_argument("--out", default=os.path.expanduser("~/localmap"))
    ap.add_argument("--seed-map", default="", help="upload this .vslam first if the device is empty")
    ap.add_argument("--res", type=float, default=0.10, help="2D grid cell size (m)")
    ap.add_argument("--height-min", type=float, default=0.15)
    ap.add_argument("--height-max", type=float, default=2.0)
    ap.add_argument("--min-pts", type=int, default=1, help="landmarks per cell to call it occupied")
    ap.add_argument("--sync-s", type=float, default=45.0, help="max seconds to let map data sync")
    ap.add_argument("--no-download", action="store_true", help="skip the full .vslam download")
    a = ap.parse_args()

    s = sdk.AuroraSDK()
    try:
        s.connect(connection_string=a.conn)
    except Exception as e:  # noqa: BLE001
        print("CONNECT-FAIL:", type(e).__name__, e)
        return 1
    print("connected:", s.is_connected())
    dp, mm = s.data_provider, s.map_manager

    if a.seed_map and not dp.get_all_map_info():
        try:
            print("device empty -> uploading seed map", a.seed_map)
            mm.start_upload_session(a.seed_map)
            while mm.is_session_active():
                time.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            print("seed upload failed:", type(e).__name__, e)

    try:
        s.enable_map_data_syncing(True)
    except Exception:  # noqa: BLE001
        pass
    # wait for the sync to fetch the map: poll until the fetched-point count stops growing
    last, stable, t0 = -1, 0, time.monotonic()
    while time.monotonic() - t0 < a.sync_s:
        try:
            gi = dp.get_global_mapping_info()
            got = int(gi.get("totalMPCountFetched", 0))
        except Exception:  # noqa: BLE001
            got = -1
        if got == last and got > 0:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = got
        time.sleep(1.0)
    print("map-data synced: ~%d points fetched" % max(0, last))

    try:
        d = s.get_map_data()
    except Exception as e:  # noqa: BLE001
        print("get_map_data ERR:", type(e).__name__, e)
        s.disconnect()
        return 1
    mp = [p for p in (xyz(x) for x in d.get("map_points", [])) if p]
    kf = [p for p in (xyz(x) for x in d.get("keyframes", [])) if p]
    if not mp:
        print("NO-POINTS: the device returned an empty map (was it mapping / did it lock?).")
        s.disconnect()
        return 1
    pts = np.array(mp, dtype=np.float64)
    kfp = np.array(kf, dtype=np.float64) if kf else np.empty((0, 3))

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(a.out, "run_" + ts)
    os.makedirs(run_dir, exist_ok=True)
    ply = os.path.join(run_dir, "localmap_3d.ply")
    write_ply(ply, pts, kfp)
    ext = pts.max(0) - pts.min(0)
    print("3D: %d landmarks + %d keyframes -> %s  (extent %.1f x %.1f x %.1f m)"
          % (len(pts), len(kfp), ply, ext[0], ext[1], ext[2]))

    grid, meta = project_2d(pts, a.res, a.height_min, a.height_max, a.min_pts)
    npy = os.path.join(run_dir, "localmap_2d.npy")
    if grid is not None:
        np.save(npy, grid)
        save_png(grid, os.path.join(run_dir, "localmap_2d.png"))
        occ = int((grid == 255).sum())
        print("2D: %dx%d @ %.2fm grid, %d occupied cells, origin (%.2f,%.2f) -> %s"
              % (meta["w"], meta["h"], meta["res"], occ, meta["origin"][0], meta["origin"][1], npy))
    else:
        print("2D: no points in the [%.2f,%.2f] m height band -> no grid" % (a.height_min, a.height_max))

    if not a.no_download:
        try:
            vslam = os.path.join(run_dir, "map_%s.vslam" % ts)
            print("downloading full map -> %s" % vslam)
            mm.start_download_session(vslam)
            while mm.is_session_active():
                time.sleep(1.0)
            print("download-ok")
        except Exception as e:  # noqa: BLE001
            print("download skipped/failed:", type(e).__name__, e)

    # update <out>/latest_* so consumers always find the freshest run
    try:
        import shutil
        for name in ("localmap_3d.ply", "localmap_2d.npy", "localmap_2d.png"):
            src = os.path.join(run_dir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(a.out, "latest_" + name))
        print("updated %s/latest_* " % a.out)
    except Exception as e:  # noqa: BLE001
        print("latest-copy skipped:", e)

    s.disconnect()
    print("OFFLOAD-OK  run_%s added to the local map" % ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
