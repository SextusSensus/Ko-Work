#!/usr/bin/env python3
"""rrd_poses.py -- extract per-frame planar odometry from a capture .rrd into the poses.jsonl that
eval/rrd_map.py consumes to stitch a run into the world frame.

Reads the /odom/{x,y,theta} scalars the follow node logs under --rerun when --odom-topic /odometer_state
is set (P6.1a/P8). Because odom is logged on the SAME frame_idx timeline as the RGB/depth, the poses are
already frame-aligned -- no wall-time join, no guessing.

  python eval/rrd_poses.py <capture.rrd> --out poses.jsonl [--cam-h 1.35 --cam-pitch 8]

Each output line: {"frame_idx": N, "x": .., "y": .., "theta": .., "cam_h": .., "cam_pitch_deg": ..}
cam_h/cam_pitch are the camera extrinsics rrd_map.py needs (T_base_optical); they are NOT recorded in
the .rrd yet (constant per robot), so they come from CLI defaults here -- override per rig.

Exit 2 (LOUD) if the .rrd carries no /odom -- that run was captured WITHOUT odometry; re-capture with the
updated app (the Rerun checkbox now adds --odom-topic) or run_follow_capture.sh (--profile capture).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rrd_to_lerobot import read_rrd   # reuse the chunk-stream .rrd reader (exposes all scalar entities)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cam-h", type=float, default=1.35, help="camera mount height (m) for T_base_optical")
    ap.add_argument("--cam-pitch", type=float, default=8.0, help="camera downward pitch (deg)")
    a = ap.parse_args(argv)

    scalars, _images, _depth = read_rrd(a.rrd)
    ox = scalars.get("/odom/x", {}); oy = scalars.get("/odom/y", {}); ot = scalars.get("/odom/theta", {})
    if not ox:
        print("NO /odom/* in %s -- this run was captured WITHOUT odometry.\n"
              "  Re-capture with the updated app (the Rerun checkbox now adds --odom-topic /odometer_state)\n"
              "  or run_follow_capture.sh (--profile capture). Odom is recording-only; the follow is "
              "byte-identical." % os.path.basename(a.rrd))
        return 2

    frames = sorted(set(ox) & set(oy) & set(ot))
    if not frames:
        print("/odom present but no frame_idx overlaps across x/y/theta -- nothing to export."); return 2
    with open(a.out, "w") as f:
        for fi in frames:
            f.write(json.dumps({"frame_idx": int(fi), "x": ox[fi], "y": oy[fi], "theta": ot[fi],
                                "cam_h": a.cam_h, "cam_pitch_deg": a.cam_pitch}) + "\n")
    dx = max(ox.values()) - min(ox.values()); dy = max(oy.values()) - min(oy.values())
    print("POSES-OK %d frames -> %s  (odom span dx=%.2f m dy=%.2f m)" % (len(frames), a.out, dx, dy))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
