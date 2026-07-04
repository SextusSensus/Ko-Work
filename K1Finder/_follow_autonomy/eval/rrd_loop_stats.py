#!/usr/bin/env python3
"""rrd_loop_stats.py -- extract loop-cost gate evidence from a K1 follow .rrd.

Prints p50/p90/p99/max for the node's self-measured timing series:
  /diag/loop_ms      -- control-loop WORK time per iteration (before the fill-sleep)
  /diag/rr_track_ms  -- cost of the Rerun scalar block inside _track (TRACK frames only)

Run on the LAPTOP (needs rerun-sdk >= 0.33 for the RrdReader API -- the robot's pinned 0.23.1
does not have it; pull the .rrd over first, e.g. with the app's 'Open .rrd' button or scp).

Usage: python rrd_loop_stats.py <recording.rrd> [entity ...]
"""
import sys


def series_from_rrd(path, entities):
    """Return {entity: [float, ...]} for Scalars entities in the .rrd (chunk-stream read --
    works on the legacy footerless files rr.save() writes)."""
    from rerun.experimental import RrdReader
    store = RrdReader(path).stream().collect()
    out = {}
    for ch in store.stream():
        if ch.is_static or ch.is_empty:
            continue
        ep = str(ch.entity_path)
        if ep not in entities:
            continue
        rb = ch.to_record_batch()
        sc = [n for n in rb.schema.names if n.lower().endswith("scalars")]
        if not sc:
            continue
        vals = rb.column(rb.schema.get_field_index(sc[0])).to_pylist()
        d = out.setdefault(ep, [])
        for v in vals:
            if v is None:
                continue
            d.append(float(v[0]) if isinstance(v, (list, tuple)) else float(v))
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    entities = sys.argv[2:] or ["/diag/loop_ms", "/diag/rr_track_ms"]
    try:
        series = series_from_rrd(path, set(entities))
    except ImportError as e:
        raise SystemExit("needs rerun-sdk >= 0.33 on this machine (RrdReader): %s" % e)
    if not series:
        print("no matching Scalars entities found (looked for: %s)" % ", ".join(entities))
        return 1
    import numpy as np
    for ep in entities:
        if ep not in series:
            print("%-20s (absent)" % ep)
            continue
        a = np.array(series[ep])
        print("%-20s n=%-5d p50=%7.2fms  p90=%7.2fms  p99=%7.2fms  max=%8.2fms"
              % (ep, len(a), np.percentile(a, 50), np.percentile(a, 90),
                 np.percentile(a, 99), a.max()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
