#!/usr/bin/env python3
"""loop_stats.py (P4.1) -- summarize the follow loop's per-iteration timing from a text log
(k1_follow.err). The node emits a `LOOP-MS` line every ~10 s with p50/p90/p99/max computed over
the trailing ~60 s window (600 samples @10 Hz), tagged by whether Rerun was on. This aggregates
those windowed snapshots into a single baseline table, split by rerun on/off -- the number every
Phase-4 perf knob is judged against (IMPLEMENTATION_README.md P4.1).

For the .rrd path (richer, per-iteration series incl. /diag/rr_track_ms) use rrd_loop_stats.py.

Usage:  python loop_stats.py <k1_follow.err> [more.err ...]
"""
import re
import sys

LINE = re.compile(
    r"LOOP-MS\s+n=(\d+)\s+p50=([\d.]+)\s+p90=([\d.]+)\s+p99=([\d.]+)\s+max=([\d.]+)"
    r"\s+budget=([\d.]+)\s+rerun=(on|off)")


def _pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(argv):
    if not argv:
        raise SystemExit("usage: loop_stats.py <k1_follow.err> [more.err ...]")
    groups = {"on": [], "off": []}   # rerun -> list of (n,p50,p90,p99,max,budget)
    total = 0
    for path in argv:
        with open(path, "r", errors="replace") as f:
            for ln in f:
                m = LINE.search(ln)
                if not m:
                    continue
                total += 1
                n, p50, p90, p99, mx, budget, rr = m.groups()
                groups[rr].append((int(n), float(p50), float(p90), float(p99), float(mx), float(budget)))

    if total == 0:
        raise SystemExit("no LOOP-MS lines found (is this a --stream/preview follow log?)")

    print("LOOP BASELINE  (%d LOOP-MS windows across %d file(s))" % (total, len(argv)))
    print("%-8s %6s %8s %8s %8s %9s %8s" % ("rerun", "wins", "p50~med", "p90~med", "p99~med", "p99~worst", "budget"))
    for rr in ("off", "on"):
        rows = groups[rr]
        if not rows:
            continue
        p50s = [r[1] for r in rows]; p90s = [r[2] for r in rows]
        p99s = [r[3] for r in rows]; mxs = [r[4] for r in rows]
        budget = rows[-1][5]
        print("%-8s %6d %8.0f %8.0f %8.0f %9.0f %8.0f"
              % (rr, len(rows), _pct(p50s, 0.5), _pct(p90s, 0.5), _pct(p99s, 0.5), max(p99s), budget))
        print("         (p99 window median=%.0f  worst-window p99=%.0f  worst max=%.0f ms)"
              % (_pct(p99s, 0.5), max(p99s), max(mxs)))
    print("\nP4.4 hint: target the C++ staleness tier at ~1.5x the healthy p99 "
          "(rerun=off, worst-window) -- currently STALE_MS=800 / STALE_PREP_MS=3000.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
