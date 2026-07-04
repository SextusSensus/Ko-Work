#!/usr/bin/env python3
"""rrd_label.py -- Phase 1 offline environmental labeling on a K1 follow .rrd (laptop, ZERO Orin budget).

Reads a recorded follow .rrd (rgb + depth), runs multi-class COCO YOLO on the frames, fuses each
detection with depth into a 3D position (pinhole, HFOV 70), and writes a NEW scrubbable .rrd:
  /camera/rgb                 the frame
  /camera/rgb/labels          labeled Boxes2D (class + conf) -- "see the chairs/people/furniture"
  /world/objects              labeled Points3D (each object placed in the robot base frame)
  /world/depth_cloud          the depth back-projected to a colored point cloud (the room geometry)
  /world/floor                a coarse RANSAC-free floor estimate (lowest-band plane)
plus obstacles.jsonl (per frame: class, xyz, range, in_forward_corridor) for the capture tier.

Honest scope: single forward POV + flaky head depth, instantaneous egocentric (no map/SLAM). This is
the labeling PIPELINE proof, not thesis data. See OBSTACLE_LABELING_PLAN.md.

Usage: python rrd_label.py <in.rrd> --out <labeled.rrd> [--stride 2] [--conf 0.35] [--hfov 70]
                           [--jsonl obstacles.jsonl] [--classes person,chair,couch,...]
"""
import argparse, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rrd_to_lerobot import read_rrd   # reuse the chunk-stream .rrd reader


def focal_px(w, hfov_deg):
    return (w / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def nearest_earlier(keys_sorted, fi):
    """Largest key <= fi (nearest-earlier), or the first key if none precede."""
    import bisect
    i = bisect.bisect_right(keys_sorted, fi) - 1
    return keys_sorted[max(0, i)] if keys_sorted else None


def sample_box_depth(d, x1, y1, x2, y2, sw, sh):
    """Robust depth (median of valid) in the box, scaled from rgb coords to depth resolution."""
    import numpy as np
    dh, dw = d.shape[:2]
    fx1 = int(max(0, min(dw - 1, x1 * sw))); fx2 = int(max(0, min(dw, x2 * sw)))
    fy1 = int(max(0, min(dh - 1, y1 * sh))); fy2 = int(max(0, min(dh, y2 * sh)))
    if fx2 <= fx1 or fy2 <= fy1:
        return None
    patch = d[fy1:fy2, fx1:fx2]
    valid = patch[(patch > 0.1) & (patch < 12.0) & np.isfinite(patch)]
    if valid.size < 8:
        return None
    return float(np.median(valid))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stride", type=int, default=2, help="label every Nth rgb frame (speed)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--hfov", type=float, default=70.0)
    ap.add_argument("--model", default="yolo11n.pt", help="ultralytics model (auto-downloads)")
    ap.add_argument("--jsonl", default=None, help="also write per-frame obstacle annotations here")
    ap.add_argument("--classes", default=None,
                    help="comma list of COCO names to keep (default: all). e.g. person,chair,couch")
    ap.add_argument("--corridor-deg", type=float, default=30.0,
                    help="+/- bearing that counts as the forward corridor (avoidance-relevant)")
    a = ap.parse_args()

    import numpy as np
    import rerun as rr
    from ultralytics import YOLO

    print("reading %s ..." % a.rrd)
    scalars, images, depth = read_rrd(a.rrd)
    print("  %d rgb frames, %d depth frames" % (len(images), len(depth)))
    if not images:
        raise SystemExit("no rgb frames in the .rrd")

    model = YOLO(a.model)
    names = model.names
    keep = None
    if a.classes:
        want = {c.strip().lower() for c in a.classes.split(",")}
        keep = {i for i, n in names.items() if n.lower() in want}
        print("  keeping classes:", sorted(names[i] for i in keep))

    rr.init("k1_label", spawn=False)
    rr.save(a.out)
    try:
        import rerun.blueprint as rrb
        rr.send_blueprint(rrb.Blueprint(rrb.Horizontal(
            rrb.Spatial2DView(origin="/camera/rgb", name="labeled camera"),
            rrb.Spatial3DView(origin="/world", name="room + objects (base frame)"),
            column_shares=[1, 1]), collapse_panels=True))
    except Exception as e:
        print("blueprint note:", e)

    depth_fis = sorted(depth)
    img_fis = sorted(images)
    jf = open(a.jsonl, "w") if a.jsonl else None
    n_obj = 0
    corridor = math.radians(a.corridor_deg)

    for k, fi in enumerate(img_fis):
        if k % a.stride:
            continue
        rgb = images[fi]                     # HxWx3, RGB (as stored)
        h, w = rgb.shape[:2]
        f = focal_px(w, a.hfov)
        rr.set_time("frame_idx", sequence=int(fi))
        rr.log("/camera/rgb", rr.Image(rgb))

        # multi-class detection (YOLO wants BGR; we stored RGB)
        res = model.predict(rgb[:, :, ::-1], conf=a.conf, verbose=False)[0]

        d = depth.get(nearest_earlier(depth_fis, fi)) if depth_fis else None
        sw = (d.shape[1] / float(w)) if d is not None else 1.0
        sh = (d.shape[0] / float(h)) if d is not None else 1.0

        mins, sizes, labels, colors = [], [], [], []
        pts, plabels, pcolors = [], [], []
        frame_objs = []
        for b in res.boxes:
            cls = int(b.cls[0]); conf = float(b.conf[0])
            if keep is not None and cls not in keep:
                continue
            nm = names[cls]
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            col = _class_color(cls)
            mins.append([x1, y1]); sizes.append([x2 - x1, y2 - y1])
            labels.append("%s %.2f" % (nm, conf)); colors.append(col)
            Z = sample_box_depth(d, x1, y1, x2, y2, sw, sh) if d is not None else None
            rec = {"frame_idx": int(fi), "cls": nm, "conf": round(conf, 3),
                   "box": [round(v, 1) for v in (x1, y1, x2, y2)]}
            if Z is not None:
                cx = (x1 + x2) / 2.0; cy = (y1 + y2) / 2.0
                bearing = math.atan2(cx - w / 2.0, f)
                X = (cx - w / 2.0) * Z / f          # right
                Yup = -(cy - h / 2.0) * Z / f       # up (image y grows down)
                pts.append([X, Z, Yup])             # rerun world: x-right, y-forward(depth), z-up
                plabels.append("%s %.1fm" % (nm, Z)); pcolors.append(col)
                rec.update(range_m=round(Z, 2), bearing_deg=round(math.degrees(bearing), 1),
                           xyz=[round(X, 2), round(Z, 2), round(Yup, 2)],
                           in_corridor=bool(abs(bearing) <= corridor))
            frame_objs.append(rec)
            n_obj += 1

        rr.log("/camera/rgb/labels", rr.Boxes2D(mins=mins, sizes=sizes, labels=labels, colors=colors))
        if pts:
            rr.log("/world/objects", rr.Points3D(pts, labels=plabels, colors=pcolors, radii=0.12))

        # room geometry: back-project a subsampled depth cloud (every 6th pixel) + a floor band
        if d is not None:
            _log_depth_cloud(rr, d, f * (d.shape[1] / float(w)), a.hfov)
        if jf:
            for rec in frame_objs:
                jf.write(json.dumps(rec) + "\n")

    if jf:
        jf.close()
    try:
        rec = rr.get_global_data_recording()
        if rec is not None:
            rec.flush()
    except Exception:
        pass
    print("WROTE labeled .rrd -> %s  (%d object detections logged)" % (a.out, n_obj))
    if a.jsonl:
        print("WROTE annotations -> %s" % a.jsonl)


def _class_color(cls):
    # stable pseudo-color per class id (person=red-ish, furniture varied)
    r = (37 * (cls + 1)) % 256
    g = (91 * (cls + 3)) % 256
    b = (151 * (cls + 7)) % 256
    return [int(r), int(g), int(b)]


def _log_depth_cloud(rr, d, fdepth, hfov):
    """Back-project a subsampled depth map to a colored 3D point cloud (room geometry) + floor band."""
    import numpy as np
    dh, dw = d.shape[:2]
    step = 6
    ys, xs = np.mgrid[0:dh:step, 0:dw:step]
    Z = d[ys, xs].astype(np.float32)
    m = (Z > 0.1) & (Z < 8.0) & np.isfinite(Z)
    if not m.any():
        return
    xs = xs[m].astype(np.float32); ys = ys[m].astype(np.float32); Z = Z[m]
    X = (xs - dw / 2.0) * Z / fdepth
    Yup = -(ys - dh / 2.0) * Z / fdepth
    pts = np.stack([X, Z, Yup], axis=1)
    # color by height (floor low = brown, higher = blue) -> quick geometry read
    hnorm = np.clip((Yup - Yup.min()) / (np.ptp(Yup) + 1e-6), 0, 1)
    col = np.stack([(150 * (1 - hnorm)).astype(np.uint8),
                    (110 + 60 * hnorm).astype(np.uint8),
                    (80 + 175 * hnorm).astype(np.uint8)], axis=1)
    rr.log("/world/depth_cloud", rr.Points3D(pts, colors=col, radii=0.01))
    # coarse floor band = lowest 8% of points
    thr = np.percentile(Yup, 8)
    fm = Yup <= thr
    if fm.sum() > 20:
        rr.log("/world/floor", rr.Points3D(pts[fm], colors=[120, 90, 60], radii=0.02))


if __name__ == "__main__":
    sys.exit(main())
