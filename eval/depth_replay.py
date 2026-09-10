#!/usr/bin/env python3
"""depth_replay.py -- replay RECORDED depth frames through gap-steer policies, offline.

WHY THIS EXISTS. Gap steer took four patches in one day and still drove into furniture. The actual
defect was a SIGN INVERSION in the proportional command -- a gap on the left produced a right turn,
9 of 9 times -- which no amount of threshold tuning could fix, and which the text log could not
reveal, because it printed the gap bearing and the yaw command without ever asserting that one
should follow from the other.

So this harness does what the field could not: replay the depth already recorded in the .rrd,
recompute the gap profile exactly as the node does, run candidate policies over it, and CHECK
INVARIANTS. An invariant is a property any correct policy must satisfy -- e.g. "a steering command
must turn toward the gap it just chose". That single check would have caught the sign bug on the
first run instead of the fourth patch.

WHAT IT IS NOT. Not a simulator: it does not integrate motion, so it cannot say whether a detour
would have succeeded. It reports what each policy WOULD command on real recorded geometry and
whether those commands are self-consistent -- which is precisely the bug class that has been
costing field sessions.

BACKENDS. The heavy step is a low percentile per column-bin over the depth band: 544x448 px/frame,
~2000 frames/run, times however many configs are swept.
  numpy  (default) vectorised; always available; no import side effects.
  torch            same math on whatever device torch reports, picking CUDA automatically when
                   present -- so a repaired dGPU lights this path up with no code change.
The AMD NPU is deliberately NOT a backend: XDNA is a quantized-neural-network accelerator reached
through the VitisAI execution provider, and a percentile over a slice is not an NN graph -- putting
it there would be theatre. The honest place for the NPU is a later offline LABELLING stage, running
a larger detector or segmentation model over the recorded RGB to produce ground truth better than
the Orin can afford in real time. That stage consumes frames, not this reduction.

USAGE
  python eval/depth_replay.py --rrd runs/<id>/k1_follow_*.rrd
  python eval/depth_replay.py --rrd <f> --backend torch --limit 400
  python eval/depth_replay.py --rrd <f> --json out.json --compare-legacy
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

# ---------------------------------------------------------------------------- config

# Mirrors the node's shipped defaults. Anything the .rrd cannot supply has to come from here, so
# every key is named exactly as in defaults.yaml to keep the two greppable together.
DEFAULTS = {
    "hfov_deg": 105.8,
    "obstacle_band_top": 0.3,
    "obstacle_band_bot": 0.75,
    "obstacle_max_m": 5.0,
    "obstacle_pctile": 12.0,
    "obstacle_min_valid": 70,
    "obstacle_brake_start": 1.15,
    "obstacle_brake_stop": 0.70,
    "gap_profile_bins": 48,
    "gap_horizon_m": 1.375,
    "gap_steer_trigger_frac": 0.80,
    "gap_steer_rate": 0.24,
    "gap_steer_deadband_deg": 8.0,
    "gap_max_detour_deg": 36.0,
    "gap_critical_m": 0.85,
    "gap_critical_detour_deg": 52.9,
    "gap_fit_margin_m": 0.12,
    "corridor_margin_m": 0.15,
    "robot_width_m": 0.46,
    "robot_length_m": 0.20,
    "k_yaw": 0.477,
    "vyaw_max": 0.30,
    "vx_max": 0.18,
}


def focal_px(w_img, hfov_deg):
    """Verbatim from robot/perception.py -- the replay is worthless if the projection differs."""
    return (w_img / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def need_width(cfg):
    """Swept width the node demands of an opening. Mirrors _gap_profile's need_w."""
    hw = 0.5 * max(0.05, cfg["robot_width_m"])
    hd = 0.5 * max(0.0, cfg["robot_length_m"])
    return 2.0 * (math.hypot(hw, hd)
                  + max(0.0, cfg["corridor_margin_m"])
                  + max(0.0, cfg["gap_fit_margin_m"]))


# ---------------------------------------------------------------------------- backends

def _bin_cube(band, nbins, max_m):
    """(rows, cols) metres -> (rows, nbins, wmax) with every invalid pixel as NaN.

    The node uses np.linspace column edges, so bin widths differ by up to one pixel and the
    band is not directly reshapeable. Padding each bin to the widest width with NaN produces a
    single dense cube whose per-bin VALID SET is identical to the node's, which is what lets
    one nanpercentile replace nbins separate percentile calls -- the actual hot spot.
    """
    rows, cols = band.shape
    edges = np.linspace(0, cols, nbins + 1).astype(int)
    wmax = int(np.max(np.diff(edges)))
    cube = np.full((rows, nbins, wmax), np.nan, dtype=np.float32)
    valid = np.isfinite(band) & (band > 0.15) & (band < max_m)
    b = np.where(valid, band, np.nan).astype(np.float32)
    for i in range(nbins):                 # slice-copies only, no percentile work
        s0, s1 = edges[i], edges[i + 1]
        if s1 - s0 < 1:
            continue
        cube[:, i, : s1 - s0] = b[:, s0:s1]
    return cube


def torch_count_valid(t, x):
    """Valid (non-NaN) sample count per row, as a host array."""
    return (~t.isnan(x)).sum(dim=1).detach().to("cpu").numpy()


class NumpyBackend:
    name = "numpy"

    @staticmethod
    def bin_percentile(band, nbins, pctile, min_px, max_m):
        """(rows, cols) metres -> (nbins,) low-percentile clearance, NaN where unmeasurable.

        Same rule as the node: a bin without enough valid pixels stays NaN, because "I cannot see"
        must never read as "nothing is there".
        """
        cube = _bin_cube(band, nbins, max_m)
        counts = np.count_nonzero(~np.isnan(cube), axis=(0, 2))
        with np.errstate(all="ignore"):
            out = np.asarray(np.nanpercentile(cube, pctile, axis=(0, 2)), dtype=np.float64)
        out[counts < min_px] = np.nan       # "cannot see" must never read as "nothing there"
        return out


class TorchBackend:
    """Same math on whatever device torch offers. Falls back to CPU silently -- an accelerator is
    an optimisation here, never a correctness dependency."""
    name = "torch"

    def __init__(self, device=None):
        import torch
        self.torch = torch
        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        self.desc = "%s (%s)" % (
            self.device,
            torch.cuda.get_device_name(0) if self.device == "cuda" else "cpu tensors")

    def bin_percentile(self, band, nbins, pctile, min_px, max_m):
        t = self.torch
        cube = _bin_cube(band, nbins, max_m)                  # (rows, nbins, wmax)
        x = t.as_tensor(cube, device=self.device)
        x = x.permute(1, 0, 2).reshape(cube.shape[1], -1)     # (nbins, rows*wmax)
        counts = torch_count_valid(t, x)
        q = t.nanquantile(x, pctile / 100.0, dim=1)
        out = q.detach().to("cpu").numpy().astype(np.float64)
        out[counts < min_px] = np.nan
        return out


def make_backend(kind, device=None):
    if kind == "numpy":
        return NumpyBackend()
    if kind == "torch":
        try:
            b = TorchBackend(device)
            print("[backend] torch on %s" % b.desc)
            return b
        except Exception as e:  # noqa: BLE001
            print("[backend] torch unavailable (%s) -- falling back to numpy" % e)
            return NumpyBackend()
    raise SystemExit("unknown backend %r" % kind)


def corridor_clearance(band, cfg, f, cx):
    """Recompute the forward-corridor clearance from depth rather than trusting a logged scalar.

    WHY: /reflex/clearance_m does not exist in older recordings -- every local .rrd from July
    and August lacks the column -- so a harness keyed to it silently scores zero frames on most
    of the archive. Recomputing makes the replay work on any recording with depth, and lets the
    logged value be CROSS-CHECKED against the geometry, which is how the 1.14 -> 0.76 m
    single-sample clearance cliff gets diagnosed instead of guessed at.

    Mirrors the footprint corridor: half the robot width plus the corridor margin, projected to
    a pixel half-width at each pixel's own range, then a low percentile of what is inside.
    """
    _rows, cols = band.shape
    half_m = 0.5 * max(0.05, cfg["robot_width_m"]) + max(0.0, cfg["corridor_margin_m"])
    valid = np.isfinite(band) & (band > 0.15) & (band < cfg["obstacle_max_m"])
    if not valid.any():
        return None
    u = np.arange(cols, dtype=np.float32)[None, :] - cx
    with np.errstate(all="ignore"):
        halfpx = f * half_m / np.where(valid, band, np.nan)
    inside = valid & (np.abs(u) <= halfpx)
    v = band[inside]
    if v.size < cfg["obstacle_min_valid"]:
        return None
    return float(np.percentile(v, cfg["obstacle_pctile"]))


# ---------------------------------------------------------------------------- gap profile

def gap_profile(clr, cx, f, cols, cfg, target_bearing, max_detour_deg):
    """Port of Follower._gap_profile, from per-bin clearance to (bearing, width, range).

    Returns (result, reason); result is None when nothing is passable and reason names the filter
    that killed it -- the same distinction the on-robot GAP-REJECT line makes.
    """
    nb = len(clr)
    bar = cfg["gap_horizon_m"] if cfg["gap_horizon_m"] > 0 else cfg["obstacle_brake_start"]
    passable = np.isfinite(clr) & (clr >= bar)
    if not passable.any():
        return None, "no_passable_bin"

    edges = np.linspace(0, cols, nb + 1).astype(int)
    runs, i = [], 0
    while i < nb:
        if not passable[i]:
            i += 1
            continue
        j = i
        while j + 1 < nb and passable[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1

    need_w = need_width(cfg)
    best, n_narrow, n_detour = None, 0, 0
    for (i0, i1) in runs:
        a0 = math.atan((edges[i0] - cx) / f)
        a1 = math.atan((edges[i1 + 1] - cx) / f)
        dth = abs(a1 - a0)
        r = float(np.nanmin(clr[i0:i1 + 1]))
        if not math.isfinite(r):
            continue
        width_m = 2.0 * r * math.sin(0.5 * dth)
        if width_m < need_w:
            n_narrow += 1
            continue
        cbear = 0.5 * (a0 + a1)
        detour = abs(cbear - target_bearing)
        if detour > math.radians(max(0.0, max_detour_deg)):
            n_detour += 1
            continue
        key = (detour, -width_m)
        if best is None or key < best[0]:
            best = (key, cbear, width_m, r)
    if best is None:
        return None, ("too_narrow" if n_narrow >= max(1, n_detour) else "off_follow_line")
    return (best[1], best[2], best[3]), "ok"


# ---------------------------------------------------------------------------- policies

def policy_current(gap, bearing, clearance, cfg):
    """What the SHIPPED code commands, reproducing the veto stack in order, so a regression in the
    real node shows up here as a behaviour difference."""
    if gap is None:
        return 0.0, "no_gap"
    gb = gap[0]
    if abs(gb) <= math.radians(cfg["gap_steer_deadband_deg"]):
        return 0.0, "deadband"
    cmd = -cfg["k_yaw"] * gb          # post-fix sign: face the gap, exactly as the tracker does
    cmd = max(-cfg["gap_steer_rate"], min(cfg["gap_steer_rate"], cmd))
    return cmd, "steer"


def policy_legacy(gap, bearing, clearance, cfg):
    """The PRE-FIX command, kept so the harness demonstrates the sign bug on recorded data rather
    than by argument. Never ship this."""
    if gap is None:
        return 0.0, "no_gap"
    gb = gap[0]
    if abs(gb) <= math.radians(cfg["gap_steer_deadband_deg"]):
        return 0.0, "deadband"
    return cfg["gap_steer_rate"] * max(-1.0, min(1.0, gb / math.radians(30.0))), "steer"


# ---------------------------------------------------------------------------- invariants

def check_invariants(rows):
    """Properties any correct steering policy must satisfy on real geometry.

    This is the harness's reason for existing. Numbers describe behaviour; invariants CATCH BUGS,
    and a violated invariant is a defect however good the numbers look.
    """
    res = []
    steer = [r for r in rows if r["why"] == "steer" and r.get("gap_deg") is not None]

    # (1) The command must turn TOWARD the gap it just chose. Bearing is positive-right and
    #     positive vyaw turns left, so facing a gap at gb requires sign(cmd) == -sign(gb).
    #     This is the invariant the sign inversion violated on 9 of 9 field decisions.
    bad = [r for r in steer
           if r["gap_deg"] != 0.0
           and math.copysign(1, r["cmd"]) != math.copysign(1, -r["gap_deg"])]
    res.append(("steers toward the chosen gap", len(steer) - len(bad), len(steer),
                "sign(cmd) == -sign(gap_bearing)"))

    # (2) Never exceed the configured rate. The proportional path documents that it "can never
    #     command a harder yaw than the fixed-rate path it replaces".
    over = [r for r in steer if abs(r["cmd"]) > r["rate"] + 1e-9]
    res.append(("magnitude within gap_steer_rate", len(steer) - len(over), len(steer),
                "|cmd| <= gap_steer_rate"))

    # (3) A gap dead ahead is not a detour: the command must be exactly zero so full yaw authority
    #     returns to the tracker.
    fwd = [r for r in rows if r.get("gap_deg") is not None and abs(r["gap_deg"]) <= 1.0]
    fwd_bad = [r for r in fwd if abs(r["cmd"]) > 1e-9]
    res.append(("no detour when the gap is dead ahead", len(fwd) - len(fwd_bad), len(fwd),
                "|gap| <= 1deg implies cmd == 0"))
    return res


# ---------------------------------------------------------------------------- rrd loading

def load_recording(path):
    try:
        import rerun as rr
    except ImportError as e:
        raise SystemExit("rerun-sdk required to read .rrd: pip install rerun-sdk") from e
    if not hasattr(rr, "dataframe"):
        raise SystemExit("this rerun-sdk lacks the dataframe reader; need >= 0.23")
    return rr.dataframe.load_recording(path)


def _first_col(tbl, needle):
    for n in tbl.column_names:
        if needle in n:
            return n
    return None


def load_scalars(rec, entities):
    """frame_idx -> {entity: value} for the scalar timeseries we score against."""
    out = {}
    for ent in entities:
        try:
            tbl = rec.view(index="frame_idx", contents={ent: ["Scalar"]}).select().read_all()
        except Exception:  # noqa: BLE001 -- a missing entity must not abort the replay
            continue
        idx = _first_col(tbl, "frame_idx")
        val = _first_col(tbl, "Scalar")
        if not idx or not val:
            continue
        for k, x in zip(tbl.column(idx).to_pylist(), tbl.column(val).to_pylist()):
            if k is None or x is None:
                continue
            if isinstance(x, list):
                if not x:
                    continue
                x = x[0]
            try:
                out.setdefault(int(k), {})[ent] = float(x)
            except (TypeError, ValueError):
                continue
    return out


# rerun ChannelDatatype -> numpy. The K1 depth recordings carry 34 (F32) and DepthMeter 1.0,
# i.e. float32 already in metres; a uint16 reading of the same bytes yields 0% valid pixels.
_RR_CHANNEL_DTYPE = {
    6: np.uint8, 7: np.uint16, 8: np.uint32, 9: np.uint64,
    33: np.float16, 34: np.float32, 35: np.float64,
}
_DTYPE_BY_ITEMSIZE = {1: np.uint8, 2: np.uint16, 4: np.float32, 8: np.float64}


def _fmt_get(fmt, *names):
    """Read a field from an ImageFormat that may be a dict OR an attribute object.

    This SDK hands back a plain dict; earlier/later versions expose attributes. The old code
    only checked attributes, so width/height/datatype were all silently missed.
    """
    if fmt is None:
        return None
    if isinstance(fmt, dict):
        for n in names:
            if fmt.get(n) is not None:
                return fmt[n]
        return None
    for n in names:
        if hasattr(fmt, n) and getattr(fmt, n) is not None:
            return getattr(fmt, n)
    return None


def _wh_from_format(fmt, nbytes=None):
    """Return (width, height, dtype) for a recorded depth frame.

    The declared channel_datatype is preferred, but it is CROSS-CHECKED against the actual
    buffer size and overridden by size inference when the two disagree -- a decode that
    silently contradicts its container is how the uint16/float32 bug survived a whole run.
    """
    w = _fmt_get(fmt, "width", "Width")
    h = _fmt_get(fmt, "height", "Height")
    if not w or not h:
        w, h = 544, 448          # the K1 head camera, which every K1 recording uses
    w, h = int(w), int(h)

    dt = None
    cd = _fmt_get(fmt, "channel_datatype", "ChannelDatatype")
    if cd is not None:
        try:
            dt = _RR_CHANNEL_DTYPE.get(int(cd))
        except (TypeError, ValueError):
            dt = None

    if nbytes:
        px = w * h
        if px > 0 and nbytes % px == 0:
            inferred = _DTYPE_BY_ITEMSIZE.get(nbytes // px)
            if inferred is not None and (dt is None
                                         or np.dtype(dt).itemsize != nbytes // px):
                dt = inferred
    if dt is None:
        dt = np.float32
    return w, h, dt


def iter_depth(rec, limit=None):
    """Yield (frame_idx, depth_metres 2-D float32), decoding the raw ImageBuffer with the recorded
    ImageFormat + DepthMeter so nothing is assumed about dtype or scale."""
    tbl = rec.view(index="frame_idx",
                   contents={"/camera/depth": ["ImageBuffer", "ImageFormat", "DepthMeter"]}
                   ).select().read_all()
    ci = _first_col(tbl, "frame_idx")
    cb = _first_col(tbl, "ImageBuffer")
    cf = _first_col(tbl, "ImageFormat")
    cm = _first_col(tbl, "DepthMeter")
    if not (ci and cb):
        raise SystemExit("no /camera/depth ImageBuffer in this recording "
                         "(was it recorded with --rerun and image logging on?)")
    idxs = tbl.column(ci).to_pylist()
    bufs = tbl.column(cb).to_pylist()
    fmts = tbl.column(cf).to_pylist() if cf else [None] * len(idxs)
    mets = tbl.column(cm).to_pylist() if cm else [None] * len(idxs)

    n, last_fmt, last_scale, announced = 0, None, None, False
    for k, b, fmt, met in zip(idxs, bufs, fmts, mets):
        if b is None:
            continue
        if isinstance(b, list):
            if not b:
                continue
            b = b[0]
        if fmt:
            last_fmt = fmt[0] if isinstance(fmt, list) and fmt else fmt
        if met:
            m0 = met[0] if isinstance(met, list) and met else met
            try:
                last_scale = float(m0)
            except (TypeError, ValueError):
                pass
        blob = bytes(b)
        w, h, dt = _wh_from_format(last_fmt, len(blob))
        raw = np.frombuffer(blob, dtype=dt)
        if raw.size < w * h:
            continue
        img = raw[:w * h].reshape(h, w).astype(np.float32)
        if last_scale and last_scale > 0:
            img = img / last_scale
        if not announced:
            announced = True
            finite = img[np.isfinite(img)]
            frac = (100.0 * ((finite > 0.15) & (finite < 5.0)).mean()) if finite.size else 0.0
            print("[depth] %dx%d %s, DepthMeter=%s -> %.1f%% of pixels in (0.15, 5.0) m"
                  % (w, h, np.dtype(dt).name, last_scale, frac))
            if frac < 5.0:
                print("[depth] WARNING: almost nothing is in range -- the decode is probably "
                      "wrong, and every downstream number will be noise.")
        yield int(k), img
        n += 1
        if limit and n >= limit:
            return


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rrd", required=True)
    ap.add_argument("--backend", default="numpy", choices=("numpy", "torch"))
    ap.add_argument("--device", default=None, help="torch device override (cuda / cpu)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N depth frames (0 = all)")
    ap.add_argument("--json", default=None, help="write the scorecard here (for the auto-tune loop)")
    ap.add_argument("--compare-legacy", action="store_true",
                    help="also replay the PRE-FIX command, demonstrating the sign bug on data")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    be = make_backend(a.backend, a.device)

    t0 = time.time()
    rec = load_recording(a.rrd)
    scal = load_scalars(rec, ["/reflex/clearance_m", "/follow/bearing", "/cmd/vyaw",
                              "/reflex/vx_cap", "/fsm/state_id", "/head/yaw_deg"])
    print("[load] scalars for %d frames in %.1fs" % (len(scal), time.time() - t0))

    bar = cfg["gap_horizon_m"] if cfg["gap_horizon_m"] > 0 else cfg["obstacle_brake_start"]
    trig = cfg["obstacle_brake_stop"] + (bar - cfg["obstacle_brake_stop"]) * cfg["gap_steer_trigger_frac"]
    print("[cfg] horizon %.3fm -> engage %.3fm | need_w %.3fm | deadband %.0fdeg | "
          "detour %.1f/%.1fdeg | critical %.2fm"
          % (bar, trig, need_width(cfg), cfg["gap_steer_deadband_deg"],
             cfg["gap_max_detour_deg"], cfg["gap_critical_detour_deg"], cfg["gap_critical_m"]))

    rows, legacy_rows, reasons = [], [], {}
    skips, cliff_deltas = {}, []
    t1, nf, nskip = time.time(), 0, 0
    for k, dimg in iter_depth(rec, a.limit or None):
        nf += 1
        h, w = dimg.shape
        f = focal_px(w, cfg["hfov_deg"])
        y0, y1 = int(h * cfg["obstacle_band_top"]), int(h * cfg["obstacle_band_bot"])
        if y1 - y0 < 4:
            continue
        nb = int(cfg["gap_profile_bins"])
        min_px = max(4, int(cfg["obstacle_min_valid"] // max(1, nb // 8)))
        clr = be.bin_percentile(dimg[y0:y1, :], nb, cfg["obstacle_pctile"], min_px,
                                cfg["obstacle_max_m"])

        s = scal.get(k, {})
        bearing = s.get("/follow/bearing", 0.0)
        logged = s.get("/reflex/clearance_m")
        # Prefer the recomputed geometry, fall back to the logged scalar, and record an
        # explicit reason for every skip: "no clearance available" and "the path was clear"
        # are opposite findings, and collapsing them makes a broken replay look like a clean
        # run -- the same defect removed from the node's gap logging this morning.
        clearance = corridor_clearance(dimg[y0:y1, :], cfg, f, w * 0.5)
        if clearance is None:
            clearance = logged
        if clearance is None:
            skips["no_clearance"] = skips.get("no_clearance", 0) + 1
            nskip += 1
            continue
        if logged is not None:
            cliff_deltas.append(abs(clearance - logged))
        if clearance >= trig:
            skips["path_clear"] = skips.get("path_clear", 0) + 1
            nskip += 1
            continue
        critical = cfg["gap_critical_m"] > 0 and clearance <= cfg["gap_critical_m"]
        cap = max(cfg["gap_max_detour_deg"], cfg["gap_critical_detour_deg"]) if critical \
            else cfg["gap_max_detour_deg"]

        gap, reason = gap_profile(clr, w * 0.5, f, w, cfg, bearing, cap)
        reasons[reason] = reasons.get(reason, 0) + 1

        cmd, why = policy_current(gap, bearing, clearance, cfg)
        rows.append({"frame": k, "clearance": clearance, "bearing_deg": math.degrees(bearing),
                     "gap_deg": math.degrees(gap[0]) if gap else None,
                     "cmd": cmd, "why": why, "critical": critical,
                     "rate": cfg["gap_steer_rate"]})
        if a.compare_legacy:
            lc, lw = policy_legacy(gap, bearing, clearance, cfg)
            legacy_rows.append({"gap_deg": math.degrees(gap[0]) if gap else None,
                                "cmd": lc, "why": lw, "rate": cfg["gap_steer_rate"]})

    dt = max(1e-6, time.time() - t1)
    print("[replay] %d depth frames, %d skipped, %d scored in %.1fs (%.1f frames/s, %s)"
          % (nf, nskip, len(rows), dt, nf / dt, be.name))
    for _r, _c in sorted(skips.items(), key=lambda kv: -kv[1]):
        print("    skip: %-16s %d" % (_r, _c))
    if cliff_deltas:
        _cd = np.array(cliff_deltas)
        print("    recomputed vs LOGGED clearance: n=%d mean|d|=%.3fm p90=%.3fm max=%.3fm"
              % (len(_cd), _cd.mean(), np.percentile(_cd, 90), _cd.max()))
        print("      (large disagreement = the logged corridor and the depth geometry "
              "diverge -- the clearance-cliff suspect)")

    print("\n=== profile outcome ===")
    for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  %-18s %d" % (r, c))

    steer = [r for r in rows if r["why"] == "steer"]
    print("\n=== commands ===")
    print("  scored frames      %d" % len(rows))
    print("  produced a steer   %d" % len(steer))
    if steer:
        left = sum(1 for r in steer if r["cmd"] > 0)
        print("  left / right       %d / %d" % (left, len(steer) - left))
        print("  |cmd| mean/max     %.3f / %.3f"
              % (float(np.mean([abs(r["cmd"]) for r in steer])),
                 float(np.max([abs(r["cmd"]) for r in steer]))))
        rev = sum(1 for p, q in zip(steer, steer[1:]) if p["cmd"] * q["cmd"] < 0)
        print("  sign reversals     %d (%.1f%% of consecutive steers) -- gait cost"
              % (rev, 100.0 * rev / max(1, len(steer) - 1)))
    print("  critical frames    %d" % sum(1 for r in rows if r["critical"]))

    print("\n=== INVARIANTS (a violation is a defect, whatever the numbers say) ===")
    inv = check_invariants(rows)
    ok_all = True
    for name, good, total, rule in inv:
        if total - good:
            ok_all = False
        print("  [%s] %-38s %d/%d   (%s)"
              % ("PASS" if good == total else "FAIL", name, good, total, rule))

    if a.compare_legacy and legacy_rows:
        print("\n=== PRE-FIX command on the SAME frames (demonstrates the sign bug) ===")
        for name, good, total, _rule in check_invariants(legacy_rows):
            print("  [%s] %-38s %d/%d"
                  % ("PASS" if good == total else "FAIL", name, good, total))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"rrd": os.path.basename(a.rrd), "backend": be.name,
                       "frames": nf, "scored": len(rows), "steers": len(steer),
                       "reasons": reasons,
                       "invariants": [{"name": n, "good": g, "total": t, "rule": r}
                                      for n, g, t, r in inv],
                       "config": cfg}, fh, indent=2)
        print("\nwrote %s" % a.json)

    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
