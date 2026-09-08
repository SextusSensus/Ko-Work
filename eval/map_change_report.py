"""Render the map-change ledger into a readable report.

    python eval/map_change_report.py map_changes/drive_20260908_104304.json
    python eval/map_change_report.py <ledger> --md > report.md

This is the layer a human or an agent reads. The detector itself (robot/map_change.py) is pure
numerics and never calls a model; this turns its accumulated ledger into prose-adjacent output so a
reporting agent has something structured to narrate rather than raw cells.

Everything here is READ-ONLY and advisory. Nothing in this file, and nothing downstream of it,
feeds back into the robot's obstacle decisions -- map-assist stays the primary and its min()-only
reinforcement is untouched. See docs/MAP_CHANGE_PLAN.md section 2.
"""

import argparse
import json
import sys
import time

CLS_NOTE = {
    "APPEARED": "something is there that the map does not have",
    "VANISHED": "the map has something the world no longer shows (REPORT ONLY -- never acted on)",
}


def age(ts, now):
    d = max(0.0, now - float(ts))
    if d < 3600:
        return "%dm ago" % int(d / 60)
    if d < 86400:
        return "%.1fh ago" % (d / 3600.0)
    return "%.1fd ago" % (d / 86400.0)


def render(led, md=False):
    now = time.time()
    regs = led.get("regions", [])
    out = []
    h = "# Map change report" if md else "MAP CHANGE REPORT"
    out.append(h)
    out.append("")
    by = {}
    for r in regs:
        by[r.get("status", "?")] = by.get(r.get("status", "?"), 0) + 1
    out.append("map: %s   resolution: %.2f m   regions: %d  (%s)"
               % (led.get("map_id", "?"), float(led.get("res_m", 0.1)), len(regs),
                  ", ".join("%s %d" % (k, v) for k, v in sorted(by.items())) or "none"))
    out.append("")

    live = [r for r in regs if r.get("status") != "resolved"]
    if not live:
        out.append("No open findings. The world matches the map everywhere it was observed.")
        return "\n".join(out)

    live.sort(key=lambda r: (-int(r.get("n_runs", 0)),
                             -max((o.get("conf", 0) for o in r.get("observations", [])),
                                  default=0)))
    for r in live:
        conf = max((o.get("conf", 0) for o in r.get("observations", [])), default=0.0)
        x, y = r.get("map_xy", [0, 0])
        ex = r.get("extent_m", [0, 0])
        title = "%s at (%.2f, %.2f)" % (r.get("cls", "?"), x, y)
        out.append(("## " + title) if md else ("- " + title))
        out.append("    %s" % CLS_NOTE.get(r.get("cls"), ""))
        out.append("    size ~%.2f x %.2f m | confidence %.2f | seen in %d run(s) | %s"
                   % (ex[0], ex[1], conf, int(r.get("n_runs", 0)), r.get("status", "?")))
        np_ = r.get("nearest_prior_m")
        if np_ is not None:
            out.append("    nearest mapped geometry: %.2f m" % float(np_))
        out.append("    first seen %s, last seen %s"
                   % (age(r.get("first_seen", now), now), age(r.get("last_seen", now), now)))
        obs = r.get("observations", [])
        if len(obs) > 1:
            out.append("    corroboration: %s"
                       % ", ".join("%s(conf %.2f)" % (o.get("run_id", "?")[:16], o.get("conf", 0))
                                   for o in obs[-4:]))
        out.append("")

    res = [r for r in regs if r.get("status") == "resolved"]
    if res:
        out.append(("## Resolved" if md else "RESOLVED (no longer confirmed)"))
        for r in res:
            out.append("    %s at (%.2f, %.2f), last seen %s"
                       % (r.get("cls"), r["map_xy"][0], r["map_xy"][1],
                          age(r.get("last_seen", now), now)))
    out.append("")
    out.append("Confidence is an ORDERING HEURISTIC, not a calibrated probability.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ledger")
    ap.add_argument("--md", action="store_true", help="markdown headings")
    a = ap.parse_args()
    with open(a.ledger) as f:
        led = json.load(f)
    print(render(led, a.md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
