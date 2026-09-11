#!/usr/bin/env python3
"""camera_gaps.py -- head-camera stall forensics from K1 follow recordings, offline and read-only.

WHAT IT MEASURES. For every run under runs/ with a local, complete .rrd (newest first), the gaps in
the RGB and depth streams on the recording's `wall` timeline (time.time() at each camera callback),
cross-checked against the node's own k1_follow.err stall lines. Built for a BEFORE/AFTER comparison
of the burst-stall problem (rgb+depth stop together for seconds, then flush a backlog).

TIMING RULES (learned the hard way; do not "simplify" them away):
  * Never time anything by frame_idx. It is the RGB callback counter (_seq); depth rows carry the
    RGB seq current when depth arrived. It is used here ONLY as a COUNTER (did a new RGB callback
    happen?), never as a clock.
  * Logged /camera/rgb and /camera/depth rows are DECIMATED 1/N inside the callback (the err line
    'RERUN active ... (image 1/N)'). Logged depth therefore arrives only every ~450 ms, so a 0.5 s
    threshold on logged depth mostly counts normal cadence; the >2 s figures are the meaningful ones
    for depth.
  * A stall that runs into the END of a recording is not an inter-frame gap. It is counted as a
    trailing gap up to the last row of the recording (the node keeps logging odom/fsm while blind).
  * The main loop's frame() stamp LEAKS onto rows logged by the cam-spin thread (measured: every
    /fsm/state_id wall also appears on /odom/x rows), so `wall` on non-camera rows is not a camera
    callback time. The camera rows themselves were checked clean (0 shared stamps with loop ticks).
    Un-decimated RGB liveness therefore uses log_time of the rows where the running max of
    frame_idx increases -- a leaked stamp can only carry an OLDER seq, never a newer one.

USAGE (the .rrd files need rerun-sdk 0.23.1 -- the laptop's .venv-analysis):
  .venv-analysis\\Scripts\\python.exe eval\\camera_gaps.py
  ... camera_gaps.py --limit 20 --json out.json
  ... camera_gaps.py --run 20260910T033242Z_fb06ea6 --gaps
"""
import argparse
import datetime as _dt
import gc
import json
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = r"C:\Users\toddm\OneDrive\Desktop\runtime"


def _repo_root(cli_repo):
    """eval/<this file> -> repo root; from anywhere else fall back to --repo."""
    if os.path.basename(HERE) == "eval" and os.path.isfile(os.path.join(HERE, "depth_replay.py")):
        return os.path.dirname(HERE)
    return cli_repo


# ---------------------------------------------------------------------------- err parsing

_RE = {
    "img_n": re.compile(r"^RERUN active .*\(image 1/(\d+)\)"),
    "noframe_stall": re.compile(r"^NO-FRAME stall=([0-9.]+)s"),
    "noframe_boot": re.compile(r"^NO-FRAME$"),
    "depth_state": re.compile(r"^DEPTH (FRESH|STALE|DOWN|WARMING) fps="),
    "rgb_total": re.compile(r"^RGB fps=[0-9.]+ \(all-topics\) total=(\d+)"),
    "starved": re.compile(r"^DEPTH-STARVED fps="),
    "starved_clr": re.compile(r"^DEPTH-STARVED cleared"),
    "loop_stall": re.compile(r"^LOOP-STALL (\d+)ms"),
    "failsafe": re.compile(r"^FAIL-SAFE STAND"),
    "rr_disabled": re.compile(r"^RERUN-DISABLED-SLOW \d+ frames"),
    "rr_overrun": re.compile(r"^RERUN-OVERRUN \d+ consecutive"),
    "drive_abort": re.compile(r"^DRIVE-ABORT (.*)"),
    "drive_active": re.compile(r"^DRIVE-ACTIVE"),
}


def parse_err(path):
    out = {"lines": 0, "img_n": None, "noframe_boot": 0, "noframe_stall_lines": 0,
           "noframe_episodes": [], "depth_down_lines": 0, "depth_down_episodes": 0,
           "depth_stale_lines": 0, "starved": 0, "starved_cleared": 0, "loop_stall_ms": [],
           "failsafe": 0, "rr_disabled": 0, "rr_overrun": 0, "drive_abort": None,
           "drive_active": False, "max_total": None}
    if not os.path.isfile(path):
        out["missing"] = True
        return out
    prev_depth, ep = None, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            out["lines"] += 1
            m = _RE["img_n"].match(line)
            if m and out["img_n"] is None:
                out["img_n"] = int(m.group(1))
            m = _RE["noframe_stall"].match(line)
            if m:
                v = float(m.group(1))
                out["noframe_stall_lines"] += 1
                if ep is None or v < ep:                   # the stall clock restarted: new episode
                    out["noframe_episodes"].append(v)
                else:
                    out["noframe_episodes"][-1] = v
                ep = v
                continue
            if _RE["noframe_boot"].match(line):
                out["noframe_boot"] += 1
            m = _RE["depth_state"].match(line)
            if m:
                st = m.group(1)
                if st == "DOWN":
                    out["depth_down_lines"] += 1
                    if prev_depth != "DOWN":
                        out["depth_down_episodes"] += 1
                elif st == "STALE":
                    out["depth_stale_lines"] += 1
                prev_depth = st
            m = _RE["rgb_total"].match(line)
            if m:
                out["max_total"] = int(m.group(1))
            if _RE["starved_clr"].match(line):
                out["starved_cleared"] += 1
            elif _RE["starved"].match(line):
                out["starved"] += 1
            m = _RE["loop_stall"].match(line)
            if m:
                out["loop_stall_ms"].append(int(m.group(1)))
            if _RE["failsafe"].match(line):
                out["failsafe"] += 1
            if _RE["rr_disabled"].match(line):
                out["rr_disabled"] += 1
            if _RE["rr_overrun"].match(line):
                out["rr_overrun"] += 1
            m = _RE["drive_abort"].match(line)
            if m and out["drive_abort"] is None:
                out["drive_abort"] = m.group(1)[:90]
            if _RE["drive_active"].match(line):
                out["drive_active"] = True
    return out


# ---------------------------------------------------------------------------- rrd reading

def _i64(tbl, name):
    import pyarrow as pa
    import pyarrow.compute as pc
    if name not in tbl.column_names:
        return None
    c = tbl.column(name)
    if pa.types.is_timestamp(c.type) or pa.types.is_duration(c.type):
        c = pc.cast(c, pa.int64())
    return pc.fill_null(c, -1).to_numpy()


def read_rows(rec, index_cols, ents, ent, comp, index):
    """Index columns only (no image bytes) for the rows of one entity/component."""
    if ent not in ents or index not in index_cols:
        return None
    cols = [c for c in ("wall", "frame_idx", "log_time") if c in index_cols]
    tbl = rec.view(index=index, contents={ent: [comp]}).select(*cols).read_all()
    return {c: _i64(tbl, c) for c in cols}


# ---------------------------------------------------------------------------- gap maths

def stream_stats(ts, end, lo, hi):
    """ts: sorted callback times (s). end: last row of the recording (s). A silence from the last
    frame to `end` is a trailing gap; the leading wait before the first frame is reported apart."""
    if len(ts) == 0:
        return {"n": 0}
    iv = np.diff(ts)
    tail = max(0.0, end - ts[-1]) if end is not None else 0.0
    gaps = np.concatenate([iv, [tail]]) if tail > lo else iv
    dur = (max(end, ts[-1]) if end is not None else ts[-1]) - ts[0]
    return {
        "n": int(len(ts)),
        "dur_s": round(float(dur), 2),
        "med_ms": round(float(np.median(iv) * 1000), 1) if len(iv) else None,
        "gaps_lo": int((gaps > lo).sum()),
        "gaps_hi": int((gaps > hi).sum()),
        "max_gap_s": round(float(gaps.max()), 2) if len(gaps) else 0.0,
        "frac_lo": round(float(gaps[gaps > lo].sum() / dur), 3) if dur > 0 else None,
        "frac_hi": round(float(gaps[gaps > hi].sum() / dur), 3) if dur > 0 else None,
        "tail_s": round(float(tail), 2),
    }


def gap_list(ts, end, thr):
    """[(start, dur)] including a trailing silence up to end."""
    out = [(float(a), float(d)) for a, d in zip(ts[:-1], np.diff(ts)) if d > thr]
    if len(ts) and end is not None and end - ts[-1] > thr:
        out.append((float(ts[-1]), float(end - ts[-1])))
    return out


def overlap_count(a, b):
    """How many intervals in a overlap at least one interval in b."""
    return sum(1 for s, d in a if any(s < t + e and t < s + d for t, e in b))


def rgb_liveness(rows_list):
    """log_time (s) of every row where the running max of frame_idx increases -- one entry per
    new RGB callback seen, timed by log_time. Robust to the leaked-stamp issue (see docstring)."""
    lt = np.concatenate([r["log_time"] for r in rows_list if r is not None])
    fi = np.concatenate([r["frame_idx"] for r in rows_list if r is not None])
    ok = (lt >= 0) & (fi >= 0)
    lt, fi = lt[ok], fi[ok]
    if len(lt) == 0:
        return np.array([]), None
    o = np.argsort(lt, kind="stable")
    lt, fi = lt[o], fi[o]
    rm = np.maximum.accumulate(fi)
    inc = np.concatenate([[True], rm[1:] > rm[:-1]])
    return lt[inc] / 1e9, int(rm[-1])


# ---------------------------------------------------------------------------- one run

def analyse_run(run_dir, lo, hi, want_gaps):
    rid = os.path.basename(run_dir)
    res = {"run": rid}
    rrds = sorted(f for f in os.listdir(run_dir) if f.startswith("k1_follow_") and f.endswith(".rrd"))
    res["err"] = parse_err(os.path.join(run_dir, "k1_follow.err"))
    if not rrds:
        res["status"] = "NO_LOCAL_RRD"
        return res
    rrd = os.path.join(run_dir, rrds[0])
    res["rrd"] = rrds[0]
    size = os.path.getsize(rrd)
    try:
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        want = next((f.get("bytes") for f in man.get("files", []) if f.get("name") == rrds[0]), None)
    except (OSError, ValueError):
        want = None
    res["rrd_bytes"] = size
    if want is not None and int(want) != size:
        res["status"] = "INCOMPLETE (%d of %d bytes -- still transferring?)" % (size, int(want))
        return res

    t0 = time.perf_counter()
    rec = load_recording(rrd)
    sch = rec.schema()
    index_cols = [c.name for c in sch.index_columns()]
    ents = sorted({c.entity_path for c in sch.component_columns() if not c.is_static})
    res["open_s"] = round(time.perf_counter() - t0, 1)
    res["timelines"] = index_cols
    res["entities"] = ents

    odo = read_rows(rec, index_cols, ents, "/odom/x", "Scalar", "log_time")
    fsm = read_rows(rec, index_cols, ents, "/fsm/state_id", "Scalar", "log_time")
    rgb = read_rows(rec, index_cols, ents, "/camera/rgb", "ImageFormat", "wall")
    dep = read_rows(rec, index_cols, ents, "/camera/depth", "ImageFormat", "wall")
    del rec
    gc.collect()

    lts = [r["log_time"][r["log_time"] >= 0] for r in (odo, fsm, rgb, dep) if r is not None]
    lts = [x for x in lts if len(x)]
    rec_lo = min(float(x.min()) for x in lts) / 1e9 if lts else None
    rec_hi = max(float(x.max()) for x in lts) / 1e9 if lts else None
    res["rec_start_utc"] = (_dt.datetime.fromtimestamp(rec_lo, _dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                            if rec_lo else None)
    res["rec_span_s"] = round(rec_hi - rec_lo, 2) if lts else None

    if "wall" not in index_cols:
        res["status"] = "NO_CAMERA (no camera callback and no loop tick ever set the wall timeline)"
        res["odom_rows"] = int(len(odo["log_time"])) if odo is not None else 0
        return res

    def walls(r):
        if r is None:
            return np.array([])
        w = r["wall"][r["wall"] >= 0] / 1e9
        return np.sort(w)

    rw, dw = walls(rgb), walls(dep)
    res["rgb"] = stream_stats(rw, rec_hi, lo, hi)
    res["depth"] = stream_stats(dw, rec_hi, lo, hi)
    res["rgb"]["first_frame_s"] = round(float(rw[0] - rec_lo), 2) if len(rw) else None
    res["depth"]["first_frame_s"] = round(float(dw[0] - rec_lo), 2) if len(dw) else None

    # Un-decimated RGB callback liveness (see module docstring for why log_time, not wall).
    live, max_seq = rgb_liveness([odo, fsm, rgb, dep])
    res["rgb_callbacks"] = stream_stats(live, rec_hi, lo, hi)
    res["rgb_callbacks"]["max_seq"] = max_seq

    # A stream with no frame at all was silent for the whole recording: one gap, not none.
    rg_hi = gap_list(rw, rec_hi, hi) if len(rw) else [(rec_lo, rec_hi - rec_lo)]
    dg_hi = gap_list(dw, rec_hi, hi) if len(dw) else [(rec_lo, rec_hi - rec_lo)]
    res["coincide"] = {"rgb_gaps_hi": len(rg_hi), "rgb_with_depth_overlap": overlap_count(rg_hi, dg_hi),
                       "depth_gaps_hi": len(dg_hi), "depth_with_rgb_overlap": overlap_count(dg_hi, rg_hi)}

    # Was the node's executor alive during each RGB stall? odom is serviced on the same thread.
    if odo is not None and len(odo["log_time"]) > 10:
        ol = np.sort(odo["log_time"][odo["log_time"] >= 0]) / 1e9
        rate = 1.0 / float(np.median(np.diff(ol)))
        fr = []
        for s, d in rg_hi:
            n = int(((ol > s) & (ol < s + d)).sum())
            fr.append(n / (d * rate))
        res["odom_during_rgb_gaps"] = {"odom_hz": round(rate, 1),
                                       "min_frac_expected": round(min(fr), 2) if fr else None,
                                       "median_frac_expected": round(float(np.median(fr)), 2) if fr else None}

    # Loop window + tick gaps (the recorded twin of LOOP-STALL, which fires on tick-to-tick > 1 s).
    if fsm is not None and len(fsm["log_time"]) > 1:
        ft = np.sort(fsm["log_time"][fsm["log_time"] >= 0]) / 1e9
        d = np.diff(ft)
        res["loop"] = {"ticks": int(len(ft)), "start_s": round(float(ft[0] - rec_lo), 2),
                       "tick_gaps_gt1s": [round(float(x), 3) for x in d[d > 1.0]]}

        # NO-FRAME needs the loop running and a frame already seen: compare with RGB callback gaps
        # > stall_seconds (1.0) that OVERLAP the loop window (a stall can begin before the loop).
        def in_loop(g):
            return g[0] < ft[-1] + 1.0 and g[0] + g[1] > ft[0]

        cb_gaps = [g for g in gap_list(live, rec_hi, 1.0) if in_loop(g)]
        res["lineup"] = {"rgb_callback_gaps_gt1s_in_loop": [round(dd, 2) for _s, dd in cb_gaps],
                         "noframe_episode_max_s": res["err"]["noframe_episodes"],
                         "logged_depth_gaps_gt2s_in_loop": [round(dd, 2) for s, dd in dg_hi
                                                            if in_loop((s, dd))],
                         "depth_down_episodes": res["err"]["depth_down_episodes"],
                         "loop_tick_gaps_gt1s": res["loop"]["tick_gaps_gt1s"],
                         "loop_stall_ms": res["err"]["loop_stall_ms"]}
    if want_gaps:
        res["rgb_gap_list"] = [(round(s - rec_lo, 2), round(d, 2)) for s, d in gap_list(rw, rec_hi, lo)]
        res["depth_gap_list"] = [(round(s - rec_lo, 2), round(d, 2)) for s, d in gap_list(dw, rec_hi, hi)]
        res["rgb_callback_gap_list"] = [(round(s - rec_lo, 2), round(d, 2))
                                        for s, d in gap_list(live, rec_hi, lo)]
    res["status"] = "OK"
    return res


# ---------------------------------------------------------------------------- report

def lineup_verdict(r):
    """Plain comparison of err stall episodes with recorded gaps -- counts and in-order durations."""
    lu = r.get("lineup")
    if not lu:
        return "loop never ran (no NO-FRAME/LOOP-STALL possible)"
    cg, ne = lu["rgb_callback_gaps_gt1s_in_loop"], lu["noframe_episode_max_s"]
    lg, ls = lu["loop_tick_gaps_gt1s"], lu["loop_stall_ms"]
    dg, de = lu["logged_depth_gaps_gt2s_in_loop"], lu["depth_down_episodes"]
    parts = ["NO-FRAME ep %d vs rgb cb gaps>1s %d" % (len(ne), len(cg)),
             "LOOP-STALL %d vs tick gaps>1s %d" % (len(ls), len(lg)),
             "DEPTH DOWN ep %d vs depth gaps>2s %d" % (de, len(dg))]
    return "; ".join(parts)


def fmt_stream(s):
    if not s or not s.get("n"):
        return "%5s %6s %6s %4s %4s %6s %5s %6s" % ("0", "-", "-", "-", "-", "-", "-", "-")
    return "%5d %6.1f %6.0f %4d %4d %6.2f %5.2f %6.1f" % (
        s["n"], s["dur_s"], s["med_ms"] or 0, s["gaps_lo"], s["gaps_hi"], s["max_gap_s"],
        s["frac_lo"] or 0, s["tail_s"])


def print_table(results, lo, hi):
    hdr = "%5s %6s %6s %4s %4s %6s %5s %6s" % ("n", "dur_s", "med_ms", ">%.1f" % lo, ">%g" % hi,
                                              "max_s", "frac", "tail_s")
    print("\nSTREAMS  (logged rows on `wall`; tail = silence from last frame to recording end, "
          "counted as a gap; frac = share of dur inside gaps > %.1f s)" % lo)
    print("%-26s %-6s %s" % ("run", "stream", hdr))
    for r in results:
        if r.get("status") != "OK":
            print("%-26s %s" % (r["run"], r.get("status")))
            continue
        for name in ("rgb", "depth", "rgb_callbacks"):
            label = {"rgb": "rgb", "depth": "depth", "rgb_callbacks": "rgb-cb"}[name]
            print("%-26s %-6s %s" % (r["run"] if name == "rgb" else "", label, fmt_stream(r.get(name))))
    print("\nCOINCIDENCE + ERR CROSS-CHECK")
    for r in results:
        e = r["err"]
        errs = ("NO-FRAME stall lines %d (episodes %s)  DEPTH DOWN lines %d  DEPTH-STARVED %d  "
                "LOOP-STALL %s  FAIL-SAFE %d%s%s" % (
                    e["noframe_stall_lines"], e["noframe_episodes"] or "-", e["depth_down_lines"],
                    e["starved"], e["loop_stall_ms"] or "-", e["failsafe"],
                    ("  RERUN-DISABLED-SLOW %d (recording truncated)" % e["rr_disabled"]) if e["rr_disabled"] else "",
                    ("  DRIVE-ABORT: " + e["drive_abort"]) if e["drive_abort"] else ""))
        print("%s  [%s]" % (r["run"], r.get("status")))
        print("    err: %s" % errs)
        if r.get("status") == "OK":
            c = r["coincide"]
            od = r.get("odom_during_rgb_gaps") or {}
            print("    coincide: rgb gaps>%gs with overlapping depth gap>%gs %d/%d; depth gaps>%gs with rgb gap %d/%d"
                  "%s" % (hi, hi, c["rgb_with_depth_overlap"], c["rgb_gaps_hi"], hi,
                          c["depth_with_rgb_overlap"], c["depth_gaps_hi"],
                          ("; odom (same executor) during rgb gaps: median %.0f%% of normal (min %.0f%%)"
                           % (100 * od["median_frac_expected"], 100 * od["min_frac_expected"]))
                          if od.get("min_frac_expected") is not None else ""))
            print("    lineup: %s" % lineup_verdict(r))
            if r.get("lineup"):
                lu = r["lineup"]
                print("      rgb cb gaps>1s in loop %s | NO-FRAME ep max %s | tick gaps>1s %s | LOOP-STALL ms %s"
                      % (lu["rgb_callback_gaps_gt1s_in_loop"], lu["noframe_episode_max_s"],
                         lu["loop_tick_gaps_gt1s"], lu["loop_stall_ms"]))
            for k in ("rgb_gap_list", "depth_gap_list", "rgb_callback_gap_list"):
                if k in r:
                    print("      %s (t_rel_s, dur_s): %s" % (k, r[k]))
        elif "odom_rows" in r:
            print("    recording: entities %s, odom rows %d over %.1f s from %sZ"
                  % (r["entities"], r["odom_rows"], r["rec_span_s"] or 0, r["rec_start_utc"]))


# ---------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO, help="repo root (auto when this file is in eval/)")
    ap.add_argument("--runs", default=None, help="runs directory (default <repo>/runs)")
    ap.add_argument("--limit", type=int, default=20, help="newest N runs with a local .rrd")
    ap.add_argument("--run", action="append", default=[], help="analyse only this run id (repeatable)")
    ap.add_argument("--lo", type=float, default=0.5, help="small gap threshold, s")
    ap.add_argument("--hi", type=float, default=2.0, help="large gap threshold, s")
    ap.add_argument("--gaps", action="store_true", help="also print every gap (start, duration)")
    ap.add_argument("--json", default=None, help="write all results here")
    a = ap.parse_args()

    repo = _repo_root(a.repo)
    sys.path.insert(0, os.path.join(repo, "eval"))
    global load_recording
    from depth_replay import load_recording  # noqa: E402 -- repo-relative on purpose

    runs_dir = a.runs or os.path.join(repo, "runs")
    snap = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    names = sorted((n for n in os.listdir(runs_dir) if re.match(r"^\d{8}T\d{6}Z_", n)), reverse=True)
    if a.run:
        names = [n for n in names if n in set(a.run)]
    picked, skipped = [], []
    for n in names:
        d = os.path.join(runs_dir, n)
        has = any(f.startswith("k1_follow_") and f.endswith(".rrd") for f in os.listdir(d))
        if has:
            if len(picked) < a.limit:
                picked.append(n)
        elif not picked or len(picked) < a.limit:
            skipped.append(n)
    print("snapshot %s  runs dir %s" % (snap, runs_dir))
    print("analysing %d run(s) with a local .rrd; %d newer-or-interleaved run(s) have NO local .rrd: %s"
          % (len(picked), len(skipped), ", ".join(skipped) or "-"))

    results = []
    for n in picked:
        t0 = time.perf_counter()
        r = analyse_run(os.path.join(runs_dir, n), a.lo, a.hi, a.gaps)
        r["elapsed_s"] = round(time.perf_counter() - t0, 1)
        results.append(r)
        print("  %-26s %-8s %.1fs" % (n, (r.get("status") or "")[:8], r["elapsed_s"]), flush=True)
        gc.collect()

    print_table(results, a.lo, a.hi)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"snapshot": snap, "runs_dir": runs_dir, "lo_s": a.lo, "hi_s": a.hi,
                       "no_local_rrd": skipped, "runs": results}, fh, indent=2)
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
