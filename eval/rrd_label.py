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

# Camera intrinsics in RGB pixels, set once in main(). "cyv" is the vertical centre used for
# back-projection: the calibrated cy, or the per-recording floor-fitted horizon when available.
_K = {}
# Back-projected semantic points collected for the validation gate, keyed "floor" / "wall".
_VAL = {"floor": [], "wall": []}

# Calibrated K1 head camera (camera_info). The recording's own pinhole is synthesized from the
# node's hfov setting and must not be trusted for 3-D placement -- see floor_calibration.
K1_FX, K1_FY, K1_CX, K1_CY = 205.8, 205.8, 247.3, 236.9
NOMINAL_CAM_HEIGHT_M = 0.86


def _kd(dw, dh):
    """Intrinsics rescaled to a depth image of (dw, dh) -- identical when depth == rgb size."""
    sx, sy = dw / float(_K["w"]), dh / float(_K["h"])
    return _K["fx"] * sx, _K["fy"] * sy, _K["cx"] * sx, _K["cyv"] * sy


def floor_calibration(depth, spread_max=0.03, min_frames=5):
    """Fit the camera's vertical geometry from the recording's OWN depth.

    A flat floor seen by a level camera at height H satisfies (v - cy) * Z = H * fy on every floor
    row. Per frame, scan candidate horizon rows and keep the one that makes (v - cy) * Z most
    constant over the lower rows; frames that obey it within `spread_max` are floor-dominated.
    Their consensus gives the horizon row (absorbs a small head pitch) and H*fy. Depth is taken as
    Z-depth along the optical axis (stereo convention). Returns None if too few clean frames.
    """
    import numpy as np
    cys = np.arange(180.0, 280.0, 0.25)
    fits = []
    for _fi, img in depth.items():
        h, w = img.shape[:2]
        sub = img[h // 2:, w // 2 - 72: w // 2 + 72]
        valid = np.isfinite(sub) & (sub > 0.3) & (sub < 6.0)
        with np.errstate(all="ignore"):
            med = np.nanmedian(np.where(valid, sub, np.nan), axis=1)
        med[valid.sum(axis=1) <= 60] = np.nan
        rows = np.arange(h // 2, h, dtype=np.float64)
        ok = np.isfinite(med)
        if ok.sum() < 40:
            continue
        r, z = rows[ok], med[ok]
        d = r[None, :] - cys[:, None]
        m = d > 8.0
        kk = np.where(m, d * z[None, :], np.nan)
        with np.errstate(all="ignore"):
            spread = np.nanstd(kk, axis=1) / np.nanmean(kk, axis=1)
        spread[m.sum(axis=1) < 40] = np.inf
        i = int(np.argmin(spread))
        if np.isfinite(spread[i]) and spread[i] < spread_max:
            fits.append((float(spread[i]), float(cys[i]), float(np.nanmedian(kk[i]))))
    if len(fits) < min_frames:
        return None
    f = np.array(fits)
    return {"clean_frames": len(fits), "median_spread": float(np.median(f[:, 0])),
            "horizon_row": float(np.median(f[:, 1])), "k_m_px": float(np.median(f[:, 2]))}


def recorded_pinhole(path):
    """Best-effort read of the static /camera/rgb pinhole the node logged, (fx, fy, cx, cy)."""
    try:
        import rerun as rr
        rec = rr.dataframe.load_recording(path)
        t = rec.view(index="frame_idx", contents={"/camera/rgb": ["PinholeProjection"]}
                     ).select_static().read_all()
        for n in t.column_names:
            if "Pinhole" in n:
                v = t.column(n).to_pylist()[0]
                v = v[0] if isinstance(v, list) and v and isinstance(v[0], list) else v
                return float(v[0]), float(v[4]), float(v[6]), float(v[7])
    except Exception:  # noqa: BLE001 -- a missing pinhole is informational only
        pass
    return None


def validate_geometry(h_eff):
    """PASS/FAIL on the back-projected semantic geometry.

    floor: a plane Yup = a*X + b*Z + c fitted to floor points must be near-horizontal (tilt <= 5
           deg) and sit at the calibrated camera height (|height + h_eff| <= 0.10 m).
    walls: wall points must not land below the floor (<= 5% more than 0.10 m under it).
    """
    import math
    import numpy as np
    out = {"checks": {}}
    ok_all = True
    fl = np.concatenate(_VAL["floor"]) if _VAL["floor"] else None
    if fl is None or len(fl) < 200:
        out["checks"]["floor"] = {"status": "SKIP", "why": "too few floor points (%d)"
                                  % (0 if fl is None else len(fl))}
    else:
        A = np.c_[fl[:, 0], fl[:, 1], np.ones(len(fl))]
        coef, *_ = np.linalg.lstsq(A, fl[:, 2], rcond=None)
        tilt = math.degrees(math.atan(math.hypot(coef[0], coef[1])))
        height = float(np.median(fl[:, 2]))
        resid = float(np.std(fl[:, 2] - A @ coef))
        c = {"points": int(len(fl)), "tilt_deg": round(tilt, 2), "height_m": round(height, 3),
             "plane_residual_m": round(resid, 3)}
        good = tilt <= 5.0
        if h_eff is not None:
            c["expected_height_m"] = round(-h_eff, 3)
            good = good and abs(height + h_eff) <= 0.10
        c["status"] = "PASS" if good else "FAIL"
        ok_all = ok_all and good
        out["checks"]["floor"] = c
    wl = np.concatenate(_VAL["wall"]) if _VAL["wall"] else None
    if wl is None or len(wl) < 200:
        out["checks"]["walls"] = {"status": "SKIP", "why": "too few wall points (%d)"
                                  % (0 if wl is None else len(wl))}
    else:
        fh = out["checks"].get("floor", {}).get("height_m")
        ref = fh if isinstance(fh, float) else (-h_eff if h_eff else None)
        c = {"points": int(len(wl)),
             "height_span_m": round(float(np.percentile(wl[:, 2], 90) - np.percentile(wl[:, 2], 10)), 3)}
        if ref is not None:
            below = float(np.mean(wl[:, 2] < ref - 0.10))
            c["frac_below_floor"] = round(below, 4)
            good = below <= 0.05
        else:
            good = True
        c["status"] = "PASS" if good else "FAIL"
        ok_all = ok_all and good
        out["checks"]["walls"] = c
    out["status"] = "PASS" if ok_all else "FAIL"
    return out


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


def box_centroid_xyz(d, x1, y1, x2, y2, sw, sh, w, h, f):
    """Center of MASS of an object's visible surface: back-project EVERY valid depth pixel inside the
    box and return the median [X(right), Z(forward), Yup(up)]. Beats the box-center pixel for large or
    irregular items (a couch), where the geometric box center lands on an arm or a background corner --
    the depth-weighted centroid sits on the object's actual mass. Returns None if too few valid pixels."""
    import numpy as np
    dh, dw = d.shape[:2]
    fx1 = int(max(0, min(dw - 1, x1 * sw))); fx2 = int(max(0, min(dw, x2 * sw)))
    fy1 = int(max(0, min(dh - 1, y1 * sh))); fy2 = int(max(0, min(dh, y2 * sh)))
    if fx2 <= fx1 or fy2 <= fy1:
        return None
    patch = d[fy1:fy2, fx1:fx2]
    ys, xs = np.mgrid[fy1:fy2, fx1:fx2]
    m = (patch > 0.1) & (patch < 12.0) & np.isfinite(patch)
    if int(m.sum()) < 8:
        return None
    z = patch[m].astype(np.float64)
    rx = xs[m] / sw; ry = ys[m] / sh                 # depth-res pixel -> rgb-res pixel (rgb focal f)
    X = (rx - _K["cx"]) * z / _K["fx"]          # calibrated principal point, not the image centre
    Yup = -(ry - _K["cyv"]) * z / _K["fy"]
    return [float(np.median(X)), float(np.median(z)), float(np.median(Yup))]


def _class_color(cls):
    return [(37 * (cls + 1)) % 256, (91 * (cls + 3)) % 256, (151 * (cls + 7)) % 256]


class Segmenter:
    """SegFormer-B0 finetuned on ADE20K (150 classes incl. wall/floor/ceiling/door). CPU-OK, offline."""
    def __init__(self, model_id="nvidia/segformer-b0-finetuned-ade-512-512"):
        from transformers import SegformerImageProcessor, SegformerForSemanticSegmentation
        import torch
        self.torch = torch
        self.proc = SegformerImageProcessor.from_pretrained(model_id)
        # GPU when present: this model never left the CPU before, even with CUDA available.
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SegformerForSemanticSegmentation.from_pretrained(model_id).eval().to(self.device)
        print("  SegFormer %s on %s" % (model_id, self.device))
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
        inp = {k: v.to(self.device) for k, v in self.proc(images=rgb, return_tensors="pt").items()}
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
    fxd, fyd, cxd, cyd = _kd(dw, dh)
    X = (xs - cxd) * Z / fxd
    Yup = -(ys - cyd) * Z / fyd
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
    fxd, fyd, cxd, cyd = _kd(dw, dh)
    X = (xx - cxd) * Zs / fxd
    Yup = -(yy - cyd) * Zs / fyd
    P = np.stack([X, Zs, Yup], axis=1)
    key = entity.rsplit("/", 1)[-1]
    if key in _VAL:
        _VAL[key].append(P)                     # kept for the validation gate
    rr.log(entity, rr.Points3D(P, colors=color, radii=0.015))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stride", type=int, default=2, help="label every Nth rgb frame")
    ap.add_argument("--conf", type=float, default=0.30)
    ap.add_argument("--hfov", type=float, default=None,
                    help="OVERRIDE: derive fx=fy from an HFOV and put the principal point at the image "
                         "centre (the old behaviour). Default: calibrated K1 intrinsics.")
    ap.add_argument("--fx", type=float, default=K1_FX)
    ap.add_argument("--fy", type=float, default=K1_FY)
    ap.add_argument("--cx", type=float, default=K1_CX)
    ap.add_argument("--cy", type=float, default=K1_CY)
    ap.add_argument("--no-floor-cal", action="store_true",
                    help="do NOT fit the horizon from this recording's floor (use --cy as-is)")
    ap.add_argument("--seg-model", default="nvidia/segformer-b0-finetuned-ade-512-512",
                    help="ADE20K SegFormer checkpoint (larger = better walls, costs GPU time)")
    ap.add_argument("--validation-json", default=None,
                    help="write the geometry validation gate (floor tilt/height, walls vs floor)")
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
    ap.add_argument("--merge-radius", type=float, default=0.0,
                    help="merge same-class obstacles whose 3D positions are within this many meters "
                         "into ONE -- collapses ByteTrack fragmentation (a cabinet re-acquired under "
                         "many track ids inflates the unique count). 0 = off (one obstacle per track).")
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
    segmenter = Segmenter(a.seg_model) if a.seg else None

    # ---- intrinsics: calibrated by default, never the synthesized recording pinhole ----
    h0, w0 = next(iter(images.values())).shape[:2]
    if a.hfov is not None:
        fx = fy = focal_px(w0, a.hfov)
        cx, cy = w0 / 2.0, h0 / 2.0
    else:
        fx, fy, cx, cy = a.fx, a.fy, a.cx, a.cy
    _K.update(fx=fx, fy=fy, cx=cx, cy=cy, cyv=cy, w=w0, h=h0)
    rec_pin = recorded_pinhole(a.rrd)
    if rec_pin is not None and abs(rec_pin[0] - fx) / fx > 0.05:
        print("  NOTE: the recording's own pinhole says fx=%.1f cx=%.1f cy=%.1f -- synthesized from the "
              "node's hfov at record time, NOT used (using fx=%.1f cx=%.1f)"
              % (rec_pin[0], rec_pin[2], rec_pin[3], fx, cx))
    cal = None if (a.no_floor_cal or not depth) else floor_calibration(depth)
    h_eff = None
    if cal is not None:
        _K["cyv"] = cal["horizon_row"] * (h0 / float(next(iter(depth.values())).shape[0]))
        h_eff = cal["k_m_px"] / fy
        print("  floor calibration: %d clean frames (spread %.1f%%) -> horizon row %.1f, H*fy %.1f "
              "=> camera height %.3f m at fy %.1f (nominal %.2f m)"
              % (cal["clean_frames"], 100 * cal["median_spread"], cal["horizon_row"],
                 cal["k_m_px"], h_eff, fy, NOMINAL_CAM_HEIGHT_M))
    else:
        print("  floor calibration: unavailable -- using cy=%.1f as the vertical centre" % cy)
    print("  intrinsics in use: fx=%.1f fy=%.1f cx=%.1f cy(vertical)=%.1f" % (fx, fy, cx, _K["cyv"]))

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
        f = _K["fx"]
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
            cen = box_centroid_xyz(d, x1, y1, x2, y2, sw, sh, w, h, f) if d is not None else None
            rec = {"frame_idx": int(fi), "cls": nm, "conf": round(conf, 3),
                   "track_id": tid, "box": [round(v, 1) for v in (x1, y1, x2, y2)]}
            if cen is not None:
                X, Z, Yup = cen                                   # depth-weighted center of mass
                bearing = math.atan2(X, Z)
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
        # same-class 3D merge: collapse fragments of one physical object (one cabinet re-acquired under
        # many track ids) into a single obstacle. Greedy, anchored on the most-seen fragment. Opt-in.
        if a.merge_radius > 0 and len(uniq) > 1:
            order = sorted(range(len(uniq)), key=lambda i: -uniq[i][3])   # most-seen first = the anchor
            used = [False] * len(uniq)
            merged = []
            for i in order:
                if used[i]:
                    continue
                used[i] = True
                tid, cls, pos, seen, rmed, corrf = uniq[i]
                for j in order:
                    if used[j] or uniq[j][1] != cls:
                        continue
                    if float(np.linalg.norm(uniq[j][2] - pos)) <= a.merge_radius:
                        used[j] = True
                        seen += uniq[j][3]                                # roll the fragment's frames in
                merged.append((tid, cls, pos, seen, rmed, corrf))
            print("MERGE: %d tracked -> %d after %.2fm same-class 3D merge"
                  % (len(uniq), len(merged), a.merge_radius))
            uniq = merged
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

    # ---- geometry validation gate: "labeled properly" as numbers, not an assertion ----
    val = validate_geometry(h_eff)
    val["intrinsics"] = {k: round(float(v), 3) for k, v in _K.items()}
    val["floor_calibration"] = cal
    val["recorded_pinhole"] = rec_pin
    print("\n=== GEOMETRY VALIDATION: %s ===" % val["status"])
    for name, c in val["checks"].items():
        print("  %-6s %s" % (name, c))
    if a.validation_json:
        with open(a.validation_json, "w", encoding="utf-8") as fh:
            json.dump(val, fh, indent=2)
        print("wrote %s" % a.validation_json)
    return 0 if val["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
