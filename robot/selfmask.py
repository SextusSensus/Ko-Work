#!/usr/bin/env python3
"""Calibrate the robot's SELF-MASK: which depth pixels are the robot's own body.

WHY BY POSITION, NOT DISTANCE. The head camera sits at 0.86 m with a 94.9 deg vertical FOV, so
its lower rows look down at the robot's own chest and arms. A range cut cannot remove them: the
body spans 0.15-0.28 m in the field data, the same distances a real obstacle occupies, so every
threshold either lets the body through or blinds the robot to genuine obstacles (0.35 was measured
doing exactly that). What DOES separate them is position -- the body is always in the same pixels.

METHOD. Collect N depth frames and take the per-pixel MAXIMUM. A world pixel varies as the robot
turns and people move, and will exceed the threshold at least once; a body pixel never does. So
    self_mask = (max_depth_over_time < --near-m)
is robust without needing a controlled empty scene, as long as the view changes a little during
capture. Frames with no return (NaN/0) do not count as evidence of nearness.

Run it with the robot standing, ideally with open space ahead, and move a little or let the head
scan. Writes a boolean .npy the follow node loads with --self-mask.

Usage:  python selfmask.py --out /home/booster/self_mask.npy [--frames 120] [--near-m 0.45]
"""
import argparse, sys, time
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


def decode(msg):
    a = np.frombuffer(msg.data, dtype=np.uint16 if "16" in msg.encoding else np.float32)
    a = a.reshape(msg.height, msg.width).astype(np.float32)
    return a / 1000.0 if "16" in msg.encoding else a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="/boostercamera/head/depth")
    ap.add_argument("--out", default="/home/booster/self_mask.npy")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--near-m", type=float, default=0.45,
                    help="a pixel whose depth NEVER exceeds this is the robot")
    ap.add_argument("--timeout-s", type=float, default=60.0)
    a = ap.parse_args()

    rclpy.init()
    n = Node("self_mask_cal")
    acc = {"mx": None, "seen": 0}

    def cb(msg):
        try:
            d = decode(msg)
        except Exception:
            return
        d = np.where(np.isfinite(d) & (d > 0.05), d, np.inf)   # no-return != near
        acc["mx"] = d if acc["mx"] is None else np.maximum(acc["mx"], d)
        acc["seen"] += 1

    n.create_subscription(Image, a.topic, cb, qos_profile_sensor_data)
    t0 = time.time()
    while acc["seen"] < a.frames and (time.time() - t0) < a.timeout_s:
        rclpy.spin_once(n, timeout_sec=0.2)
    rclpy.shutdown()

    if acc["mx"] is None or acc["seen"] < 10:
        print("FAIL: only %d depth frames on %s" % (acc["seen"], a.topic)); return 1
    mx = acc["mx"]
    mask = np.isfinite(mx) & (mx < a.near_m)      # never got far => the robot
    h, w = mask.shape
    print("frames %d   mask %d px (%.1f%% of frame)" % (acc["seen"], mask.sum(), 100.0*mask.sum()/mask.size))
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size:
        print("  rows %d..%d of %d   cols %d..%d of %d" % (rows.min(), rows.max(), h, cols.min(), cols.max(), w))
        print("  -> the body occupies the BOTTOM %.0f%% of the frame" % (100.0*(h-rows.min())/h))
    if mask.sum() > 0.5 * mask.size:
        print("REFUSING to write: %.0f%% of the frame looks like 'self', which means the view was "
              "blocked or --near-m is too large. Re-run with open space ahead."
              % (100.0*mask.sum()/mask.size))
        return 2
    np.save(a.out, mask)
    print("SELF-MASK-OK wrote %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
