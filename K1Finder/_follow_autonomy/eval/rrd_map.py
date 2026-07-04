#!/usr/bin/env python3
"""rrd_map.py -- Phase 5 (offline): stitch a follow .rrd's per-frame egocentric depth (+ labeled
obstacles) into a PERSISTENT local map, using the robot's ego-motion (/odometer_state x,y,theta
composed with the camera mount). Turns the instantaneous snapshots into an accumulated world-frame
map + world-frame obstacle dedup -- which also fixes the Phase-2 ego-motion confound.

Pose source (per frame, in the odom frame):
  --poses poses.jsonl   {frame_idx, x, y, theta[, cam_h, cam_pitch_deg]}  (from /odometer_state + mount)
  --synthetic MODE      generate a trajectory to VALIDATE the stitch math without real poses
                        (zero | forward | arc) -- 'zero' should collapse all frames to one clean view.

Frames (ROS REP-103): odom/base X-fwd Y-left Z-up; camera optical Z-fwd X-right Y-down.
Honest limits (see k1-odometry-mapping): 2D planar odom (flat-floor), legged drift, no loop closure
-> local map, not global SLAM.

Usage: python rrd_map.py <in.rrd> --out map.rrd [--poses poses.jsonl | --synthetic forward]
                         [--obstacles obstacles.jsonl] [--hfov 70] [--cam-h 1.35] [--cam-pitch 8]
                         [--voxel 0.05] [--stride 2] [--max-range 6]
"""
import argparse, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rrd_to_lerobot import read_rrd


def focal_px(w, hfov_deg):
    return (w / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def T_world_base(x, y, theta, z):
    import numpy as np
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s, 0, x], [s, c, 0, y], [0, 0, 1, z], [0, 0, 0, 1]], dtype=np.float64)


def T_base_optical(cam_h, pitch_deg):
    """Camera mounted at height cam_h on the base, looking forward, pitched DOWN by pitch_deg.
    optical(Z-fwd,X-right,Y-down) -> base(X-fwd,Y-left,Z-up)."""
    import numpy as np
    # base axes of the optical frame with zero pitch: opt_X=right=-baseY, opt_Y=down=-baseZ, opt_Z=fwd=+baseX
    R0 = np.array([[0, 0, 1], [-1, 0, 0], [0, -1, 0]], dtype=np.float64)  # cols = opt axes in base
    p = math.radians(pitch_deg)
    # pitch tilts the look direction down: rotate about the base Y (left) axis
    cp, sp = math.cos(p), math.sin(p)
    Rp = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=np.float64)
    R = Rp @ R0
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = [0.0, 0.0, cam_h]
    return T


def synth_poses(mode, frames):
    """Fabricate an odom trajectory to validate the stitch math (no real odometry needed)."""
    out = {}
    n = len(frames)
    for i, fi in enumerate(frames):
        t = i / max(1, n - 1)
        if mode == "zero":
            out[fi] = (0.0, 0.0, 0.0)                      # no motion -> map should collapse to one view
        elif mode == "forward":
            out[fi] = (1.5 * t, 0.0, 0.0)                  # walk 1.5m straight
        elif mode == "arc":
            out[fi] = (2.0 * math.sin(t), 1.0 - math.cos(t), 0.9 * t)  # a gentle arc + turn
        else:
            out[fi] = (0.0, 0.0, 0.0)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--poses", default=None)
    ap.add_argument("--synthetic", default=None, choices=["zero", "forward", "arc"])
    ap.add_argument("--obstacles", default=None, help="obstacles.jsonl -> world-frame obstacle dedup")
    ap.add_argument("--hfov", type=float, default=70.0)
    ap.add_argument("--cam-h", type=float, default=1.35, help="camera height above the floor (m)")
    ap.add_argument("--cam-pitch", type=float, default=8.0, help="camera downward pitch (deg)")
    ap.add_argument("--voxel", type=float, default=0.05, help="map voxel size (m) -- bounds growth")
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--max-range", type=float, default=6.0)
    ap.add_argument("--dedup-dist", type=float, default=0.6, help="world-frame obstacle merge radius")
    a = ap.parse_args()

    import numpy as np
    import rerun as rr

    print("reading %s ..." % a.rrd)
    scalars, images, depth = read_rrd(a.rrd)
    print("  %d rgb, %d depth frames" % (len(images), len(depth)))
    if not depth:
        raise SystemExit("no depth in the .rrd -- mapping needs depth")

    depth_fis = sorted(depth)
    if a.poses:
        pj = {}
        for line in open(a.poses):
            r = json.loads(line)
            pj[r["frame_idx"]] = (r["x"], r["y"], r.get("theta", 0.0))
        posef = pj
    elif a.synthetic:
        posef = synth_poses(a.synthetic, depth_fis)
        print("  synthetic poses: %s" % a.synthetic)
    else:
        raise SystemExit("need --poses or --synthetic")

    Tbo = T_base_optical(a.cam_h, a.cam_pitch)

    rr.init("k1_map", spawn=False)
    rr.save(a.out)
    try:
        rr.log("/map", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
    except Exception:
        pass

    voxels = {}                         # (ix,iy,iz) -> color, bounds the accumulated cloud
    traj = []
    used = 0
    for k, fi in enumerate(depth_fis):
        if k % a.stride or fi not in posef:
            continue
        d = depth[fi]
        h, w = d.shape[:2]
        f = focal_px(w, a.hfov)
        x, y, theta = posef[fi]
        Twb = T_world_base(x, y, theta, 0.0)
        Two = Twb @ Tbo                 # camera-optical -> world
        traj.append([x, y, a.cam_h])

        step = 4
        ys, xs = np.mgrid[0:h:step, 0:w:step]
        Z = d[ys, xs].astype(np.float32)
        m = (Z > 0.2) & (Z < a.max_range) & np.isfinite(Z)
        if not m.any():
            continue
        xs = xs[m].astype(np.float32); ys = ys[m].astype(np.float32); Z = Z[m]
        Xo = (xs - w / 2.0) * Z / f      # optical right
        Yo = (ys - h / 2.0) * Z / f      # optical down
        pts_opt = np.stack([Xo, Yo, Z, np.ones_like(Z)], axis=0)   # (4, N)
        pw = (Two @ pts_opt)[:3].T       # (N, 3) world
        # color by height (floor low -> high)
        zc = np.clip((pw[:, 2] - 0.0) / 2.0, 0, 1)
        col = np.stack([(180 * (1 - zc)).astype(np.uint8),
                        (120 + 60 * zc).astype(np.uint8),
                        (90 + 165 * zc).astype(np.uint8)], axis=1)
        for p, c in zip(pw, col):
            key = (int(p[0] / a.voxel), int(p[1] / a.voxel), int(p[2] / a.voxel))
            voxels[key] = c
        used += 1

        # scrubbable growing map: log the accumulated cloud at this frame
        if used % 3 == 0 or k == depth_fis[-1]:
            allpts = np.array([[kx * a.voxel, ky * a.voxel, kz * a.voxel] for (kx, ky, kz) in voxels])
            allcol = np.array(list(voxels.values()))
            rr.set_time("frame_idx", sequence=int(fi))
            rr.log("/map/cloud", rr.Points3D(allpts, colors=allcol, radii=a.voxel * 0.5))
            rr.log("/map/trajectory", rr.LineStrips3D([traj], colors=[[255, 220, 0]]))

    # final static map
    allpts = np.array([[kx * a.voxel, ky * a.voxel, kz * a.voxel] for (kx, ky, kz) in voxels])
    allcol = np.array(list(voxels.values()))
    rr.log("/map/cloud_final", rr.Points3D(allpts, colors=allcol, radii=a.voxel * 0.5), static=True)
    print("  stitched %d frames -> %d map voxels (%.1fm x %.1fm x %.1fm)"
          % (used, len(voxels),
             np.ptp(allpts[:, 0]) if len(allpts) else 0,
             np.ptp(allpts[:, 1]) if len(allpts) else 0,
             np.ptp(allpts[:, 2]) if len(allpts) else 0))

    # world-frame obstacle dedup (fixes the Phase-2 ego-motion confound)
    if a.obstacles:
        obs = [json.loads(l) for l in open(a.obstacles) if l.strip()]
        world = []
        for o in obs:
            fi = o["frame_idx"]
            if fi not in posef or "xyz" not in o:
                continue
            X, Zf, Yup = o["xyz"]        # rrd_label world: x-right, y-fwd, z-up (optical-ish)
            popt = np.array([X, -Yup, Zf, 1.0])   # back to optical (right, down, fwd)
            pw = (T_world_base(*posef[fi], 0.0) @ Tbo @ popt)[:3]
            world.append((o["cls"], pw))
        # greedy merge by class + world distance
        merged = []
        for cls, pw in world:
            hit = None
            for m2 in merged:
                if m2["cls"] == cls and np.linalg.norm(m2["xyz"] - pw) < a.dedup_dist:
                    hit = m2; break
            if hit:
                hit["xyz"] = (hit["xyz"] * hit["n"] + pw) / (hit["n"] + 1); hit["n"] += 1
            else:
                merged.append({"cls": cls, "xyz": pw, "n": 1})
        merged = [m2 for m2 in merged if m2["n"] >= 3]
        if merged:
            rr.log("/map/obstacles",
                   rr.Points3D([m2["xyz"] for m2 in merged],
                               labels=["%s (n=%d)" % (m2["cls"], m2["n"]) for m2 in merged],
                               colors=[[70, 200, 90]] * len(merged), radii=0.2), static=True)
        import collections
        by = collections.Counter(m2["cls"] for m2 in merged)
        print("  world-frame obstacles: %s" % (", ".join("%dx %s" % (v, k) for k, v in by.most_common()) or "none"))

    try:
        rec = rr.get_global_data_recording()
        if rec is not None:
            rec.flush()
    except Exception:
        pass
    print("WROTE map .rrd -> %s" % a.out)


if __name__ == "__main__":
    sys.exit(main())
