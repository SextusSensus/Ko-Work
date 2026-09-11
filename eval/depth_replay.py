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
import warnings

# A bin with no valid pixel is EXPECTED -- it becomes NaN, i.e. "cannot see", by design -- so
# numpy's "All-NaN slice" warning is noise here, and noise in a log is how real signal gets missed.
warnings.filterwarnings("ignore", message="All-NaN slice encountered")

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

    @staticmethod
    def batch_corridor(bands, cfg, f, cx):
        """(B, rows, cols) -> (B,) corridor clearance, NaN where unmeasurable.

        The REFERENCE: literally the per-frame corridor_clearance, looped. Every accelerated
        backend is judged against this, so it must never be "optimised" into something else.
        """
        out = np.full(len(bands), np.nan)
        for i in range(len(bands)):
            c = corridor_clearance(bands[i], cfg, f, cx)
            if c is not None:
                out[i] = c
        return out

    @staticmethod
    def batch_bins(bands, nbins, pctile, min_px, max_m):
        """(B, rows, cols) -> (B, nbins). The REFERENCE: the per-frame reduction, looped."""
        if len(bands) == 0:
            return np.zeros((0, nbins))
        return np.stack([NumpyBackend.bin_percentile(b, nbins, pctile, min_px, max_m)
                         for b in bands])


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

    # torch.quantile/nanquantile refuse very large inputs; keep every reduction well under it.
    _MAX_ELEMS = 8_000_000

    def _spans(self, n, per_item):
        step = max(1, int(self._MAX_ELEMS // max(1, per_item)))
        for s0 in range(0, n, step):
            yield s0, min(n, s0 + step)

    def batch_corridor(self, bands, cfg, f, cx):
        """Batched twin of corridor_clearance: one NaN-masked nanquantile per batch.

        Same valid window, same footprint test (a pixel at range r is inside when
        |u - cx| <= f*half_m/r), same low percentile, same min-valid rule -> NaN.
        """
        t = self.torch
        B, R, C = bands.shape
        half_m = 0.5 * max(0.05, cfg["robot_width_m"]) + max(0.0, cfg["corridor_margin_m"])
        u = (t.arange(C, device=self.device, dtype=t.float32) - float(cx)).abs().view(1, 1, C)
        nan = float("nan")
        out = np.full(B, np.nan)
        for s0, s1 in self._spans(B, R * C):
            x = t.as_tensor(bands[s0:s1], device=self.device)
            valid = t.isfinite(x) & (x > 0.15) & (x < cfg["obstacle_max_m"])
            # Invalid pixels get a dummy range of 1 so the division is finite; `valid` below
            # excludes them anyway, exactly as the reference's NaN comparisons do.
            halfpx = (f * half_m) / t.where(valid, x, t.ones_like(x))
            inside = valid & (u <= halfpx)
            v = t.where(inside, x, t.full_like(x, nan)).reshape(s1 - s0, -1)
            cnt = (~t.isnan(v)).sum(dim=1)
            q = t.nanquantile(v, cfg["obstacle_pctile"] / 100.0, dim=1)
            q = t.where(cnt >= int(cfg["obstacle_min_valid"]), q, t.full_like(q, nan))
            out[s0:s1] = q.detach().to("cpu").numpy()
        return out

    def batch_bins(self, bands, nbins, pctile, min_px, max_m):
        """Batched twin of the per-bin reduction.

        The node's np.linspace edges give bins that differ by up to one pixel, so a plain reshape
        is impossible. A (nbins, wmax) column-index map -- short bins padded with an index that
        points at an appended all-NaN column -- gathers the whole batch into one dense cube whose
        per-bin VALID SET is identical to the reference's, then a single nanquantile reduces it.
        """
        t = self.torch
        B, R, C = bands.shape
        if B == 0:
            return np.zeros((0, nbins))
        edges = np.linspace(0, C, nbins + 1).astype(int)
        wmax = int(np.max(np.diff(edges)))
        idx = np.full((nbins, wmax), C, dtype=np.int64)          # C == the all-NaN column
        for i in range(nbins):
            e0, e1 = edges[i], edges[i + 1]
            if e1 > e0:
                idx[i, : e1 - e0] = np.arange(e0, e1)
        gi = t.as_tensor(idx.reshape(-1), device=self.device)
        nan = float("nan")
        out = np.full((B, nbins), np.nan)
        for s0, s1 in self._spans(B, R * nbins * wmax):
            x = t.as_tensor(bands[s0:s1], device=self.device)
            valid = t.isfinite(x) & (x > 0.15) & (x < max_m)
            x = t.where(valid, x, t.full_like(x, nan))
            pad = t.full((s1 - s0, R, 1), nan, device=self.device, dtype=x.dtype)
            cube = t.cat([x, pad], dim=2).index_select(2, gi)     # (b, R, nbins*wmax)
            cube = cube.reshape(s1 - s0, R, nbins, wmax).permute(0, 2, 1, 3)
            cube = cube.reshape(s1 - s0, nbins, R * wmax)
            cnt = (~t.isnan(cube)).sum(dim=2)
            q = t.nanquantile(cube, pctile / 100.0, dim=2)
            q = t.where(cnt >= int(min_px), q, t.full_like(q, nan))
            out[s0:s1] = q.detach().to("cpu").numpy()
        return out

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


def _image_cells(col):
    """Yield one zero-copy uint8 view per row of a rerun ImageBuffer column; None for an empty or
    null row. No Python objects are materialised per pixel.

    MEASURED: to_pylist() turned every depth frame into a Python list of ~1M ints (5.8 s
    one-time on a 1 GB recording) and bytes(list) then cost 2.8 ms per frame to undo it. Arrow
    already holds the bytes contiguously; this walks the list offsets and slices that buffer.

    The column is list<list<uint8>>: a batch of component instances per row, each a byte blob.
    Any chunk not shaped that way falls back to the slow general path, so an SDK layout change
    costs speed, never correctness -- and the in-range check in iter_depth_batches still flags a
    decode that has silently gone wrong.
    """
    for chunk in col.chunks:
        try:
            outer = chunk.offsets.to_numpy(zero_copy_only=False)
            inner = chunk.values
            ioff = inner.offsets.to_numpy(zero_copy_only=False)
            data = inner.values.to_numpy(zero_copy_only=False)
            if data.dtype != np.uint8:
                raise TypeError("unexpected inner dtype %s" % data.dtype)
            ok = chunk.is_valid().to_numpy(zero_copy_only=False)
        except Exception:  # noqa: BLE001 -- layout surprise: fall back, stay correct
            for cell in chunk.to_pylist():
                if not cell:
                    yield None
                    continue
                c0 = cell[0] if isinstance(cell, list) else cell
                yield None if c0 is None else np.frombuffer(bytes(c0), dtype=np.uint8)
            continue
        for r in range(len(chunk)):
            j0, j1 = int(outer[r]), int(outer[r + 1])
            if not ok[r] or j1 <= j0:
                yield None
                continue
            yield data[int(ioff[j0]):int(ioff[j0 + 1])]


def iter_depth_batches(rec, batch, limit=None, timing=None):
    """Yield lists of (frame_idx, depth_metres HxW float32), at most `batch` per list.

    Decodes with the recorded ImageFormat + DepthMeter (nothing assumed about dtype or scale),
    and reports the in-range pixel fraction once so a wrong decode can never again be silent.
    """
    T = timing if timing is not None else {}
    t0 = time.perf_counter()
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
    # ImageFormat / DepthMeter are a few bytes per row, so plain conversion costs nothing.
    fmts = tbl.column(cf).to_pylist() if cf else [None] * len(idxs)
    mets = tbl.column(cm).to_pylist() if cm else [None] * len(idxs)
    cells = _image_cells(tbl.column(cb))
    T["columns"] = T.get("columns", 0.0) + (time.perf_counter() - t0)

    out, n, shape = [], 0, None
    last_fmt, last_scale, announced = None, None, False
    for k, cell, fmt, met in zip(idxs, cells, fmts, mets):
        td = time.perf_counter()
        if fmt:
            last_fmt = fmt[0] if isinstance(fmt, list) and fmt else fmt
        if met:
            m0 = met[0] if isinstance(met, list) and met else met
            try:
                last_scale = float(m0)
            except (TypeError, ValueError):
                pass
        if cell is None or k is None:
            continue
        w, h, dt = _wh_from_format(last_fmt, int(cell.nbytes))
        need = w * h * np.dtype(dt).itemsize
        if cell.nbytes < need:
            continue
        img = cell[:need].view(dt).reshape(h, w)          # a view: no copy for float32 depth
        if img.dtype != np.float32:
            img = img.astype(np.float32)
        if last_scale and last_scale > 0 and last_scale != 1.0:
            img = img / np.float32(last_scale)
        T["decode"] = T.get("decode", 0.0) + (time.perf_counter() - td)
        if not announced:
            announced = True
            finite = img[np.isfinite(img)]
            frac = (100.0 * ((finite > 0.15) & (finite < 5.0)).mean()) if finite.size else 0.0
            print("[depth] %dx%d %s, DepthMeter=%s -> %.1f%% of pixels in (0.15, 5.0) m"
                  % (w, h, np.dtype(dt).name, last_scale, frac))
            if frac < 5.0:
                print("[depth] WARNING: almost nothing is in range -- the decode is probably "
                      "wrong, and every downstream number will be noise.")
        if shape is not None and img.shape != shape and out:
            yield out                                    # a batch must be one shape
            out = []
        shape = img.shape
        out.append((int(k), img))
        n += 1
        if len(out) >= batch:
            yield out
            out = []
        if limit and n >= limit:
            break
    if out:
        yield out


def iter_depth(rec, limit=None):
    """Frame-at-a-time view of iter_depth_batches, for callers that want single frames."""
    for b in iter_depth_batches(rec, 1, limit):
        for item in b:
            yield item


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rrd", required=True)
    ap.add_argument("--backend", default="numpy", choices=("numpy", "torch"))
    ap.add_argument("--device", default=None, help="torch device override (cuda / cpu)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N depth frames (0 = all)")
    ap.add_argument("--batch", type=int, default=64,
                    help="frames per batch; the torch backend reduces a whole batch per launch")
    ap.add_argument("--json", default=None, help="write the scorecard here (for the auto-tune loop)")
    ap.add_argument("--compare-legacy", action="store_true",
                    help="also replay the PRE-FIX command, demonstrating the sign bug on data")
    ap.add_argument("--check-parity", action="store_true",
                    help="ALSO run the numpy reference on every batch and compare. An "
                         "accelerated backend is only trustworthy if every gap DECISION agrees "
                         "with the reference; any disagreement exits 2.")
    a = ap.parse_args()

    cfg = dict(DEFAULTS)
    be = make_backend(a.backend, a.device)
    ref = NumpyBackend() if (a.check_parity and be.name != "numpy") else None

    T = {}

    def lap(key, t0):
        T[key] = T.get(key, 0.0) + (time.perf_counter() - t0)

    t0 = time.perf_counter()
    rec = load_recording(a.rrd)
    lap("open", t0)
    t0 = time.perf_counter()
    scal = load_scalars(rec, ["/reflex/clearance_m", "/follow/bearing", "/cmd/vyaw",
                              "/reflex/vx_cap", "/fsm/state_id", "/head/yaw_deg"])
    lap("scalars", t0)
    print("[load] opened in %.1fs; scalars for %d frames in %.1fs"
          % (T["open"], len(scal), T["scalars"]))

    bar = cfg["gap_horizon_m"] if cfg["gap_horizon_m"] > 0 else cfg["obstacle_brake_start"]
    trig = cfg["obstacle_brake_stop"] + (bar - cfg["obstacle_brake_stop"]) * cfg["gap_steer_trigger_frac"]
    print("[cfg] horizon %.3fm -> engage %.3fm | need_w %.3fm | deadband %.0fdeg | "
          "detour %.1f/%.1fdeg | critical %.2fm"
          % (bar, trig, need_width(cfg), cfg["gap_steer_deadband_deg"],
             cfg["gap_max_detour_deg"], cfg["gap_critical_detour_deg"], cfg["gap_critical_m"]))

    nb = int(cfg["gap_profile_bins"])
    min_px = max(4, int(cfg["obstacle_min_valid"] // max(1, nb // 8)))

    rows, legacy_rows, reasons = [], [], {}
    skips, cliff_deltas = {}, []
    par = {"clr_max": 0.0, "clr_nan_mismatch": 0, "scored_set_diff": 0,
           "bin_max": 0.0, "bin_nan_mismatch": 0, "decisions": 0, "decision_diff": 0}
    nf = nskip = 0

    t_loop = time.perf_counter()
    for batch in iter_depth_batches(rec, max(1, a.batch), a.limit or None, T):
        ks = [k for k, _img in batch]
        h, w = batch[0][1].shape
        nf += len(batch)
        f = focal_px(w, cfg["hfov_deg"])
        cx = w * 0.5
        y0, y1 = int(h * cfg["obstacle_band_top"]), int(h * cfg["obstacle_band_bot"])
        if y1 - y0 < 4:
            continue
        bands = np.ascontiguousarray(np.stack([img[y0:y1, :] for _k, img in batch]),
                                     dtype=np.float32)

        t0 = time.perf_counter()
        geo = be.batch_corridor(bands, cfg, f, cx)
        lap("corridor", t0)
        rgeo = None
        if ref is not None:
            rgeo = ref.batch_corridor(bands, cfg, f, cx)
            both = np.isfinite(geo) & np.isfinite(rgeo)
            if both.any():
                par["clr_max"] = max(par["clr_max"],
                                     float(np.max(np.abs(geo[both] - rgeo[both]))))
            par["clr_nan_mismatch"] += int(np.sum(np.isfinite(geo) != np.isfinite(rgeo)))

        todo = []
        for i, k in enumerate(ks):
            s_ = scal.get(k, {})
            bearing = s_.get("/follow/bearing", 0.0)
            logged = s_.get("/reflex/clearance_m")
            # Prefer the recomputed geometry, fall back to the logged scalar, and itemise every
            # skip: "no clearance available" and "the path was clear" are opposite findings, and
            # collapsing them makes a broken replay look like a clean run.
            clearance = float(geo[i]) if np.isfinite(geo[i]) else logged
            if clearance is None:
                skips["no_clearance"] = skips.get("no_clearance", 0) + 1
                nskip += 1
                continue
            if logged is not None:
                cliff_deltas.append(abs(clearance - logged))
            if rgeo is not None:
                rc = float(rgeo[i]) if np.isfinite(rgeo[i]) else logged
                if rc is not None and ((rc < trig) != (clearance < trig)):
                    par["scored_set_diff"] += 1
            if clearance >= trig:
                skips["path_clear"] = skips.get("path_clear", 0) + 1
                nskip += 1
                continue
            todo.append((i, k, clearance, bearing))
        if not todo:
            continue

        sel = bands[[item[0] for item in todo]]
        t0 = time.perf_counter()
        bins = be.batch_bins(sel, nb, cfg["obstacle_pctile"], min_px, cfg["obstacle_max_m"])
        lap("bins", t0)
        rbins = None
        if ref is not None:
            rbins = ref.batch_bins(sel, nb, cfg["obstacle_pctile"], min_px, cfg["obstacle_max_m"])
            both = np.isfinite(bins) & np.isfinite(rbins)
            if both.any():
                par["bin_max"] = max(par["bin_max"],
                                     float(np.max(np.abs(bins[both] - rbins[both]))))
            par["bin_nan_mismatch"] += int(np.sum(np.isfinite(bins) != np.isfinite(rbins)))

        for j, (i, k, clearance, bearing) in enumerate(todo):
            critical = cfg["gap_critical_m"] > 0 and clearance <= cfg["gap_critical_m"]
            cap = (max(cfg["gap_max_detour_deg"], cfg["gap_critical_detour_deg"]) if critical
                   else cfg["gap_max_detour_deg"])
            gap, reason = gap_profile(bins[j], cx, f, w, cfg, bearing, cap)
            reasons[reason] = reasons.get(reason, 0) + 1
            cmd, why = policy_current(gap, bearing, clearance, cfg)
            if rbins is not None:
                rgap, rreason = gap_profile(rbins[j], cx, f, w, cfg, bearing, cap)
                rcmd, rwhy = policy_current(rgap, bearing, clearance, cfg)
                par["decisions"] += 1
                if rreason != reason or rwhy != why or abs(rcmd - cmd) > 1e-6:
                    par["decision_diff"] += 1
            rows.append({"frame": k, "clearance": clearance, "bearing_deg": math.degrees(bearing),
                         "gap_deg": math.degrees(gap[0]) if gap else None,
                         "cmd": cmd, "why": why, "critical": critical,
                         "rate": cfg["gap_steer_rate"]})
            if a.compare_legacy:
                lc, lw = policy_legacy(gap, bearing, clearance, cfg)
                legacy_rows.append({"gap_deg": math.degrees(gap[0]) if gap else None,
                                    "cmd": lc, "why": lw, "rate": cfg["gap_steer_rate"]})
    lap("loop", t_loop)

    comp = T.get("corridor", 0.0) + T.get("bins", 0.0)
    print("[replay] %d depth frames, %d skipped, %d scored  (backend %s, batch %d)"
          % (nf, nskip, len(rows), be.name, a.batch))
    print("    time: open %.1fs | scalars %.1fs | columns %.1fs | loop %.2fs "
          "[decode %.2fs, corridor %.2fs, bins %.2fs]"
          % (T.get("open", 0.0), T.get("scalars", 0.0), T.get("columns", 0.0), T.get("loop", 0.0),
             T.get("decode", 0.0), T.get("corridor", 0.0), T.get("bins", 0.0)))
    if nf:
        print("    geometry compute %.2f ms/frame | whole loop %.2f ms/frame"
              % (1000.0 * comp / nf, 1000.0 * T.get("loop", 0.0) / nf))
    for r_, c_ in sorted(skips.items(), key=lambda kv: -kv[1]):
        print("    skip: %-16s %d" % (r_, c_))
    if cliff_deltas:
        cd = np.array(cliff_deltas)
        print("    recomputed vs LOGGED clearance: n=%d mean|d|=%.3fm p90=%.3fm max=%.3fm"
              % (len(cd), cd.mean(), np.percentile(cd, 90), cd.max()))
        print("      (large disagreement = the logged corridor and the depth geometry "
              "diverge -- the clearance-cliff suspect)")

    parity_ok = True
    if ref is not None:
        parity_ok = (par["decision_diff"] == 0 and par["scored_set_diff"] == 0
                     and par["clr_nan_mismatch"] == 0 and par["bin_nan_mismatch"] == 0)
        print("\n=== PARITY vs numpy reference: %s ===" % ("PASS" if parity_ok else "FAIL"))
        print("  corridor   max|d| %.2e m   NaN mismatches %d"
              % (par["clr_max"], par["clr_nan_mismatch"]))
        print("  bins       max|d| %.2e m   NaN mismatches %d"
              % (par["bin_max"], par["bin_nan_mismatch"]))
        print("  frames scored differently   %d" % par["scored_set_diff"])
        print("  gap decisions compared %d, disagreements %d"
              % (par["decisions"], par["decision_diff"]))

    print("\n=== profile outcome ===")
    for r_, c_ in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  %-18s %d" % (r_, c_))

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
                       "device": getattr(be, "device", "cpu"),
                       "frames": nf, "scored": len(rows), "steers": len(steer),
                       "reasons": reasons, "skips": skips,
                       "invariants": [{"name": n, "good": g, "total": t, "rule": r}
                                      for n, g, t, r in inv],
                       "timing_s": {k: round(v, 4) for k, v in T.items()},
                       "parity": par if ref is not None else None,
                       "config": cfg}, fh, indent=2)
        print("\nwrote %s" % a.json)

    if not ok_all:
        return 1
    if not parity_ok:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
