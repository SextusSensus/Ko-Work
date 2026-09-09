#!/usr/bin/env python3
"""rerun_tune.py -- offline tuning analyser for K1 follow .rrd captures.

WHAT IT IS
Reads a rerun (.rrd) recording produced by follow_person_k1.py --rerun on,
extracts per-frame telemetry (from both scalar streams and the text-log
stream), and emits a compact tuning report plus a machine-readable JSON
diff of suggested config changes.

WHY IT EXISTS
Field tuning of the follow loop is done by hand against ad-hoc `grep`
counts. That works one-off but doesn't build knowledge: two runs an hour
apart with slightly different config get compared by eyeball, and small
regressions slip in unnoticed. This script gives the operator a single
consistent scorecard per run and a diff against a previous run's scorecard
so tuning is an evidence-based conversation, not a vibe check.

WHAT IT INSPECTS
1. Loop latency (SLOW-LOOP / LOOP-MS): p50 / p90 / p99 / max, %over-budget
2. Tracker bearing jitter: %frames with |dbearing| > 2 deg -- proxy for
   how much yaw wobble the control law is asked to reject
3. vyaw when bearing is straight (|bearing| <= 3 deg): mean + %|vyaw|>0.02
   -- proxy for residual yaw drift the deadband isn't catching
4. CLEARANCE distribution: histogram of live corridor clearance, plus
   %vx-cap=0.00 (full brake) and their nearest-blob range signature
5. LOCALMAP: refine count, delta distribution (live - lm), %refines
   inside self-range (RED FLAG -- means the read-side gate isn't holding)
6. GAP-STEER: bias direction balance, %events, correlation with LOCALMAP
   phantoms
7. GROUND-REJECT: healthy count vs corridor-blocking count
8. Depth: sample average valid-pixel fraction in the corridor, hole-event
   distribution, whole-frame blind events
9. RECOMMENDATIONS: per-parameter suggested change based on measurements
   (with the rule that triggered it), plus a --emit-config-patch YAML
   file the operator can drop into a profile

USAGE
  python3 eval/rerun_tune.py --rrd path/to/k1_follow_XXXXXX.rrd
  python3 eval/rerun_tune.py --rrd path/to/k1_follow_XXXXXX.rrd \
      --baseline previous_report.json --emit-patch tune_patch.yaml
  python3 eval/rerun_tune.py --log path/to/k1_follow.err   # ALSO works on
                                                            # a plain stderr
                                                            # file when the
                                                            # .rrd isn't
                                                            # available.

EXIT CODES
  0  report emitted; no critical regressions vs --baseline
  1  regression detected vs --baseline
  2  input file unreadable or empty of relevant events
"""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict


# -- regexes ------------------------------------------------------------
# Each line in the follow's stderr / rerun TextLog has a stable prefix
# per event type. We compile once, use on every line -- these appear in
# the tens of thousands per run.
RE_TRACK = re.compile(
    r"TRACK.*?range=([\d.]+)\[[^\]]+\]\s+bearing=([+-][\d.]+)deg\s+"
    r"vx=([+-][\d.]+)\s+vyaw=([+-][\d.]+)"
)
RE_CLEARANCE = re.compile(
    r"CLEARANCE\s+([\d.]+)m\s+->\s+vx-cap\s+([\d.]+)"
)
RE_LOCALMAP = re.compile(
    r"LOCALMAP\s+([\d.]+)m\s+refines live\s+([\d.]+)m\s+\((\d+)\s+cells\)"
)
RE_GAPSTEER = re.compile(
    r"GAP-STEER\s+bias\s+([+-][\d.]+)\s+rad/s\s+\(bearing\s+([+-][\d.]+)deg"
)
RE_GROUND = re.compile(
    r"GROUND-REJECT\s+(\d+)px\s+at\s+([\d.]+)m"
)
RE_NODATA = re.compile(
    r"OBSTACLE-NODATA\s+\w+\s+blob=(\d+)px\s+need=(\d+)px\s+"
    r"frame_valid=(\d+)\s+->\s+(SENSOR BLIND|window empty)"
)
RE_DEPTHHOLE = re.compile(
    r"DEPTH-HOLE\s+corridor valid=(\d+)px\s+median=(\d+)px\s+ratio=([\d.]+)"
)
RE_SLOWLOOP = re.compile(
    r"SLOW-LOOP\s+\d+\s+frames over budget\s+\(dt=(\d+)ms"
)
RE_LOOPMS = re.compile(
    r"LOOP-MS\s+n=\d+\s+p50=(\d+)\s+p90=(\d+)\s+p99=(\d+)\s+max=(\d+)\s+budget=(\d+)"
)
RE_FAILSAFE = re.compile(r"FAIL-SAFE")
RE_LOST = re.compile(r"\bLOST\b")
RE_REACQUIRE = re.compile(r"\bREACQUIRE\b")


def wilson(k, n, z=1.96):
    """Wilson score interval for a success rate. Better than +/-1.96sqrt(p(1-p)/n)
    at small n or extremes -- we want honest confidence bounds when the report
    says "12 events out of 3000" not just a bare 0.4%."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), p, min(1.0, centre + half))


def parse_stream(iter_lines):
    """Parse whichever stream (rerun TextLog or stderr file) into a shared
    stats bucket. All work happens in ONE pass so a 200 MB rerun file with
    ~1M lines completes in seconds."""
    s = {
        "n_lines": 0,
        "track": [],           # (range, bearing_deg, vx, vyaw)
        "clearance": [],       # (clr_m, vx_cap)
        "localmap": [],        # (lm_m, live_m, cells)
        "gapsteer": [],        # (bias, bearing_deg)
        "ground": [],          # (npx, range_m)
        "nodata": [],          # (blob_px, need_px, frame_valid, kind)
        "depthhole": [],       # (cur_px, med_px, ratio)
        "slowloop_dt": [],     # ms
        "loopms": [],          # (p50, p90, p99, max, budget)
        "failsafe": 0,
        "lost": 0,
        "reacquire": 0,
    }
    for line in iter_lines:
        s["n_lines"] += 1
        m = RE_TRACK.search(line)
        if m:
            s["track"].append((float(m.group(1)), float(m.group(2)),
                                float(m.group(3)), float(m.group(4))))
            continue
        m = RE_CLEARANCE.search(line)
        if m:
            s["clearance"].append((float(m.group(1)), float(m.group(2))))
            continue
        m = RE_LOCALMAP.search(line)
        if m:
            s["localmap"].append((float(m.group(1)), float(m.group(2)), int(m.group(3))))
            continue
        m = RE_GAPSTEER.search(line)
        if m:
            s["gapsteer"].append((float(m.group(1)), float(m.group(2))))
            continue
        m = RE_GROUND.search(line)
        if m:
            s["ground"].append((int(m.group(1)), float(m.group(2))))
            continue
        m = RE_NODATA.search(line)
        if m:
            s["nodata"].append((int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)), m.group(4)))
            continue
        m = RE_DEPTHHOLE.search(line)
        if m:
            s["depthhole"].append((int(m.group(1)), int(m.group(2)), float(m.group(3))))
            continue
        m = RE_SLOWLOOP.search(line)
        if m:
            s["slowloop_dt"].append(int(m.group(1)))
            continue
        m = RE_LOOPMS.search(line)
        if m:
            s["loopms"].append((int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), int(m.group(4)), int(m.group(5))))
            continue
        if RE_FAILSAFE.search(line):
            s["failsafe"] += 1
        if RE_LOST.search(line):
            s["lost"] += 1
        if RE_REACQUIRE.search(line):
            s["reacquire"] += 1
    return s


def read_lines_from_rrd(path):
    """Yield text lines from a .rrd file's TextLog entities.

    Uses `rerun` if available; otherwise falls back to attempting to import
    from the sibling k1_rerun module. If neither is available, raises so the
    caller can fall back to --log."""
    try:
        import rerun as rr
    except ImportError as e:
        raise RuntimeError("rerun package not installed. Install: pip install rerun-sdk. "
                            "Or point --log at the k1_follow.err file instead.") from e
    # The rerun SDK Python API for reading recordings changed between 0.20-0.35.
    # We prefer the streaming Recording -> DataFrame approach; if it isn't
    # available, we try the RRD storage reader; if none, we raise.
    try:
        # rerun >= 0.24 exposes rr.dataframe.load_recording
        rec = rr.dataframe.load_recording(path)
        view = rec.view(index="log_time", contents="/**")
        # Filter to TextLog entities. The recording stores TextLog components on
        # entities where the follow logged them (top-level "text" or per-source
        # subpaths). We iterate ALL rows and pull the .body field where present.
        for batch in view.select().read_all():
            for row in batch.to_pylist():
                body = None
                for k, v in row.items():
                    if isinstance(v, str) and ("TRACK" in v or "CLEARANCE" in v
                            or "LOCALMAP" in v or "GAP-STEER" in v or "GROUND"
                            in v or "OBSTACLE-NODATA" in v or "DEPTH-HOLE" in v
                            or "SLOW-LOOP" in v or "LOOP-MS" in v):
                        body = v; break
                if body is not None:
                    yield body
        return
    except AttributeError:
        pass
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("failed to load rrd via dataframe API: %s" % e) from e
    # Older API fallback -- untested against every point release, so raise a
    # clear message if we get here so the operator can pass --log instead.
    raise RuntimeError("this rerun SDK version does not expose dataframe.load_recording; "
                        "pass --log path/to/k1_follow.err to analyse the stderr file directly.")


def read_lines_from_log(path):
    """Yield lines from a plain stderr file. errors='ignore' because ROS
    occasionally slips a stray byte through and we don't want to abort."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            yield line


def pct(v, p):
    if not v:
        return None
    v = sorted(v)
    i = max(0, min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1)))))
    return v[i]


def summarize(s, cfg):
    """Turn a stats bucket into a report dict. cfg carries the *current*
    tunable values so recommendations can be phrased as diffs."""
    r = {"totals": {"lines": s["n_lines"],
                    "track_frames": len(s["track"]),
                    "clearance_events": len(s["clearance"]),
                    "localmap_events": len(s["localmap"]),
                    "gapsteer_events": len(s["gapsteer"]),
                    "ground_events": len(s["ground"]),
                    "nodata_events": len(s["nodata"]),
                    "depthhole_events": len(s["depthhole"]),
                    "slowloop_events": len(s["slowloop_dt"]),
                    "loopms_reports": len(s["loopms"]),
                    "failsafe": s["failsafe"],
                    "lost": s["lost"],
                    "reacquire": s["reacquire"]}}
    recs = []
    # ---- yaw jitter -----------------------------------------------------
    bearings = [t[1] for t in s["track"]]
    if len(bearings) > 1:
        big = sum(1 for i in range(1, len(bearings))
                    if abs(bearings[i] - bearings[i - 1]) > 2.0)
        pct_big = 100.0 * big / (len(bearings) - 1)
        r["yaw_jitter"] = {"n": len(bearings) - 1, "big_jumps": big,
                            "pct_big_2deg": round(pct_big, 2)}
        if pct_big > 8.0 and cfg.get("follow_yaw_deadband_deg", 0.0) < 2.0:
            recs.append({
                "param": "follow_yaw_deadband_deg",
                "current": cfg.get("follow_yaw_deadband_deg"),
                "suggest": 2.0,
                "why": "bearing jitter %.1f%% of frames swing >2deg; raise deadband to reject." % pct_big,
                "confidence": "medium",
            })
    # vyaw at straight
    straight_v = [t[3] for t in s["track"] if abs(t[1]) <= 3.0]
    if straight_v:
        pos = sum(1 for v in straight_v if v > 0.02)
        neg = sum(1 for v in straight_v if v < -0.02)
        r["vyaw_when_straight"] = {"n": len(straight_v),
                                    "mean": round(sum(straight_v) / len(straight_v), 4),
                                    "pos": pos, "neg": neg}
    # ---- CLEARANCE distribution -----------------------------------------
    clr = [c[0] for c in s["clearance"]]
    fullstops = sum(1 for c in s["clearance"] if c[1] <= 0.005)
    if clr:
        r["clearance"] = {
            "n": len(clr),
            "p50": pct(clr, 50), "p10": pct(clr, 10), "p90": pct(clr, 90),
            "sub_65cm_pct": round(100.0 * sum(1 for c in clr if c < 0.65) / len(clr), 2),
            "fullstops": fullstops,
        }
        # A high sub_65cm_pct with no matching real obstacle is a phantom-brake
        # cluster. Flag it -- but only recommend a self-range bump if the
        # cluster is dense and no equivalently-close ground/localmap explanation
        # exists.
        if r["clearance"]["sub_65cm_pct"] > 15.0:
            recs.append({
                "param": "obstacle_self_range_m",
                "current": cfg.get("obstacle_self_range_m"),
                "suggest": round(cfg.get("obstacle_self_range_m", 0.65) + 0.05, 2),
                "why": "%.0f%% of clearances sub-0.65m -- arm/self shell still leaking. Raise self-range."
                        % r["clearance"]["sub_65cm_pct"],
                "confidence": "low",
            })
    # ---- LOCALMAP anomalies ---------------------------------------------
    if s["localmap"]:
        lm_inside = [x for x in s["localmap"] if x[0] < 0.5]
        r["localmap"] = {
            "n": len(s["localmap"]),
            "inside_0_5m": len(lm_inside),
            "median_delta_m": pct([x[1] - x[0] for x in s["localmap"]], 50),
            "median_cells": pct([x[2] for x in s["localmap"]], 50),
        }
        if len(lm_inside) > 5:
            recs.append({
                "param": "localmap_min_hits",
                "current": cfg.get("localmap_min_hits"),
                "suggest": max(3, int(cfg.get("localmap_min_hits", 2)) + 1),
                "why": "%d LOCALMAP hits INSIDE 0.5m -- read-side self-range gate not filtering. "
                        "Raise min-hits to reject faster-flashing arm cells." % len(lm_inside),
                "confidence": "high",
            })
    # ---- GAP-STEER ------------------------------------------------------
    if s["gapsteer"]:
        left = sum(1 for g in s["gapsteer"] if g[0] > 0)
        right = sum(1 for g in s["gapsteer"] if g[0] < 0)
        r["gapsteer"] = {"n": len(s["gapsteer"]), "left": left, "right": right}
        if left + right > 20 and abs(left - right) > 0.6 * (left + right):
            recs.append({
                "param": "gap_steer",
                "current": cfg.get("gap_steer"),
                "suggest": "audit",
                "why": "gap-steer heavily one-sided (%d L / %d R) -- likely reacting to a persistent "
                        "phantom on one side. Switch to audit until localmap phantoms are silenced."
                        % (left, right),
                "confidence": "medium",
            })
    # ---- ground reject balance ------------------------------------------
    if s["ground"]:
        r["ground_reject"] = {"n": len(s["ground"]),
                                "median_px": pct([g[0] for g in s["ground"]], 50)}
    # ---- depth-hole -----------------------------------------------------
    if s["depthhole"]:
        r["depth_hole"] = {"n": len(s["depthhole"]),
                            "median_ratio": pct([h[2] for h in s["depthhole"]], 50),
                            "median_baseline_px": pct([h[1] for h in s["depthhole"]], 50)}
    # ---- LOOP timing ----------------------------------------------------
    if s["slowloop_dt"]:
        r["slowloop"] = {"n": len(s["slowloop_dt"]),
                            "mean_ms": round(sum(s["slowloop_dt"]) / len(s["slowloop_dt"]), 1),
                            "max_ms": max(s["slowloop_dt"])}
        # If max is > 400 ms we're breaking the bridge staleness tier and
        # every long stall becomes a jerky recovery. Flag it. Not a param --
        # fix is jetson_clocks / rerun-off / detect batch tuning.
        if r["slowloop"]["max_ms"] > 400:
            recs.append({
                "param": "(runtime)",
                "current": None,
                "suggest": None,
                "why": "max loop dt %d ms breaks bridge staleness tier (>400ms). Verify "
                        "jetson_clocks pinned; consider --rerun off if enabled." % r["slowloop"]["max_ms"],
                "confidence": "high",
            })
    if s["loopms"]:
        # Take the LAST reported LOOP-MS as representative -- these are periodic
        # summaries emitted by the node itself.
        p50, p90, p99, mx, budget = s["loopms"][-1]
        r["loopms_final"] = {"p50": p50, "p90": p90, "p99": p99,
                              "max": mx, "budget": budget}
    # ---- tracking health ------------------------------------------------
    r["tracking"] = {
        "lost": s["lost"], "reacquire": s["reacquire"], "failsafe": s["failsafe"],
    }
    r["recommendations"] = recs
    return r


def diff_reports(cur, base):
    """Compare two report dicts, return regressions and improvements."""
    out = {"regressions": [], "improvements": [], "unchanged": []}
    def _cmp(name, cur_v, base_v, higher_is_bad=True):
        if cur_v is None or base_v is None or base_v == 0:
            return
        rel = (cur_v - base_v) / abs(base_v)
        if abs(rel) < 0.10:
            out["unchanged"].append((name, cur_v, base_v))
            return
        entry = {"metric": name, "cur": cur_v, "base": base_v,
                    "delta_pct": round(100.0 * rel, 1)}
        target = out["regressions" if (rel > 0) == higher_is_bad else "improvements"]
        target.append(entry)
    if "yaw_jitter" in cur and "yaw_jitter" in base:
        _cmp("yaw_jitter.pct_big_2deg", cur["yaw_jitter"]["pct_big_2deg"],
                base["yaw_jitter"]["pct_big_2deg"])
    if "clearance" in cur and "clearance" in base:
        _cmp("clearance.sub_65cm_pct", cur["clearance"]["sub_65cm_pct"],
                base["clearance"]["sub_65cm_pct"])
        _cmp("clearance.fullstops", cur["clearance"]["fullstops"],
                base["clearance"]["fullstops"])
    if "localmap" in cur and "localmap" in base:
        _cmp("localmap.inside_0_5m", cur["localmap"]["inside_0_5m"],
                base["localmap"]["inside_0_5m"])
    if "slowloop" in cur and "slowloop" in base:
        _cmp("slowloop.max_ms", cur["slowloop"]["max_ms"],
                base["slowloop"]["max_ms"])
    if "loopms_final" in cur and "loopms_final" in base:
        _cmp("loopms.p99", cur["loopms_final"]["p99"],
                base["loopms_final"]["p99"])
    return out


def print_report(r):
    def _k(d, path, default="-"):
        cur = d
        for p in path.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur
    print("=" * 72)
    print("TUNING REPORT")
    print("=" * 72)
    t = r["totals"]
    print("Totals: track=%d, clearance=%d, localmap=%d, gapsteer=%d, "
            "ground=%d, nodata=%d, depthhole=%d, slowloop=%d, "
            "lost=%d, reacquire=%d, failsafe=%d"
            % (t["track_frames"], t["clearance_events"], t["localmap_events"],
                t["gapsteer_events"], t["ground_events"], t["nodata_events"],
                t["depthhole_events"], t["slowloop_events"],
                t["lost"], t["reacquire"], t["failsafe"]))
    print("")
    print("Yaw jitter: %s%% frames swing >2deg (%s/%s)" % (
        _k(r, "yaw_jitter.pct_big_2deg"), _k(r, "yaw_jitter.big_jumps"),
        _k(r, "yaw_jitter.n")))
    print("vyaw when straight (|bearing|<=3): mean=%s, pos=%s, neg=%s (of %s)" % (
        _k(r, "vyaw_when_straight.mean"), _k(r, "vyaw_when_straight.pos"),
        _k(r, "vyaw_when_straight.neg"), _k(r, "vyaw_when_straight.n")))
    print("")
    print("CLEARANCE p10/p50/p90: %s/%s/%s m, sub-0.65m: %s%%, fullstops: %s" % (
        _k(r, "clearance.p10"), _k(r, "clearance.p50"), _k(r, "clearance.p90"),
        _k(r, "clearance.sub_65cm_pct"), _k(r, "clearance.fullstops")))
    print("LOCALMAP events=%s, inside 0.5m=%s, median delta=%s m, median cells=%s" % (
        _k(r, "localmap.n"), _k(r, "localmap.inside_0_5m"),
        _k(r, "localmap.median_delta_m"), _k(r, "localmap.median_cells")))
    print("GAP-STEER events=%s (L=%s / R=%s)" % (
        _k(r, "gapsteer.n"), _k(r, "gapsteer.left"), _k(r, "gapsteer.right")))
    print("DEPTH-HOLE events=%s, median ratio=%s, median baseline=%s px" % (
        _k(r, "depth_hole.n"), _k(r, "depth_hole.median_ratio"),
        _k(r, "depth_hole.median_baseline_px")))
    print("SLOW-LOOP events=%s, mean=%s ms, max=%s ms | LOOP-MS final: %s" % (
        _k(r, "slowloop.n"), _k(r, "slowloop.mean_ms"),
        _k(r, "slowloop.max_ms"), _k(r, "loopms_final")))
    print("")
    if r["recommendations"]:
        print("RECOMMENDATIONS:")
        for rec in r["recommendations"]:
            print("  [%s conf] %s: %s -> %s"
                    % (rec["confidence"], rec["param"], rec["current"], rec["suggest"]))
            print("      %s" % rec["why"])
    else:
        print("No tuning recommendations -- everything within thresholds.")


def emit_patch(recs, out_path):
    """Write a YAML fragment of suggested config changes. Only parameters
    with a concrete numeric/string suggest survive; runtime-hint recs (e.g.
    jetson_clocks) are dropped since they have no config counterpart."""
    lines = ["# Auto-generated tuning patch from rerun_tune.py\n",
                "# Review each line, then merge into a profile YAML.\n"]
    for r in recs:
        if r["suggest"] is None or r["param"].startswith("("):
            continue
        val = r["suggest"]
        vstr = ('"%s"' % val) if isinstance(val, str) else str(val)
        lines.append("%s: %s   # %s\n" % (r["param"], vstr, r["why"]))
    with open(out_path, "w") as f:
        f.writelines(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rrd", help="rerun .rrd recording (requires rerun-sdk installed)")
    src.add_argument("--log", help="k1_follow.err stderr file (no dependencies)")
    ap.add_argument("--cfg",
                    help="current defaults.yaml so recommendations are phrased as diffs. "
                            "Optional; without it 'current' fields will be empty.")
    ap.add_argument("--baseline",
                    help="prior report JSON to compare against; exits 1 on regression")
    ap.add_argument("--emit-patch",
                    help="write a YAML patch of suggested config changes to this path")
    ap.add_argument("--json", action="store_true",
                    help="also print the report as JSON to stdout")
    ap.add_argument("--out",
                    help="write report JSON to this path (in addition to stdout summary)")
    a = ap.parse_args()

    cfg = {}
    if a.cfg and os.path.exists(a.cfg):
        # We deliberately don't require pyyaml -- parse only the flat scalars
        # we care about with a tiny hand-rolled loop.
        with open(a.cfg) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip(); v = v.strip().strip("'\"")
                try:
                    cfg[k] = float(v) if "." in v or "e" in v.lower() else int(v)
                except ValueError:
                    cfg[k] = v

    if a.rrd:
        try:
            iter_lines = read_lines_from_rrd(a.rrd)
            stats = parse_stream(iter_lines)
        except RuntimeError as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 2
    else:
        if not os.path.exists(a.log):
            print("ERROR: log file not found: %s" % a.log, file=sys.stderr)
            return 2
        stats = parse_stream(read_lines_from_log(a.log))

    if stats["n_lines"] == 0:
        print("ERROR: no lines read from input", file=sys.stderr)
        return 2

    report = summarize(stats, cfg)
    print_report(report)

    if a.json:
        print("\n" + "=" * 72)
        print(json.dumps(report, indent=2, default=str))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print("\nreport JSON -> %s" % a.out)
    if a.emit_patch:
        emit_patch(report["recommendations"], a.emit_patch)
        print("patch YAML -> %s (review before merging into a profile)" % a.emit_patch)

    rc = 0
    if a.baseline and os.path.exists(a.baseline):
        with open(a.baseline) as f:
            base = json.load(f)
        d = diff_reports(report, base)
        print("\n" + "=" * 72)
        print("DIFF vs baseline (%s)" % a.baseline)
        if d["regressions"]:
            rc = 1
            print("REGRESSIONS:")
            for e in d["regressions"]:
                print("  %s: %s (was %s, %+.1f%%)"
                        % (e["metric"], e["cur"], e["base"], e["delta_pct"]))
        if d["improvements"]:
            print("IMPROVEMENTS:")
            for e in d["improvements"]:
                print("  %s: %s (was %s, %+.1f%%)"
                        % (e["metric"], e["cur"], e["base"], e["delta_pct"]))
        if not d["regressions"] and not d["improvements"]:
            print("No changes above 10% threshold in tracked metrics.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
