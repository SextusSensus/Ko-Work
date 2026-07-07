#!/usr/bin/env python3
"""rrd_label.py -- Phase 1 offline environmental labeling on a K1 follow .rrd (laptop, ZERO Orin budget).

Reads a recorded follow .rrd (rgb + depth) and produces a NEW scrubbable labeled .rrd:
  /camera/rgb                 the frame
  /camera/rgb/labels          multi-class COCO YOLO Boxes2D (class + conf [+ track id])
  /camera/seg                 (--seg) SegFormer-ADE20K structural segmentation (wall/floor/ceiling/door)
  /world/objects              per-frame labeled Points3D (each object in the robot base frame)
  /world/obstacle_set         (--track) the DEDUPED unique obstacle set (one chair = one point)
  /world/depth_cloud          the depth back-projected to a colored point cloud (room geometry)
  /world/wall,/world/floor    (--seg) the semantic wall/floor pixels back-projected to 3D
plus obstacles.jsonl (per detection: class [track id], xyz, range, in_forward_corridor).

Object track = COCO YOLO (chairs/people/furniture) fused with depth. Structural track = SegFormer
(the only way to LABEL a wall vs. just a geometric plane). Tracking = ByteTrack dedup into a stable
obstacle set. Honest scope: single forward POV + flaky head depth, instantaneous egocentric (no
map/SLAM). Labeling PIPELINE proof, not thesis data. See OBSTACLE_LABELING_PLAN.md.

Usage:
  python rrd_label.py <in.rrd> --out <labeled.rrd> [--track] [--seg] [--stride 2] [--seg-every 12]
                      [--conf 0.30] [--jsonl obstacles.jsonl] [--classes person,chair,couch,...]
"""
import argparse, collections, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rrd_to_lerobot import read_rrd   # reuse the chunk-stream .rrd reader

# ADE20K structural classes we care about for "environment" labeling (SegFormer id2label names).
STRUCT_NAMES = {"wall", "floor", "ceiling", "door", "windowpane", "stairs", "stairway",
                "railing", "column", "fence", "escalator"}


def focal_px(w, hfov_deg):
    return (w / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def nearest_earlier(keys_sorted, fi):
    import bisect
    i = bisect.bisect_right(keys_sorted, fi) - 1
    return keys_sorted[max(0, i)] if keys_sorted else None


def sample_box_depth(d, x1, y1, x2, y2, sw, sh):
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


def _class_color(cls):
    return [(37 * (cls + 1)) % 256, (91 * (cls + 3)) % 256, (151 * (cls + 7)) % 256]


class Segmenter:
    """SegFormer-B0 finetuned on ADE20K (150 classes incl. wall/floor/ceiling/door). CPU-OK, offline."""
    def __init__(self, model_id="nvidia/segformer-b0-finetuned-ade-512-512"):
        from transformers import SegformerImageProcessor, SegformerForSemanticSegmentation
        import torch
        self.torch = torch
        self.proc = SegformerImageProcessor.from_pretrained(model_id)
        self.model = SegformerForSemanticSegmentation.from_pretrained(model_id).eval()
        self.id2label = {int(i): n for i, n in self.model.config.id2label.items()}
        self.struct_ids = {i: n for i, n in self.id2label.items() if n.lower() in STRUCT_NAMES}

    def annotation_context(self, rr):
        # id -> (label, color): structural classes vivid, the rest a dim grey so structure pops.
        ade = []
        for i, n in self.id2label.items():
            if i in self.struct_ids:
                ade.append((i, n, _class_color(i * 7 + 3)))
            else:
                ade.append((i, n, [70, 70, 70]))
        return rr.AnnotationContext(ade)

    def seg(self, rgb):
        import numpy as np
        import torch.nn.functional as F
        inp = self.proc(images=rgb, return_tensors="pt")
        with self.torch.no_grad():
            logits = self.model(**inp).logits            # (1,150,h/4,w/4)
        up = F.interpolate(logits, size=rgb.shape[:2], mode="bilinear", align_corners=False)
        return up.argmax(1)[0].cpu().numpy().astype(np.uint16)


def _log_depth_cloud(rr, d, fdepth):
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
    hnorm = np.clip((Yup - Yup.min()) / (np.ptp(Yup) + 1e-6), 0, 1)
    col = np.stack([(150 * (1 - hnorm)).astype(np.uint8),
                    (110 + 60 * hnorm).astype(np.uint8),
                    (80 + 175 * hnorm).astype(np.uint8)], axis=1)
    rr.log("/world/depth_cloud", rr.Points3D(pts, colors=col, radii=0.01))


def _project_mask(rr, entity, mask_rgbres, d, w, h, fdepth, color):
    """Back-project a boolean mask (in rgb resolution) to 3D via depth -> semantic geometry points."""
    import numpy as np
    dh, dw = d.shape[:2]
    # resize mask to depth resolution (nearest) without cv2
    ys = (np.arange(dh) * (h / float(dh))).astype(int).clip(0, h - 1)
    xs = (np.arange(dw) * (w / float(dw))).astype(int).clip(0, w - 1)
    mdep = mask_rgbres[np.ix_(ys, xs)]
    Z = d.astype(np.float32)
    sel = mdep & (Z > 0.1) & (Z < 8.0) & np.isfinite(Z)
    if sel.sum() < 20:
        return
    yy, xx = np.nonzero(sel)
    step = max(1, len(xx) // 4000)          # cap points
    xx = xx[::step]; yy = yy[::step]; Zs = Z[yy, xx]
    X = (xx - dw / 2.0) * Zs / fdepth
    Yup = -(yy - dh / 2.0) * Zs / fdepth
    rr.log(entity, rr.Points3D(np.stack([X, Zs, Yup], axis=1), colors=color, radii=0.015))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stride", type=int, default=2, help="label every Nth rgb frame")
    ap.add_argument("--conf", type=float, default=0.30)
    ap.add_argument("--hfov", type=float, default=70.0)
    ap.add_argument("--model", default="yolo11n.pt")
    ap.add_argument("--open-vocab", default=None,
                    help="OPEN-VOCAB detection: a comma list of arbitrary text prompts (e.g. "
                         "'pallet,cardboard box,shopping cart,forklift,person') -> YOLO-World detects "
                         "them with NO retraining. Beats the fixed COCO-80 for the domain long tail.")
    ap.add_argument("--model-world", default="yolov8s-worldv2.pt",
                    help="YOLO-World checkpoint for --open-vocab (auto-downloads)")
    ap.add_argument("--track", action="store_true",
                    help="ByteTrack the detections and dedup into a stable obstacle set (one chair = one)")
    ap.add_argument("--seg", action="store_true",
                    help="also run SegFormer-ADE20K structural segmentation (walls/floor/ceiling/doors)")
    ap.add_argument("--seg-every", type=int, default=12, help="run segmentation every Nth labeled frame")
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--classes", default=None)
    ap.add_argument("--corridor-deg", type=float, default=30.0)
    a = ap.parse_args()

    import numpy as np
    import rerun as rr

    print("reading %s ..." % a.rrd)
    scalars, images, depth = read_rrd(a.rrd)
    print("  %d rgb frames, %d depth frames" % (len(images), len(depth)))
    if not images:
        raise SystemExit("no rgb frames in the .rrd")

    keep = None
    if a.open_vocab:
        from ultralytics import YOLOWorld
        model = YOLOWorld(a.model_world)
        prompts = [c.strip() for c in a.open_vocab.split(",") if c.strip()]
        model.set_classes(prompts)     # open-vocab: detect these text prompts, no retraining
        print("  OPEN-VOCAB (%d prompts): %s" % (len(prompts), ", ".join(prompts)))
    else:
        from ultralytics import YOLO
        model = YOLO(a.model)
    names = model.names
    if a.classes and not a.open_vocab:
        want = {c.strip().lower() for c in a.classes.split(",")}
        keep = {i for i, n in names.items() if n.lower() in want}
    segmenter = Segmenter() if a.seg else None

    rr.init("k1_label", spawn=False)
    rr.save(a.out)
    try:
        rr.log("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)   # +X right, +Y fwd, +Z up
    except Exception:
        pass
    if segmenter is not None:
        try:
            rr.log("/camera/seg", segmenter.annotation_context(rr), static=True)
        except Exception as e:
            print("seg annotation note:", e)
    try:
        import rerun.blueprint as rrb
        views = [rrb.Spatial2DView(origin="/camera/rgb", name="labeled camera"),
                 rrb.Spatial3DView(origin="/world", name="room + objects (base frame)")]
        if segmenter is not None:
            views.insert(1, rrb.Spatial2DView(origin="/camera/seg", name="structural seg"))
        rr.send_blueprint(rrb.Blueprint(rrb.Horizontal(*views), collapse_panels=True))
    except Exception as e:
        print("blueprint note:", e)

    depth_fis = sorted(depth)
    img_fis = sorted(images)
    jf = open(a.jsonl, "w") if a.jsonl else None
    n_obj = 0
    corridor = math.radians(a.corridor_deg)
    tracks = collections.defaultdict(lambda: {"cls": collections.Counter(), "pos": [],
                                              "rng": [], "frames": [], "corr": 0})

    labeled_frames = [fi for k, fi in enumerate(img_fis) if k % a.stride == 0]
    for si, fi in enumerate(labeled_frames):
        rgb = images[fi]
        h, w = rgb.shape[:2]
        f = focal_px(w, a.hfov)
        rr.set_time("frame_idx", sequence=int(fi))
        rr.log("/camera/rgb", rr.Image(rgb))
        bgr = rgb[:, :, ::-1]
        res = (model.track(bgr, persist=True, conf=a.conf, verbose=False, tracker="bytetrack.yaml")[0]
               if a.track else model.predict(bgr, conf=a.conf, verbose=False)[0])

        d = depth.get(nearest_earlier(depth_fis, fi)) if depth_fis else None
        sw = (d.shape[1] / float(w)) if d is not None else 1.0
        sh = (d.shape[0] / float(h)) if d is not None else 1.0

        mins, sizes, labels, colors = [], [], [], []
        pts, plabels, pcolors = [], [], []
        for b in res.boxes:
            cls = int(b.cls[0]); conf = float(b.conf[0])
            if keep is not None and cls not in keep:
                continue
            nm = names[cls]
            tid = int(b.id[0]) if (a.track and b.id is not None) else None
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            col = _class_color(cls)
            tag = ("%s#%d %.2f" % (nm, tid, conf)) if tid is not None else ("%s %.2f" % (nm, conf))
            mins.append([x1, y1]); sizes.append([x2 - x1, y2 - y1]); labels.append(tag); colors.append(col)
            Z = sample_box_depth(d, x1, y1, x2, y2, sw, sh) if d is not None else None
            rec = {"frame_idx": int(fi), "cls": nm, "conf": round(conf, 3),
                   "track_id": tid, "box": [round(v, 1) for v in (x1, y1, x2, y2)]}
            if Z is not None:
                cx = (x1 + x2) / 2.0; cy = (y1 + y2) / 2.0
                bearing = math.atan2(cx - w / 2.0, f)
                X = (cx - w / 2.0) * Z / f; Yup = -(cy - h / 2.0) * Z / f
                pts.append([X, Z, Yup]); plabels.append(tag.split(" ")[0]); pcolors.append(col)
                rec.update(range_m=round(Z, 2), bearing_deg=round(math.degrees(bearing), 1),
                           xyz=[round(X, 2), round(Z, 2), round(Yup, 2)],
                           in_corridor=bool(abs(bearing) <= corridor))
                if tid is not None:
                    t = tracks[tid]
                    t["cls"][nm] += 1; t["pos"].append([X, Z, Yup]); t["rng"].append(Z)
                    t["frames"].append(int(fi)); t["corr"] += int(abs(bearing) <= corridor)
            n_obj += 1
            if jf:
                jf.write(json.dumps(rec) + "\n")

        rr.log("/camera/rgb/labels", rr.Boxes2D(mins=mins, sizes=sizes, labels=labels, colors=colors))
        if pts:
            rr.log("/world/objects", rr.Points3D(pts, labels=plabels, colors=pcolors, radii=0.12))
        if d is not None:
            _log_depth_cloud(rr, d, f * (d.shape[1] / float(w)))

        if segmenter is not None and (si % a.seg_every == 0):
            try:
                seg = segmenter.seg(rgb)
                rr.log("/camera/seg", rr.SegmentationImage(seg))
                if d is not None:
                    fdepth = f * (d.shape[1] / float(w))
                    for nm2, colr in (("wall", [200, 60, 60]), ("floor", [90, 130, 90])):
                        ids = [i for i, n in segmenter.id2label.items() if n.lower() == nm2]
                        if ids:
                            _project_mask(rr, "/world/%s" % nm2, seg == ids[0], d, w, h, fdepth, colr)
            except Exception as e:
                print("seg frame note:", e)

    if jf:
        jf.close()

    # ---- dedup summary (tracking) ----
    if a.track and tracks:
        uniq = []
        for tid, t in tracks.items():
            if not t["pos"]:
                continue
            cls = t["cls"].most_common(1)[0][0]
            pos = np.median(np.array(t["pos"]), axis=0)
            uniq.append((tid, cls, pos, len(t["frames"]),
                         float(np.median(t["rng"])), t["corr"] / max(1, len(t["frames"]))))
        # log all unique obstacles at once (their median position), labeled cls#id
        name2id = {n: i for i, n in names.items()}
        rr.set_time("frame_idx", sequence=int(labeled_frames[-1]))
        try:
            rr.log("/world/obstacle_set",
                   rr.Points3D([u[2].tolist() for u in uniq],
                               labels=["%s#%d" % (u[1], u[0]) for u in uniq],
                               colors=[_class_color(name2id.get(u[1], 0)) for u in uniq],
                               radii=0.2))
        except Exception as e:  # a viz glitch must never lose the recording
            print("obstacle_set log note:", e)
        by = collections.Counter(u[1] for u in uniq)
        print("\nDEDUP: %d raw detections -> %d UNIQUE tracked obstacles" % (n_obj, len(uniq)))
        for c, k in by.most_common():
            print("  %-16s %d unique" % (c, k))
        print("  (each: class, median range m, %% frames in forward corridor)")
        for tid, cls, pos, seen, rmed, corrf in sorted(uniq, key=lambda x: x[4])[:12]:
            print("    %-14s #%-3d  %.1fm  corridor=%d%%  seen=%d" % (cls, tid, rmed, int(100 * corrf), seen))

    try:
        rec = rr.get_global_data_recording()
        if rec is not None:
            rec.flush()
    except Exception:
        pass
    print("\nWROTE labeled .rrd -> %s  (%d detections%s%s)"
          % (a.out, n_obj, ", tracked" if a.track else "", ", segmented" if a.seg else ""))


if __name__ == "__main__":
    sys.exit(main())
