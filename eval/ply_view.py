#!/usr/bin/env python3
"""Render an ASCII .ply point cloud to a top-down PNG for the K1 Finder SLAM Map tab.

  python ply_view.py <in.ply> --png out.png [--view top|front|side]

Sparse SLAM clouds carry a few far-flung outlier landmarks that blow up the bounding box, so the
1-99th percentile per axis sets the frame (outliers are still drawn, just don't drive the scale).
Points keep their .ply RGB if present (keyframe camera path shows in its own colour); otherwise a
light dot on a dark ground. Prints a one-line summary the app shows next to the image.
"""
import argparse
import sys

import numpy as np


def load_ply(path):
    xyz, rgb = [], []
    with open(path) as f:
        in_body = False
        has_rgb = False
        for line in f:
            if not in_body:
                s = line.strip()
                if s.startswith("property") and ("red" in s or "green" in s):
                    has_rgb = True
                if s == "end_header":
                    in_body = True
                continue
            p = line.split()
            if len(p) < 3:
                continue
            xyz.append((float(p[0]), float(p[1]), float(p[2])))
            if has_rgb and len(p) >= 6:
                rgb.append((int(p[3]), int(p[4]), int(p[5])))
    a = np.array(xyz, dtype=np.float64)
    c = np.array(rgb, dtype=np.uint8) if rgb else None
    return a, c


AXES = {"top": (0, 1), "front": (0, 2), "side": (1, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ply")
    ap.add_argument("--png", required=True)
    ap.add_argument("--view", choices=list(AXES), default="top")
    ap.add_argument("--size", type=int, default=1000)
    a = ap.parse_args()

    pts, rgb = load_ply(a.ply)
    if len(pts) == 0:
        print("empty cloud: no vertices found")
        return 1

    ext_full = pts.max(0) - pts.min(0)
    print("cloud: %d points, extent %.1f x %.1f x %.1f m" %
          (len(pts), ext_full[0], ext_full[1], ext_full[2]))

    from PIL import Image
    ax0, ax1 = AXES[a.view]
    h_, v_ = pts[:, ax0], pts[:, ax1]
    # frame on the robust core so a few outliers don't shrink everything to a dot
    lo0, hi0 = np.percentile(h_, 1), np.percentile(h_, 99)
    lo1, hi1 = np.percentile(v_, 1), np.percentile(v_, 99)
    span = max(hi0 - lo0, hi1 - lo1, 1e-3)
    W = a.size
    sc = (W - 40) / span
    H = int((hi1 - lo1) * sc) + 40
    img = np.full((H, W, 3), 24, np.uint8)
    px = np.clip(((h_ - lo0) * sc + 20).astype(int), 0, W - 1)
    py = np.clip((H - 20 - (v_ - lo1) * sc).astype(int), 0, H - 1)
    if rgb is None:
        rgb = np.full((len(pts), 3), 215, np.uint8)
    for a_, b_, col in zip(px, py, rgb):
        img[max(0, b_ - 1):b_ + 1, max(0, a_ - 1):a_ + 1] = col
    Image.fromarray(img).save(a.png)
    print("wrote", a.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
