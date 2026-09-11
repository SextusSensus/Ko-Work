#!/usr/bin/env python3
"""Estimate the head camera's PITCH and HEIGHT from recorded runs, by fitting the ground.

WHY. _corridor_clearance computes a pixel's height as `camera_height - z*(v-cy)/f`, which assumes
the optical axis is LEVEL. On 2026-09-04 that assumption was measured wrong by ~10 deg, and the
error it produces is `z*sin(pitch)` -- it GROWS WITH RANGE, so the far floor computes higher than
the near floor and the whole floor plane climbs above --floor-margin-m into the hit box. Field
result: clearances of 0.43-0.75 m on an empty floor, vx capped to zero, the robot refusing to walk
across an open room. Raising --obstacle-self-range-m removed the ARM false positive and left this
one standing alone at 0.49-0.67 m.

WHAT THIS ANSWERS. Whether that pitch is a FIXED MOUNTING OFFSET or something dynamic (a head
pitch command, or lean from the gait). It decides the fix:
  tight distribution across runs -> a constant correction, no new sensor input
  broad distribution            -> must read the live pitch per frame, and no recording carries
                                   the head pose before cfdcb14, so it needs a fresh run

THE METHOD is mode-seeking, not least squares. For a candidate pitch b, every point that really is
on the ground shares the same implied camera height c = Y - b*Z, so a histogram of c SPIKES at the
right pitch and smears at the wrong one. Plain least squares was tried first and fails here: the
lower image half is NOT floor-dominated in these scenes (walls, furniture, people), so trimming
from a corrupted initial fit converges onto whatever else is large -- measured inlier fractions of
0.10-0.16 and implied camera heights of 1.69 m against a true 0.86 m.

ONLY HIGH-SUPPORT FRAMES COUNT. Where the floor is a small part of the view the fit is noise
(pitch SD 10.9 deg, height spread 0.31-1.13 m). --min-support gates that out; the surviving frames
agreed to within a couple of degrees and put the camera at 0.93 m, which independently matched a
RANSAC fit done by a different method on the same recording.

Usage:
  python ground_fit_from_runs.py --runs ../runs --since 20260903
  python ground_fit_from_runs.py --runs ../runs --since 20260903 --min-support 0.15 --per-run
"""
import argparse
import glob
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def depth_frames(path):
    """Yield (h,w) float32 metre depth arrays from a .rrd. Shared shape with selfmask_from_runs."""
    import warnings
    warnings.filterwarnings("ignore")
    from rerun.recording import load_recording
    dt_map = {1: np.uint8, 2: np.uint16, 3: np.uint32, 7: np.float16, 8: np.float32, 9: np.float64}
    try:
        rec = load_recording(path)
    except Exception as e:  # noqa: BLE001
        print("  ! unreadable (%s)" % e, file=sys.stderr)
        return
    for ch in rec.chunks():
        try:
            if "depth" not in str(ch.entity_path).lower():
                continue
            rb = ch.to_record_batch()
            if "DepthImage:buffer" not in rb.schema.names:
                continue
            buf, fmt = rb.column("DepthImage:buffer"), rb.column("DepthImage:format")
            mtr = rb.column("DepthImage:meter") if "DepthImage:meter" in rb.schema.names else None
            for i in range(rb.num_rows):
                fl, raw = fmt[i].as_py(), buf[i].as_py()
                if not fl or not raw:
                    continue
                f = fl[0]
                dt = dt_map.get(f["channel_datatype"], np.float32)
                a = np.frombuffer(bytes(raw[0]), dtype=np.uint8).view(dt).astype(np.float32)
                need = f["height"] * f["width"]
                if a.size < need:
                    continue
                a = a[:need].reshape(f["height"], f["width"])
                if dt is not np.float32:
                    m = (mtr[i].as_py() or [1000.0]) if mtr is not None else [1000.0]
                    a = a / (m[0] if m and m[0] else 1000.0)
                yield a
        except Exception:  # noqa: BLE001
            continue


def ground(d, hfov, pitches, tans, bins, step, zmin, zmax, min_support):
    """Mode-seeking ground fit -> (pitch_deg, camera_height_m, support) or None."""
    h, w = d.shape
    f = (w / 2.0) / math.tan(math.radians(hfov) / 2.0)
    ys = np.arange(h // 2, h, step)          # lower half: where the floor can be
    xs = np.arange(0, w, step)
    sub = d[np.ix_(ys, xs)]
    v = (ys - h * 0.5) / f
    ok = np.isfinite(sub) & (sub > zmin) & (sub < zmax)
    if ok.sum() < 120:
        return None
    Z = sub[ok]
    V = np.broadcast_to(v[:, None], sub.shape)[ok]
    Y = Z * V
    C = Y[None, :] - tans[:, None] * Z[None, :]      # implied camera height per (pitch, point)
    best_n, best_i, best_c = -1, None, None
    for i in range(C.shape[0]):
        cnt, edges = np.histogram(C[i], bins=bins)
        j = int(cnt.argmax())
        if cnt[j] > best_n:
            best_n, best_i, best_c = int(cnt[j]), i, 0.5 * (edges[j] + edges[j + 1])
    sup = best_n / float(Z.size)
    if sup < min_support:
        return None
    # PLAUSIBILITY GATE ON THE RESULT, not just on plane support. Support alone says "many points
    # lie on SOME plane", which a wall or a table satisfies perfectly. The physical check is the
    # implied camera height: this camera is ~0.86-0.93 m off the floor and cannot be anywhere else.
    # Without this the tool accepted a fit implying 0.41 m, and that single impossible frame was
    # enough to swing an SD-based "the pitch varies" verdict off n=4. A wrong verdict here sends
    # the floor fix down the wrong road entirely, so the gate belongs in the tool, not the reader.
    if not (0.70 <= best_c <= 1.10):
        return None
    return math.degrees(pitches[best_i]), float(best_c), sup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--since", default="20260903")
    ap.add_argument("--hfov-deg", type=float, default=105.8)
    ap.add_argument("--min-support", type=float, default=0.15,
                    help="fraction of sampled points on the winning plane. Below this the fit is "
                         "noise: measured pitch SD 10.9 deg at 0.05, versus agreement within a "
                         "couple of degrees at 0.15")
    ap.add_argument("--step", type=int, default=6, help="pixel subsample stride")
    ap.add_argument("--per-run", action="store_true", help="also print each run's own estimate")
    ap.add_argument("--max-runs", type=int, default=0, help="0 = all")
    a = ap.parse_args()

    pitches = np.radians(np.arange(-25.0, 36.0, 0.5))
    tans = np.tan(pitches)
    bins = np.arange(0.30, 1.70, 0.02)

    dirs = sorted(d for d in glob.glob(os.path.join(a.runs, "*"))
                  if os.path.isdir(d) and os.path.basename(d) >= a.since)
    if a.max_runs:
        dirs = dirs[-a.max_runs:]
    allp, allc, per_run = [], [], []
    for d in dirs:
        for rrd in sorted(glob.glob(os.path.join(d, "*.rrd"))):
            got = []
            for fr in depth_frames(rrd):
                r = ground(fr, a.hfov_deg, pitches, tans, bins, a.step, 0.35, 4.5, a.min_support)
                if r:
                    got.append(r)
            if got:
                p = np.array([g[0] for g in got])
                c = np.array([g[1] for g in got])
                allp.append(p)
                allc.append(c)
                per_run.append((os.path.basename(d), len(got), float(np.median(p)), float(np.median(c))))
                if a.per_run:
                    print("%-30s n=%-4d pitch %+6.1f deg   height %.2f m"
                          % (os.path.basename(d), len(got), np.median(p), np.median(c)))
            else:
                if a.per_run:
                    print("%-30s no confident fit" % os.path.basename(d))

    if not allp:
        print("\nNO CONFIDENT FIT ANYWHERE. The floor is too small a share of these views to "
              "measure this way; the pitch has to come from the live head pose instead.")
        return 1
    P = np.concatenate(allp)
    C = np.concatenate(allc)
    print("\n%d confident frames across %d recordings" % (P.size, len(per_run)))
    print("PITCH  deg : p5=%+.1f  p50=%+.1f  p95=%+.1f   SD=%.2f" % (*np.percentile(P, [5, 50, 95]), P.std()))
    print("HEIGHT m   : p5=%.2f  p50=%.2f  p95=%.2f   SD=%.3f  (config assumes 0.86)"
          % (*np.percentile(C, [5, 50, 95]), C.std()))
    if len(per_run) > 1:
        rp = np.array([r[2] for r in per_run])
        print("BETWEEN-RUN pitch medians: min %+.1f  max %+.1f  SD %.2f" % (rp.min(), rp.max(), rp.std()))
    # The verdict is the whole point of running this.
    print("\nVERDICT:")
    if P.std() <= 3.0 and (len(per_run) < 2 or np.array([r[2] for r in per_run]).std() <= 3.0):
        print("  Pitch is STABLE (SD %.2f deg) -> a FIXED mounting offset. It can be corrected with"
              " a constant, and no live head pose is needed." % P.std())
        print("  Suggested: --camera-pitch-deg %+.1f  (and camera_height_m %.2f, config has 0.86)"
              % (np.median(P), np.median(C)))
    else:
        print("  Pitch VARIES (SD %.2f deg) -> not a fixed offset. A constant correction would be"
              " wrong most of the time; the live per-frame pitch is required." % P.std())
        print("  No recording before cfdcb14 carries the head pose, so this needs a fresh run with"
              " --rerun surviving (pin the Orin clocks first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
