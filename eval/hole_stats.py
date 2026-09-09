#!/usr/bin/env python3
"""Measure what a DEPTH-HOLE rule WOULD HAVE DONE on the archived runs. Measures only; changes
nothing, imports nothing from robot/, and is not imported by any robot code.

WHY THIS EXISTS. _corridor_clearance scores a frame "clear" whenever the corridor holds no NEAR
blob. A couch that gives the brake NO USABLE NEAR EVIDENCE -- dark fabric, oblique surface, beyond
the stereo's usable range -- therefore produces the same empty corridor as an open room, and
3a91550's sparse-frame guard does not catch it: measured over the whole archive, 1062
OBSTACLE-NODATA lines across 8194.7 s of runtime, and every single one resolved "window empty,
clear". Not one ever said SENSOR BLIND. The fail-open hole is real and the existing guard has never
once fired on it.

Before writing a fix we have to know the COST of the candidate rule, because the obvious rule --
"corridor mostly invalid => obstacle" -- is a false-brake generator: an ordinary open room is
mostly beyond obstacle_max_m too, so a large share of perfectly safe frames look identical to a
couch by invalid-fraction alone. This tool puts real numbers on both sides from recorded depth.

TWO DEFINITIONS OF "HOLE", MEASURED SIDE BY SIDE AND NEVER CONFLATED
  strict   -- a true NO RETURN: ~isfinite(band) | (band <= --hole-max-m).
              This is the honest definition of a hole, and it is reported FIRST even though the
              archive shows it is nearly empty (measured 0.001%-1.0% of a frame across runs from
              2026-07 to 2026-09): this depth sensor essentially always emits a range, including
              garbage out to 65.5 m. A fix written against "no return" would therefore never fire.
  unusable -- what the BRAKE actually cannot use: strict | (band >= --obstacle-max-m).
              A couch that reads 30 m because its fabric defeated the stereo is exactly as
              invisible to the brake as one that reads nothing, so this is the set with the real
              mass -- and it is also the set that swallows every open doorway and far wall. That
              confound is the whole problem, which is why contiguity and the ring exist below.
DELIBERATELY NOT REUSED: _nodata's own validity mask. It is `isfinite & >0.15 & <obstacle_max_m`,
whose complement mixes the two definitions above into one number, and that number is precisely
what has been hiding the couch. The tool separates them so the choice is made on evidence.

THE OTHER TRAP: THE HOLE MASK IS COMPUTED IN IMAGE SPACE, ALWAYS. A hole has no range, and every
corridor test in the footprint path is range-dependent: lat = |band * u| is 0 for a hole, so
lat <= half passes for EVERY hole pixel and the corridor silently degenerates to the whole frame.
Any hole measurement projected through depth is measuring nothing. The corridor here is therefore
the fixed image rectangle of the 'frac' path (obstacle_band_top/bot x obstacle_corridor_frac) --
the one corridor that is defined without a range.

WHAT IT REPORTS, per hole definition
  1. Baseline risk: per-frame hole fraction of the corridor, percentiles, and the fire rate of the
     naive "fraction > X" rule, split THREE ways -- runs that logged OBSTACLE-NODATA (the fail-open
     path), runs whose brake demonstrably ran and met near obstacles without ever taking that path
     (the false-brake control group), and runs with no OBSTACLE-*/CLEARANCE line at all (ordinary
     scenes, but no evidence about the brake, so they are never pooled into the control group).
  2. Contiguity: size of the largest CONNECTED hole blob (scipy.ndimage.label), because a couch is
     one region and dead pixels are not.
  3. Context: depth statistics of the USABLE pixels in a dilated RING around each blob. This is the
     discriminator. A couch is a hole EMBEDDED IN NEAR GEOMETRY (ring median low); an open doorway
     or a far wall has a far ring, or no usable ring at all.
  4. A (blob size x ring median) joint table and a candidate-rule sweep over whole FRAMES, so
     thresholds can be chosen from numbers instead of guessed.
  5. A per-run breakdown to spot which runs are the couch ones.

KNOWN LIMITS, stated so no number is over-read:
  * HEAD-PAN IS NOT CORRECTED. The pre-2026-09 archive carries no head-yaw entity (only
    /camera/depth, /camera/rgb, /odom/*, /fsm/*, /health/*, /diag/*). Recordings made after the
    head-tracking feature (--head-track, ~2026-09-03) log /head/yaw_deg and /head/pitch_deg, but
    this tool does not yet read them. The corridor here is therefore HEAD-forward where the live
    brake shifts it to BODY-forward by -head_yaw*focal px. Frames recorded mid-pan sample a
    window offset from the one the robot actually judged.
  * Depth is logged to .rrd at 1-in-5 frames, so a run contributes tens of frames, not thousands.
    These are samples of the scenes, not of every braking decision.
  * THE ARCHIVED config/ IS NOT PROVABLY THE CONFIG THAT RAN. manifest.json names profiles
    ('tracker-drive') the folder does not contain, one run archives obstacle_brake:false while
    logging 216 OBSTACLE-NODATA lines, and the CLI overrides both. So this tool measures ONE image
    rectangle for every run -- which is what makes the percentiles poolable -- and reports archived
    corridor settings as ADVISORY only. Anything that had to be true (did the brake run? did it see
    near obstacles?) is taken from the run's own log, never from its config.

Usage:
  python eval/hole_stats.py --runs runs --limit 4
  python eval/hole_stats.py --runs runs --only-nodata --json /tmp/holes.json
"""
import argparse
import glob
import json
import os
import re
import sys

import numpy as np

# rerun's Recording API is imported lazily inside depth_frames so --help works without rerun.
# rerun ChannelDatatype ids. The float entries matter: this archive logs depth as F32 (34) in
# METRES. eval/selfmask_from_runs.py has a DTYPES table but its IDs are wrong (signed types
# mapped to unsigned, float IDs offset by ~26 from the actual ChannelDatatype enum), so the
# archive's F32 id=34 misses the table and falls through to the .get() default of float32 --
# correct by accident. Named here so a future integer-encoded recording is decoded because it
# was handled, not because the fallback happened to be right.
DTYPES = {1: np.int8, 2: np.int16, 3: np.int32, 4: np.int64,
          6: np.uint8, 7: np.uint16, 8: np.uint32, 9: np.uint64,
          33: np.float16, 34: np.float32, 35: np.float64}


def depth_frames(path, stride=1, limit=0, stats=None):
    """Yield (h, w) float32 depth-in-metres arrays from one .rrd. Never raises on a bad chunk.

    Same reader as selfmask_from_runs.depth_frames -- deliberately duplicated rather than imported,
    because that module is a build tool the operator edits and a measurement instrument must not
    break when it changes.

    `stats`, a dict updated in place, separates "this recording holds no depth at all" from "it
    holds depth that would not decode". Both end at zero frames, and reporting them the same way
    would file a LOST MEASUREMENT as nothing to measure -- the archive really does contain
    odometry-only recordings (20260907T083018Z_c68f3ad has no /camera/depth entity), so this
    distinction is the difference between an honest skip line and a misleading one.
    """
    import warnings
    warnings.filterwarnings("ignore")
    from rerun.recording import load_recording
    st = stats if stats is not None else {}
    for k in ("depth_chunks", "chunk_errors", "short_rows"):
        st.setdefault(k, 0)
    rec = load_recording(path)          # caller owns the failure: an unreadable run is SKIPPED loud
    seen = 0
    kept = 0
    for ch in rec.chunks():
        try:
            if "depth" not in str(ch.entity_path).lower():
                continue
            st["depth_chunks"] += 1
            rb = ch.to_record_batch()
            if "DepthImage:buffer" not in rb.schema.names:
                continue
            buf = rb.column("DepthImage:buffer")
            fmt = rb.column("DepthImage:format")
            mtr = rb.column("DepthImage:meter") if "DepthImage:meter" in rb.schema.names else None
            for i in range(rb.num_rows):
                fl = fmt[i].as_py()
                raw = buf[i].as_py()
                if not fl or not raw:
                    continue
                seen += 1
                if stride > 1 and (seen - 1) % stride:
                    continue
                f = fl[0]
                dt = DTYPES.get(f["channel_datatype"], np.float32)
                a = np.frombuffer(bytes(raw[0]), dtype=np.uint8).view(dt).astype(np.float32)
                need = f["height"] * f["width"]
                if a.size < need:
                    st["short_rows"] += 1
                    continue
                a = a[:need].reshape(f["height"], f["width"])
                if dt not in (np.float16, np.float32, np.float64):
                    m = (mtr[i].as_py() or [1000.0]) if mtr is not None else [1000.0]
                    a = a / (m[0] if m and m[0] else 1000.0)   # integer encodings are scaled units
                yield a
                kept += 1
                if limit and kept >= limit:
                    return
        except Exception:  # noqa: BLE001 -- one bad chunk must not cost the rest of the recording
            st["chunk_errors"] += 1
            continue


def cfg_scalars(path, keys):
    """Pull flat `key: value` settings out of an archived defaults.yaml. Returns {} if absent.

    A handful of settings does not justify a yaml dependency, and the archived defaults.yaml is
    flat. Numbers come back as floats and everything else as the bare token, so an unparseable
    value reads as "unknown" rather than as a wrong number silently substituted for the real one.
    """
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*['\"]?([^'\"#\s]+)['\"]?\s*(?:#.*)?$",
                             line)
                if not m or m.group(1) not in keys:
                    continue
                try:
                    out[m.group(1)] = float(m.group(2))
                except ValueError:
                    out[m.group(1)] = m.group(2)
    except OSError:
        return {}
    return out


def corridor_rect(h, w, band_top, band_bot, frac):
    """The 'frac' path's image rectangle, replicated exactly from _corridor_clearance_uncached.

    Head shift is 0 here (see the module docstring): the recordings carry no head yaw, so this is
    the head-forward window, not the body-forward one the live brake uses.
    """
    cf = max(0.05, min(1.0, frac))
    y0 = int(h * band_top)
    y1 = int(h * band_bot)
    x0 = int(max(0, min(w - 2, w * (0.5 - cf / 2.0))))
    x1 = int(max(x0 + 1, min(w, w * (0.5 + cf / 2.0))))
    return y0, max(y0 + 1, y1), x0, x1


MODES = ("strict", "unusable")


def hole_masks(band, hole_max, obstacle_max):
    """The two hole definitions, as boolean masks over the corridor. See the module docstring."""
    strict = ~np.isfinite(band) | (band <= hole_max)
    return {"strict": strict, "unusable": strict | (band >= obstacle_max)}


def blob_stats(mask, band, dilate, min_blob, max_blobs, ndimage, y0, x0):
    """Connected components of `mask`, each with the depth stats of a dilated RING around it.

    The ring keeps only pixels OUTSIDE the mask -- i.e. pixels the brake could actually have used
    at that definition of usable. Other holes swept in by the dilation are excluded, so a ring
    never reports the depth of a neighbouring hole.
    """
    lab, n = ndimage.label(mask)
    if n <= 0:
        return 0, []
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    largest = int(sizes.max())
    keep = np.flatnonzero(sizes >= max(1, min_blob))
    if keep.size == 0:
        return largest, []
    usable = ~mask
    out = []
    # Bounded, largest first -- mirrors _nearest_blob's 16-blob cap. Many blobs means speckle, and
    # speckle is what the contiguity test exists to reject, so there is nothing to learn past the
    # big ones.
    for li in keep[np.argsort(sizes[keep])[::-1][:max_blobs]]:
        m = (lab == li)
        ring = ndimage.binary_dilation(m, iterations=int(dilate)) & (~m) & usable
        rv = band[ring]
        rv = rv[np.isfinite(rv)]
        ys, xs = np.nonzero(m)
        out.append({
            "px": int(sizes[li]),
            "row": float(ys.mean()) + y0,          # absolute image row: a couch sits LOW in frame
            "col": float(xs.mean()) + x0,
            "ring_px": int(rv.size),
            "ring_med": float(np.median(rv)) if rv.size else None,
            "ring_p10": float(np.percentile(rv, 10)) if rv.size else None,
        })
    return largest, out


def analyse_frame(d, rect, args, ndimage):
    """One frame -> per-mode hole fraction, largest blob and ring-annotated blobs. Pure measurement."""
    y0, y1, x0, x1 = rect
    band = d[y0:y1, x0:x1]
    masks = hole_masks(band, args.hole_max_m, args.obstacle_max_m)

    # The same count _nodata prints as frame_valid=, so these numbers tie back to the archived log
    # lines. It uses the WHOLE decoded frame because 889 of the archive's 1062 NODATA lines say
    # 'footprint', and on that path band IS the whole frame. (Which also settles the config
    # question: every one of those runs archives corridor_mode 'frac', so the CLI overrode it --
    # the log is ground truth, the archived yaml is not.)
    fv = int(np.count_nonzero(np.isfinite(d) & (d > args.hole_max_m) & (d < args.obstacle_max_m)))
    res = {
        "frame_valid": fv,
        "frame_valid_frac": float(fv) / float(d.size),
        # 4.0 * obstacle_min_valid (70) and the 2% floor, both as shipped in _nodata.
        "nodata_blind": bool(fv < max(0.02 * float(d.size), 4.0 * args.obstacle_min_valid)),
    }
    # THE SAME TEST ON THE 'frac' BAND. The archive's NODATA lines split 889 footprint / 173 frac,
    # and on the frac path `band` is the corridor, not the whole frame -- so the 2% floor is 2% of a
    # window ~4x smaller and the guard is correspondingly easier to trip. Measuring only the
    # whole-frame form would let the cross-check below speak for the 889 and silently ignore the 173.
    # The usable set is the exact complement of the 'unusable' hole mask, so this costs one count.
    cv = band.size - int(np.count_nonzero(masks["unusable"]))
    res["corridor_valid"] = cv
    res["nodata_blind_frac"] = bool(cv < max(0.02 * float(band.size),
                                             4.0 * args.obstacle_min_valid))
    for k in MODES:
        largest, blobs = blob_stats(masks[k], band, args.dilate_px, args.min_blob_px,
                                    args.max_blobs, ndimage, y0, x0)
        res[k] = {"hole_frac": float(masks[k].mean()), "largest": largest, "blobs": blobs}
    return res


def pct(a, qs):
    if not len(a):
        return {str(q): None for q in qs}
    arr = np.asarray(a, dtype=np.float64)
    return {str(q): float(np.percentile(arr, q)) for q in qs}


# ------------------------------------------------------------------ reporting

SZ_BUCKETS = ((100, 400), (400, 1600), (1600, 6400), (6400, 10 ** 9))
RING_BUCKETS = (("none", None, None), ("<0.80", 0.0, 0.80), ("0.80-1.15", 0.80, 1.15),
                ("1.15-2.0", 1.15, 2.0), ("2.0-3.5", 2.0, 3.5), (">=3.5", 3.5, 1e9))
FRAC_GRID = (0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 0.90)
RING_GRID = (None, 3.50, 2.00, 1.15, 0.80)


def report_mode(mode, groups, corridor_px, args):
    """Print sections [1]-[4] for one hole definition."""
    print("\n" + "=" * 78)
    print("HOLE DEFINITION: %s  (%s)" % (
        mode.upper(),
        "~isfinite | <= %.2f m -- a TRUE no-return" % args.hole_max_m if mode == "strict"
        else "no-return OR >= %.2f m -- everything the brake cannot use" % args.obstacle_max_m))

    print("\n[1] BASELINE -- per-frame HOLE FRACTION of the image-space corridor")
    for label, rs in groups:
        vals = [v for r in rs for v in r[mode]["hole_frac"]]
        if not vals:
            print("   %-16s (no frames)" % label)
            continue
        p = pct(vals, (5, 25, 50, 75, 90, 99))
        print("   %-16s n=%-5d  p5 %.1f%%  p25 %.1f%%  p50 %.1f%%  p75 %.1f%%  p90 %.1f%%  "
              "p99 %.1f%%" % (label, len(vals), 100 * p["5"], 100 * p["25"], 100 * p["50"],
                              100 * p["75"], 100 * p["90"], 100 * p["99"]))
    print("   naive rule 'hole_frac > X' -- share of FRAMES it would call blocked:")
    print("      %-7s %s" % ("X", "".join("%-16s" % g[0] for g in groups)))
    for x in FRAC_GRID:
        cells = []
        for _label, rs in groups:
            vals = [v for r in rs for v in r[mode]["hole_frac"]]
            cells.append("%-16s" % (("%.1f%%" % (100.0 * sum(1 for v in vals if v > x) / len(vals)))
                                    if vals else "-"))
        print("      %-7.2f %s" % (x, "".join(cells)))
    print("   THE RISK IS THE RIGHT-HAND COLUMN: a rule that fires on ordinary frames from runs "
          "that never had an obstacle problem is a false-brake generator, however well it catches "
          "the couch.")

    print("\n[2] CONTIGUITY -- largest CONNECTED hole blob in the corridor (px of %d)" % corridor_px)
    for label, rs in groups:
        vals = [v for r in rs for v in r[mode]["largest"]]
        if not vals:
            print("   %-16s (no frames)" % label)
            continue
        p = pct(vals, (50, 75, 90, 99))
        print("   %-16s p50 %6d  p75 %6d  p90 %6d  p99 %6d  max %6d   (p50 = %.1f%% of corridor)"
              % (label, int(p["50"]), int(p["75"]), int(p["90"]), int(p["99"]), int(max(vals)),
                 100.0 * p["50"] / max(1, corridor_px)))

    print("\n[3] CONTEXT -- joint (blob size px) x (ring median depth m), blobs >= %d px"
          % args.min_blob_px)
    print("    'none' = the dilated ring held no usable pixel at all: the hole has no context, "
          "which is itself a signal -- a hole with no near edge is not a near object.")
    for label, rs in groups:
        bl = [(b["px"], b["ring_med"]) for r in rs for b in r[mode]["blobs"]]
        print("\n   %s -- %d blobs" % (label, len(bl)))
        print("      %-14s %s" % ("blob px", "".join("%-11s" % g[0] for g in RING_BUCKETS)))
        for lo, hi in SZ_BUCKETS:
            if hi <= args.min_blob_px:
                continue
            lo = max(lo, args.min_blob_px)
            row = []
            for _n, rlo, rhi in RING_BUCKETS:
                if rlo is None:
                    c = sum(1 for p_, rm in bl if lo <= p_ < hi and rm is None)
                else:
                    c = sum(1 for p_, rm in bl if lo <= p_ < hi and rm is not None
                            and rlo <= rm < rhi)
                row.append("%-11d" % c)
            print("      %-14s %s" % ("%d-%s" % (lo, "inf" if hi >= 10 ** 9 else hi), "".join(row)))

    print("\n[4] RULE SWEEP -- share of FRAMES holding a blob with (px >= B) and (ring med <= R)")
    print("    Shippable only if the right-hand group stays near zero at a B/R that still catches "
          "the couch runs in [5].")
    for label, rs in groups:
        nfr = sum(r["frames"] for r in rs)
        if not nfr:
            continue
        print("\n   %s (%d frames)" % (label, nfr))
        print("      %-8s %s" % ("B \\ R", "".join("%-9s" % ("any" if R is None else "%.2f" % R)
                                                  for R in RING_GRID)))
        for B in (args.min_blob_px, 200, 400, 800, 1600, 3200, 6400, 12800):
            if B < args.min_blob_px:
                continue
            cells = []
            for R in RING_GRID:
                hits = set()
                for r in rs:
                    for b in r[mode]["blobs"]:
                        if b["px"] < B:
                            continue
                        if R is None or (b["ring_med"] is not None and b["ring_med"] <= R):
                            hits.add((r["run"], b["frame"]))
                cells.append("%-9s" % ("%.1f%%" % (100.0 * len(hits) / nfr)))
            print("      %-8d %s" % (B, "".join(cells)))


def main():
    ap = argparse.ArgumentParser(
        description="Measure candidate depth-HOLE rules against the archived runs. Read-only.")
    ap.add_argument("--runs", default="runs", help="directory of <timestamp>_<commit>/ run dirs")
    ap.add_argument("--limit", type=int, default=0, help="analyse at most N run dirs (newest first)")
    ap.add_argument("--match", default="",
                    help="comma-separated substrings; keep a run whose dir name contains any of "
                         "them. THE CORPUS IS ~15 GB, so a full sweep is not a thing you do while "
                         "iterating -- and --limit alone takes the NEWEST runs, which cannot reach "
                         "the older control group. Name the runs to compare instead")
    ap.add_argument("--only-nodata", action="store_true",
                    help="only runs whose k1_follow.err logged OBSTACLE-NODATA. Leaving this OFF "
                         "is usually right: the runs that braked WITHOUT ever taking that path are "
                         "the false-brake control group, and without them a rule cannot be judged")
    ap.add_argument("--stride", type=int, default=1,
                    help="decode every Nth depth frame. Depth is already logged to .rrd at 1-in-5, "
                         "so a run holds only tens of frames and 1 is normally correct")
    ap.add_argument("--max-frames", type=int, default=0, help="cap frames per run (0 = all)")
    ap.add_argument("--json", default="", help="also write the full per-run/per-blob record here")

    ap.add_argument("--hole-max-m", type=float, default=0.15,
                    help="a pixel at or below this (or non-finite) is NO RETURN. 0.15 is the same "
                         "floor _corridor_clearance uses to reject its own dead band")
    ap.add_argument("--obstacle-max-m", type=float, default=5.0,
                    help="obstacle_max_m. Defines the 'unusable' hole set and reproduces _nodata's "
                         "frame_valid= for cross-checking against the archived log lines")
    ap.add_argument("--obstacle-min-valid", type=float, default=70.0,
                    help="obstacle_min_valid, only to reproduce _nodata's SENSOR-BLIND threshold")
    ap.add_argument("--corridor-frac", type=float, default=0.55, help="obstacle_corridor_frac")
    ap.add_argument("--band-top", type=float, default=0.30, help="obstacle_band_top")
    ap.add_argument("--band-bot", type=float, default=0.75, help="obstacle_band_bot")
    ap.add_argument("--dilate-px", type=int, default=6,
                    help="ring width in pixels around a blob. Too thin and the ring lands on the "
                         "object's own soft edge; too thick and it reaches past the object")
    ap.add_argument("--min-blob-px", type=int, default=100,
                    help="smallest hole blob kept for ring analysis. Sets the floor of the rule "
                         "sweep -- the sweep is only exact for sizes at or above this")
    ap.add_argument("--max-blobs", type=int, default=16, help="blobs examined per frame, largest first")
    ap.add_argument("--ref-blob-px", type=int, default=800,
                    help="reference candidate rule, per-run column: blob at least this big ...")
    ap.add_argument("--ref-ring-m", type=float, default=1.15,
                    help="... with a ring median at or below this (default = obstacle_brake_start)")
    a = ap.parse_args()

    try:
        from scipy import ndimage
    except Exception as e:  # noqa: BLE001
        # REFUSE, do not degrade. Contiguity IS the measurement; a bare pixel count without
        # labelling answers a different question while looking like an answer to this one.
        print("REFUSING: scipy.ndimage is required (contiguity is the whole point): %s" % e)
        return 2

    dirs = sorted((d for d in glob.glob(os.path.join(a.runs, "*")) if os.path.isdir(d)),
                  reverse=True)
    if not dirs:
        print("no run dirs under %s" % a.runs)
        return 2

    CFG_KEYS = ("obstacle_corridor_frac", "obstacle_band_top", "obstacle_band_bot",
                "obstacle_max_m", "corridor_mode")
    # Lines that PROVE the corridor code ran and saw something near. Used instead of the archived
    # obstacle_brake setting, which is not the value that ran: manifest.json names profiles
    # ('tracker-drive') that the run's own config/ folder does not even contain, and one run with
    # 216 OBSTACLE-NODATA lines archives obstacle_brake:false. The log is the only ground truth.
    BRAKE_EV = re.compile(r"^(CLEARANCE|OBSTACLE-|GAP-STEER|GROUND-REJECT|LOCALMAP)")
    want = [s for s in (x.strip() for x in a.match.split(",")) if s]
    chosen = []
    for d in dirs:
        if want and not any(s in os.path.basename(d) for s in want):
            continue
        err = os.path.join(d, "k1_follow.err")
        nd = ev = 0
        if os.path.exists(err):
            try:
                with open(err, "r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        nd += "OBSTACLE-NODATA" in ln
                        ev += bool(BRAKE_EV.match(ln))
            except OSError:
                nd = ev = 0
        if a.only_nodata and nd == 0:
            continue
        if not glob.glob(os.path.join(d, "*.rrd")):
            continue
        chosen.append((d, nd, ev))
        if a.limit and len(chosen) >= a.limit:
            break
    if not chosen:
        print("no runs matched (--only-nodata=%s, --match=%r) under %s"
              % (a.only_nodata, a.match, a.runs))
        return 2

    print("hole_stats: %d runs, stride %d, %s frames/run, corridor frac=%.2f rows %.2f-%.2f, "
          "no-return<=%.2fm, unusable>=%.2fm, ring dilate %dpx"
          % (len(chosen), a.stride, a.max_frames or "all", a.corridor_frac, a.band_top,
             a.band_bot, a.hole_max_m, a.obstacle_max_m, a.dilate_px))
    print("NOTE head-pan is NOT corrected offline (pre-2026-09 archive has no head-yaw; newer "
          "recordings log /head/yaw_deg but this tool does not yet read it): this is the "
          "HEAD-forward corridor, not the body-forward one the live brake uses.\n")

    runs, skipped, rect = [], [], None
    for idx, (d, ndlines, brake_ev) in enumerate(chosen, 1):
        name = os.path.basename(d)
        dur, prof = None, "?"
        try:
            with open(os.path.join(d, "manifest.json"), "r", encoding="utf-8") as fh:
                mf = json.load(fh)
            dur = float(mf.get("duration_s") or 0.0)
            prof = str(mf.get("profile") or "?")
        except Exception:  # noqa: BLE001 -- a missing manifest costs a column, not the run
            pass
        cfg = cfg_scalars(os.path.join(d, "config", "defaults.yaml"), CFG_KEYS)
        mism = []
        for k, v in (("obstacle_corridor_frac", a.corridor_frac),
                     ("obstacle_band_top", a.band_top), ("obstacle_band_bot", a.band_bot),
                     ("obstacle_max_m", a.obstacle_max_m)):
            if isinstance(cfg.get(k), float) and abs(cfg[k] - v) > 1e-6:
                mism.append("%s=%g (measuring %g)" % (k, cfg[k], v))
        if not cfg:
            # Never silent: an unknown archived geometry is reported, so nobody later attributes
            # this run's percentiles to a corridor it was never measured against.
            mism.append("archived config UNREADABLE - geometry unverified")

        rrds = sorted(glob.glob(os.path.join(d, "*.rrd")))
        rrd = rrds[0]
        print("[%2d/%d] %s  nodata=%d brake_ev=%d  dur=%s  profile=%s  archived_mode=%s"
              % (idx, len(chosen), name, ndlines, brake_ev, ("%.0fs" % dur) if dur else "?",
                 prof, cfg.get("corridor_mode", "?")))
        if len(rrds) > 1:
            # Every run in the archive today holds exactly one recording, so the extra files are
            # unreachable rather than merely unused -- and a silently dropped recording is a silently
            # smaller sample. Say it rather than let the frame count look like the whole run.
            print("        NOTE %d .rrd files here; only %s is read"
                  % (len(rrds), os.path.basename(rrd)))
        if mism:
            # ADVISORY, not authoritative. The archived config/ folder is NOT provably the config
            # that ran -- manifest.json names profiles ('tracker-drive') that the folder does not
            # contain, and the CLI overrides both. Treat a mismatch as "this run may have used a
            # different corridor", never as "it did".
            print("        NOTE archived defaults differ (advisory, profile+CLI not recoverable): "
                  "%s" % "; ".join(mism))

        acc = {k: {"hole_frac": [], "largest": [], "blobs": []} for k in MODES}
        fvf, blind_n, blind_fr_n, nfr = [], 0, 0, 0
        dstat = {}
        try:
            for f in depth_frames(rrd, stride=max(1, a.stride), limit=a.max_frames, stats=dstat):
                if rect is None or rect[4:] != f.shape:
                    r = corridor_rect(f.shape[0], f.shape[1], a.band_top, a.band_bot,
                                      a.corridor_frac)
                    rect = (r[0], r[1], r[2], r[3], f.shape[0], f.shape[1])
                    print("        corridor rows %d..%d cols %d..%d of %dx%d = %d px"
                          % (r[0], r[1], r[2], r[3], f.shape[0], f.shape[1],
                             (r[1] - r[0]) * (r[3] - r[2])))
                res = analyse_frame(f, rect[:4], a, ndimage)
                fvf.append(res["frame_valid_frac"])
                blind_n += int(res["nodata_blind"])
                blind_fr_n += int(res["nodata_blind_frac"])
                for k in MODES:
                    acc[k]["hole_frac"].append(res[k]["hole_frac"])
                    acc[k]["largest"].append(res[k]["largest"])
                    for b in res[k]["blobs"]:
                        b["frame"] = nfr           # ties a blob to its frame: the sweep counts
                        acc[k]["blobs"].append(b)  # FRAMES, not blobs, so it cannot double-count
                nfr += 1
        except Exception as e:  # noqa: BLE001 -- one bad recording must never stop the sweep
            skipped.append((name, "%s: %s" % (type(e).__name__, str(e)[:90])))
            print("        ! SKIPPED (%s: %s)" % (type(e).__name__, str(e)[:90]))
            continue
        if nfr == 0:
            # Say WHICH kind of nothing this was. "0 depth chunks" is a recording that never held
            # depth (odometry-only capture) and costs the measurement nothing; "N depth chunks, M
            # failed to decode" is depth that WAS recorded and could not be read, which is a hole in
            # the evidence and must not be filed under the same heading.
            why = ("no /camera/depth in the recording (%d chunks scanned for it)"
                   % dstat.get("depth_chunks", 0)) if not dstat.get("depth_chunks") else (
                "%d depth chunks present but 0 frames decoded (%d chunk errors, %d short rows) "
                "-- DEPTH WAS RECORDED AND IS UNREADABLE"
                % (dstat["depth_chunks"], dstat.get("chunk_errors", 0),
                   dstat.get("short_rows", 0)))
            skipped.append((name, why))
            print("        ! SKIPPED (%s)" % why)
            continue

        rec = {"run": name, "nodata_lines": ndlines, "brake_evidence": brake_ev,
               "duration_s": dur, "frames": nfr, "profile": prof,
               "corridor_mode": cfg.get("corridor_mode", "?"), "config_mismatch": mism,
               "frame_valid_frac": pct(fvf, (50, 90)), "nodata_blind_frames": blind_n,
               "nodata_blind_frac_frames": blind_fr_n, "decode": dict(dstat)}
        if dstat.get("chunk_errors") or dstat.get("short_rows"):
            # A run that decoded SOME frames but lost others is a biased sample of its own scene,
            # not a clean one. Said out loud so its percentiles are read with that in mind.
            print("        NOTE partial decode: %d depth chunks, %d chunk errors, %d short rows "
                  "-- these frames are a subset of what was recorded"
                  % (dstat.get("depth_chunks", 0), dstat.get("chunk_errors", 0),
                     dstat.get("short_rows", 0)))
        for k in MODES:
            rec[k] = acc[k]
            _rb = [b for b in acc[k]["blobs"] if b["px"] >= a.ref_blob_px
                   and b["ring_med"] is not None and b["ring_med"] <= a.ref_ring_m]
            rec[k + "_ref_hits"] = len({b["frame"] for b in _rb})
            # WHERE the reference blobs land, in pixels of scatter across the run's frames. THIS IS
            # THE SELF-OCCLUSION TEST, and it is not optional: measured over the archive, the
            # highest-scoring "couch signature" in the CONTROL group is a ~3200 px no-return parked
            # at row 156 col 157 in 40+ frames of 20260904T002538Z_88a86f9 with a ring at 0.42-0.88 m
            # -- a hole that never moves, ringed by near geometry, is the ROBOT'S OWN ARM, which
            # returns 0.22-0.35 m and is the exact thing selfmask.py exists to remove. models/
            # self_mask.npy is EMPTY today (0 px: the K1 pans its head, so no pixel is always-near),
            # so nothing upstream is excluding the body and every hole rule below would brake on it.
            # A near-zero scatter means "fixed feature, suspect the robot"; a large one means the
            # blob actually moves through the frame the way an approached obstacle does.
            rec[k + "_ref_spread"] = (
                [float(np.std([b["row"] for b in _rb])), float(np.std([b["col"] for b in _rb]))]
                if len(_rb) > 1 else None)
        runs.append(rec)
        print("        %d frames  frame_valid p50=%.0f%%  strict hole p50=%.3f%% maxblob=%d  |  "
              "unusable hole p50=%.1f%% maxblob=%d  refhit=%d/%d  blind=%d"
              % (nfr, 100 * rec["frame_valid_frac"]["50"],
                 100 * float(np.percentile(acc["strict"]["hole_frac"], 50)),
                 int(max(acc["strict"]["largest"])),
                 100 * float(np.percentile(acc["unusable"]["hole_frac"], 50)),
                 int(max(acc["unusable"]["largest"])), rec["unusable_ref_hits"], nfr, blind_n))

    if not runs:
        print("\nno run produced a single readable depth frame -- nothing measured")
        return 1

    # THREE populations, not two. A run that logged no OBSTACLE-* line at all never exercised the
    # corridor code, so its frames say what an ordinary scene looks like but not what the brake did
    # with one. Kept separate rather than pooled into "clean", because pooling would let 63 runs
    # that never armed the brake dilute the false-brake rate of the ones that did.
    nodata_runs = [r for r in runs if r["nodata_lines"] > 0]
    clean_armed = [r for r in runs if r["nodata_lines"] == 0 and r["brake_evidence"] > 0]
    no_evidence = [r for r in runs if r["nodata_lines"] == 0 and r["brake_evidence"] == 0]
    groups = [("NODATA runs", nodata_runs), ("clean+obstacles", clean_armed),
              ("no-brake-log", no_evidence)]
    corridor_px = (rect[1] - rect[0]) * (rect[3] - rect[2]) if rect else 0

    print("\n" + "=" * 78)
    print("POPULATIONS (%d frames total, corridor %d px):"
          % (sum(r["frames"] for r in runs), corridor_px))
    print("   NODATA runs     %2d runs, %5d frames -- logged OBSTACLE-NODATA: the fail-open path"
          % (len(nodata_runs), sum(r["frames"] for r in nodata_runs)))
    print("   clean+obstacles %2d runs, %5d frames -- brake ran and saw near obstacles, never took "
          "the nodata path. THIS is the false-brake control group."
          % (len(clean_armed), sum(r["frames"] for r in clean_armed)))
    print("   no-brake-log    %2d runs, %5d frames -- no OBSTACLE-*/CLEARANCE line at all, so the "
          "corridor code is not shown to have run. Ordinary scenes, but NOT evidence about the "
          "brake." % (len(no_evidence), sum(r["frames"] for r in no_evidence)))
    if skipped:
        print("SKIPPED %d run(s):" % len(skipped))
        for n, why in skipped:
            print("   %s  %s" % (n, why))
    else:
        print("SKIPPED 0 runs")

    for mode in MODES:
        report_mode(mode, groups, corridor_px, a)

    print("\n" + "=" * 78)
    print("[5] PER-RUN -- reference rule = blob >= %d px with ring median <= %.2f m, on the "
          "'unusable' definition. The couch runs should surface at the top."
          % (a.ref_blob_px, a.ref_ring_m))
    print("   'spread' is the row/col scatter (px) of those blobs across the run's frames. READ IT "
          "BEFORE THE HIT COUNT: a few px of scatter means the blob never moved, which is the "
          "signature of the robot's own body, not of an obstacle being approached.")
    print("   %-26s %5s %6s %6s %7s %8s %9s %7s %11s"
          % ("run", "nodat", "brk_ev", "frames", "valid50", "strictmx", "unus_hf50", "refhit",
             "spread r/c"))
    for r in sorted(runs, key=lambda x: -x["unusable_ref_hits"]):
        sp = r.get("unusable_ref_spread")
        print("   %-26s %5d %6d %6d %6.0f%% %8d %8.1f%% %7d %11s"
              % (r["run"], r["nodata_lines"], r["brake_evidence"], r["frames"],
                 100 * r["frame_valid_frac"]["50"], int(max(r["strict"]["largest"])),
                 100 * float(np.percentile(r["unusable"]["hole_frac"], 50)),
                 r["unusable_ref_hits"], ("%.0f/%.0f" % (sp[0], sp[1])) if sp else "-"))

    tot_blind = sum(r["nodata_blind_frames"] for r in runs)
    tot_blind_fr = sum(r["nodata_blind_frac_frames"] for r in runs)
    nall = sum(r["frames"] for r in runs)
    print("\n   cross-check: of %d analysed frames, %d would trip the EXISTING sparse-frame guard "
          "(_nodata 'SENSOR BLIND') on the FOOTPRINT band (whole frame) and %d on the FRAC band "
          "(corridor only). The archive logged SENSOR BLIND 0 times in 1062 NODATA lines -- 889 of "
          "them footprint, 173 frac -- so 0/0 here confirms the decode and the thresholds agree "
          "with the robot's own view, and that the guard is simply never reached."
          % (nall, tot_blind, tot_blind_fr))

    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)) or ".", exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"params": vars(a), "runs": runs,
                       "skipped": [{"run": n, "why": w} for n, w in skipped]}, fh, indent=1)
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
