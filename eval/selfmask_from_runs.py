#!/usr/bin/env python3
"""Build the robot's SELF-MASK from the .rrd recordings of PAST RUNS -- no robot time needed.

WHY THIS EXISTS. selfmask.py does the same job live on the robot from ~120 frames of one
controlled capture. That capture is a trip to the robot, it needs a scene that happens to be open,
and a single scene is exactly the weak case for this method: a pixel is only proved to be WORLD by
once seeing something far through it, so a capture facing a near wall silently marks the wall as
"robot". The run archive fixes that by brute force -- thousands of frames across many sessions,
rooms, headings and lighting. Every extra run makes the separation strictly better, because a body
pixel must be near in EVERY frame ever recorded while a world pixel only has to be far ONCE.

THE METHOD (identical in spirit to selfmask.py, just far more evidence):
    self_mask = (max_depth_over_every_recorded_frame < --near-m)
No-return pixels (0 / NaN) are +inf, not near -- absence of a return is not evidence of nearness,
and treating it as such would mask the whole frame.

WHY IT MATTERS. Field-measured 2026-09-04: the robot's own left shoulder/arm returns 0.22-0.35 m,
the same distances a real obstacle occupies, and it pinned vx to 0.00 for an entire session with
the operator 4.39 m away. --obstacle-self-range-m cannot fix that: 0.22 lets the arm through, and
0.35 was measured blinding the robot to genuine obstacles at 0.25-0.30 m. Range does not separate
the robot from the world. POSITION does, and that is what this produces.

ONLY MIX RECORDINGS FROM THE SAME PHYSICAL SETUP. The mask is pixel-indexed, so a camera that was
re-seated, re-aimed or swapped invalidates every earlier run. --since is the guard; default is the
current setup. Frames whose resolution differs from the first are skipped and counted, never
silently resized.

Usage:
  python selfmask_from_runs.py --runs ../runs --since 20260903 --out ../models/self_mask.npy
  python selfmask_from_runs.py --runs ../runs --since 20260903 --out /tmp/m.npy --report
"""
import argparse
import glob
import math
import os
import sys

import numpy as np

# rerun's Recording API. Kept local so the module imports on a box without rerun for --help.
DTYPES = {1: np.uint8, 2: np.uint16, 3: np.uint32, 7: np.float16, 8: np.float32, 9: np.float64}


def depth_frames(path):
    """Yield (h, w) float32 depth-in-metres arrays from one .rrd. Never raises on a bad chunk."""
    import warnings
    warnings.filterwarnings("ignore")
    from rerun.recording import load_recording
    try:
        rec = load_recording(path)
    except Exception as e:  # noqa: BLE001 -- one unreadable recording must not stop the sweep
        print("  ! unreadable (%s)" % e, file=sys.stderr)
        return
    for ch in rec.chunks():
        try:
            if "depth" not in str(ch.entity_path).lower():
                continue
            rb = ch.to_record_batch()
            if "DepthImage:buffer" not in rb.schema.names:
                continue
            buf = rb.column("DepthImage:buffer")
            fmt = rb.column("DepthImage:format")
            has_m = "DepthImage:meter" in rb.schema.names
            mtr = rb.column("DepthImage:meter") if has_m else None
            for i in range(rb.num_rows):
                fl = fmt[i].as_py()
                raw = buf[i].as_py()
                if not fl or not raw:
                    continue
                f = fl[0]
                dt = DTYPES.get(f["channel_datatype"], np.float32)
                a = np.frombuffer(bytes(raw[0]), dtype=np.uint8).view(dt).astype(np.float32)
                need = f["height"] * f["width"]
                if a.size < need:
                    continue
                a = a[:need].reshape(f["height"], f["width"])
                if dt is not np.float32:                    # integer encodings are scaled units
                    m = (mtr[i].as_py() or [1000.0]) if mtr is not None else [1000.0]
                    a = a / (m[0] if m and m[0] else 1000.0)
                yield a
        except Exception:  # noqa: BLE001
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs", help="directory of <timestamp>_<commit>/ run dirs")
    ap.add_argument("--since", default="20260903",
                    help="only runs whose dir name sorts >= this. GUARDS THE PIXEL INDEX: raise it "
                         "after any camera re-seat/re-aim, or the mask mixes incompatible geometry")
    ap.add_argument("--out", default="models/self_mask.npy")
    ap.add_argument("--near-m", type=float, default=0.45,
                    help="a pixel whose depth NEVER exceeds this across every frame is the robot. "
                         "Must sit above the body (measured 0.22-0.35 m) and below the nearest "
                         "range a real obstacle needs to be seen at")
    ap.add_argument("--min-frames", type=int, default=200,
                    help="refuse to write a mask built on less evidence than this")
    ap.add_argument("--report", action="store_true", help="print mask geometry, write nothing")
    a = ap.parse_args()

    dirs = sorted(d for d in glob.glob(os.path.join(a.runs, "*"))
                  if os.path.isdir(d) and os.path.basename(d) >= a.since)
    if not dirs:
        print("no run dirs >= %s under %s" % (a.since, a.runs))
        return 2

    mx = None
    n_frames = n_files = n_skipped = 0
    for d in dirs:
        for rrd in sorted(glob.glob(os.path.join(d, "*.rrd"))):
            n_files += 1
            print("[%2d] %s" % (n_files, os.path.relpath(rrd, a.runs)))
            got = 0
            for f in depth_frames(rrd):
                if mx is None:
                    mx = np.full(f.shape, -np.inf, dtype=np.float32)
                if f.shape != mx.shape:
                    n_skipped += 1                          # never resize: pixel index is the point
                    continue
                # A no-return is NOT evidence of nearness. +inf so it can never pull a pixel below
                # the threshold; a pixel that is always no-return stays +inf and is not masked.
                z = np.where(np.isfinite(f) & (f > 0.05), f, np.inf)
                np.maximum(mx, z, out=mx)
                got += 1
            n_frames += got
            print("     %d depth frames" % got)

    if mx is None or n_frames < a.min_frames:
        print("REFUSING: %d frames is below --min-frames %d -- too little evidence for a mask "
              "that silently blinds pixels" % (n_frames, a.min_frames))
        return 1

    mask = (mx < a.near_m)
    h, w = mask.shape
    frac = 100.0 * mask.sum() / mask.size
    print("\n%d frames from %d recordings (%d skipped on resolution)" % (n_frames, n_files, n_skipped))
    print("SELF-MASK %dx%d: %d px (%.2f%%) are never farther than %.2f m" %
          (w, h, int(mask.sum()), frac, a.near_m))

    if mask.any():
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        # Row 0 is the TOP of the image. Say which edge in words -- an earlier version of this
        # diagnostic printed a fraction that could not distinguish top from bottom and misread
        # a top-edge blob as "the bottom 100%".
        print("  rows %d..%d of %d  (row 0 = TOP)   cols %d..%d of %d" %
              (rows.min(), rows.max(), h, cols.min(), cols.max(), w))
        from scipy import ndimage
        lab, n = ndimage.label(mask)
        if n:
            sz = np.bincount(lab.ravel()); sz[0] = 0
            order = np.argsort(sz)[::-1][:4]
            print("  %d connected regions; largest:" % n)
            for li in order:
                if sz[li] == 0:
                    continue
                ys, xs = np.where(lab == li)
                print("    %6d px  rows %3d..%3d  cols %3d..%3d  median max-depth %.2f m" %
                      (sz[li], ys.min(), ys.max(), xs.min(), xs.max(),
                       float(np.median(mx[lab == li]))))
    # Sanity: a mask covering a large share of the frame is a bad capture, not a big robot.
    if frac > 25.0:
        print("\nWARNING: %.1f%% of the frame is masked. That is far more than a robot's own body "
              "and usually means the recordings never saw anything far through those pixels "
              "(a near wall throughout). Add runs from more open scenes before trusting this." % frac)

    if a.report:
        print("\n--report: nothing written")
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    np.save(a.out, mask)
    print("\nwrote %s" % a.out)
    print("use it with:  --self-mask %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
