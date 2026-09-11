"""Offline validation for robot/map_change.py -- no robot, no ROS, no recorded depth required.

Run:  python eval/map_change_selftest.py            (prints SELFTEST-OK on success)
      python eval/map_change_selftest.py --ply <p>  (also run cases against a real Aurora .ply)

WHAT IS AND IS NOT PROVEN HERE. These cases drive the manager with SYNTHETIC depth raycast against
a known world, so they prove the detector's logic: that it finds a planted change, localises it,
stays SILENT when nothing changed, and rejects a body-fixed artifact. They do NOT prove behaviour
against real sensor noise -- runs/ is empty and the replay stub still forces latest_depth -> None
(P5.2 unbuilt), so real-depth validation is VERIFY ON ROBOT and is not claimed by this file.

The negative cases are the load-bearing ones. A change detector that cannot stay quiet is worthless:
it would bury every real finding under noise, and the whole point of the ledger is that a reported
change means something.
"""

import argparse
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "robot"))
from map_change import MapChangeManager, update_ledger  # noqa: E402

RES = 0.10
FOV_DEG = 100.0
N_RAYS = 64
RANGE_M = 4.0


# ------------------------------------------------------------------ synthetic world

def box(x0, y0, x1, y1, step=0.05):
    """Filled axis-aligned rectangle of points -- a solid obstacle, not a sparse outline."""
    xs = np.arange(x0, x1 + 1e-9, step)
    ys = np.arange(y0, y1 + 1e-9, step)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


def room():
    """A corridor: two long walls 4 m apart, robot drives up the middle."""
    return np.vstack([box(-1.0, 2.0, 11.0, 2.15), box(-1.0, -2.15, 11.0, -2.0)])


def raycast(world_keys, pose, rng_m=RANGE_M, res=RES):
    """March each ray of the FOV through an occupied-cell set; return (fwd, left) robot-frame hits.

    Deliberately the same occupancy model the detector uses, so these tests exercise the manager's
    comparison logic rather than a disagreement between two different world models."""
    ox, oy, oth = pose
    fwd, left = [], []
    step = res * 0.5
    for i in range(N_RAYS):
        b = math.radians(-FOV_DEG / 2.0 + FOV_DEG * i / (N_RAYS - 1.0))
        ca, sa = math.cos(oth + b), math.sin(oth + b)
        r = 0.25
        while r < rng_m:
            gx = int(round((ox + r * ca) / res))
            gy = int(round((oy + r * sa) / res))
            if (gx, gy) in world_keys:
                # robot frame: +fwd along heading, +left per REP-103
                fwd.append(r * math.cos(b))
                left.append(r * math.sin(b))
                break
            r += step
    return np.array(fwd), np.array(left)


def keys_of(pts, res=RES, solid=0):
    """Occupied-cell set for raycasting.

    `solid` dilates the world before casting, and it MATTERS for the real-map cases. A SLAM .ply is
    a sparse landmark cloud: cast against it raw and rays slip BETWEEN the points of a real wall,
    terminate on something further away, and carve through the wall -- manufacturing false VANISHED
    findings that say more about the test harness than the detector. Real depth is dense and
    terminates at the surface, so the simulated world must be solid even though the PRIOR handed to
    the manager stays sparse and unmodified. (The residual real-world version of this -- genuine
    depth holes on dark or reflective surfaces -- is a documented limitation, not one this harness
    can characterise; see docs/MAP_CHANGE_PLAN.md section 9.)"""
    ks = set((int(round(p[0] / res)), int(round(p[1] / res))) for p in pts)
    for _ in range(int(solid)):
        ks |= set((k[0] + dx, k[1] + dy) for k in list(ks)
                  for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    return ks


def drive(mgr, world_pts, poses, solid=0):
    wk = keys_of(world_pts, solid=solid)
    for i, p in enumerate(poses):
        f, l = raycast(wk, p)
        if len(f):
            mgr.observe(p, f, l, t=1000.0 + i)


def straight_path(n=40, y=0.0, x0=0.0, x1=8.0):
    return [(x0 + (x1 - x0) * i / (n - 1.0), y, 0.0) for i in range(n)]


def arc_path(cx, cy, r=2.0, a0=-70.0, a1=70.0, n=40):
    """Viewpoints on an arc AROUND a point -- gives real parallax on whatever sits at (cx,cy)."""
    out = []
    for i in range(n):
        a = math.radians(a0 + (a1 - a0) * i / (n - 1.0))
        x, y = cx - r * math.cos(a), cy - r * math.sin(a)
        out.append((x, y, math.atan2(cy - y, cx - x)))
    return out


def mk(prior, **kw):
    kw.setdefault("res_m", RES)
    kw.setdefault("range_m", RANGE_M)
    kw.setdefault("min_votes", 6)
    kw.setdefault("min_region_cells", 2)
    return MapChangeManager(prior, map_id="selftest", **kw)


# ------------------------------------------------------------------ cases

FAIL = []


def check(name, ok, detail=""):
    print("  %-46s %s%s" % (name, "PASS" if ok else "FAIL", ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


def case_appeared():
    """A box appears in open floor that the map calls empty."""
    walls = room()
    obj = box(4.0, -0.6, 4.5, 0.0)
    mgr = mk(walls)
    drive(mgr, np.vstack([walls, obj]), arc_path(4.25, -0.3, r=2.2))
    f = mgr.findings()
    app = [r for r in f if r["cls"] == "APPEARED"]
    ok = len(app) >= 1
    near = False
    if app:
        best = min(app, key=lambda r: math.hypot(r["map_xy"][0] - 4.25, r["map_xy"][1] + 0.3))
        near = math.hypot(best["map_xy"][0] - 4.25, best["map_xy"][1] + 0.3) < 0.5
        check("appeared: localised within 0.5 m", near,
              "at %s conf=%.2f parallax=%.0fdeg" % (best["map_xy"], best["conf"],
                                                    best["parallax_deg"]))
    check("appeared: object detected", ok, "%d region(s)" % len(app))
    return ok and near


def case_null():
    """THE LOAD-BEARING CASE. World == map. The manager must emit NOTHING."""
    walls = room()
    mgr = mk(walls)
    drive(mgr, walls, arc_path(4.0, 0.0, r=2.2) + straight_path())
    f = mgr.findings()
    check("null: no change -> zero findings", len(f) == 0,
          "got %d %s" % (len(f), [r["cls"] for r in f[:3]]))
    return len(f) == 0


def case_body_fixed():
    """The phantom-obstacle case: a return at a CONSTANT robot-relative range, every frame.

    This is what self-occlusion by the robot's own body/arm/Aurora rig looks like. In the MAP frame
    it moves with the robot, so it can never accumulate votes in one cell -- it must NOT be a
    finding. If this case ever fails, the parallax gate has stopped working."""
    walls = room()
    mgr = mk(walls)
    wk = keys_of(walls)
    for i, p in enumerate(straight_path(n=50)):
        f, l = raycast(wk, p)
        f = np.append(f, 0.45)        # phantom: always 0.45 m dead ahead, like the real one
        l = np.append(l, 0.0)
        mgr.observe(p, f, l, t=1000.0 + i)
    got = mgr.findings()
    check("body-fixed phantom -> not reported", len(got) == 0,
          "got %d" % len(got))
    return len(got) == 0


def case_vanished():
    """The map says obstacle; the world is now clear and rays pass through the spot."""
    walls = room()
    obj = box(4.0, -0.6, 4.5, 0.0)
    mgr = mk(np.vstack([walls, obj]))
    # Radius must keep the robot INSIDE the corridor -- from outside a wall the rays never reach
    # the spot, so nothing gets carved and there is no evidence of absence to vote on.
    drive(mgr, walls, arc_path(4.25, -0.3, r=1.5))
    f = mgr.findings()
    van = [r for r in f if r["cls"] == mgr.VANISHED]
    ok = len(van) >= 1
    check("vanished: removed object detected", ok, "%d region(s)" % len(van))
    if ok:
        best = min(van, key=lambda r: math.hypot(r["map_xy"][0] - 4.25, r["map_xy"][1] + 0.3))
        check("vanished: localised within 0.6 m",
              math.hypot(best["map_xy"][0] - 4.25, best["map_xy"][1] + 0.3) < 0.6,
              "at %s conf=%.2f" % (best["map_xy"], best["conf"]))
    return ok


def case_ledger():
    """new -> persistent -> resolved, and survives a corrupt file."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ledger.json")
    reg = [{"cls": "APPEARED", "map_xy": [4.2, -0.3], "extent_m": [0.5, 0.6], "cells": 12,
            "votes": 30, "n_views": 4, "parallax_deg": 70.0, "conf": 0.83,
            "nearest_prior_m": 0.9, "first_t": 1.0, "last_t": 2.0, "map": "m"}]
    a = update_ledger(p, "m", RES, reg, run_id="run1", now=1000.0)
    ok1 = len(a["regions"]) == 1 and a["regions"][0]["status"] == "new"
    b = update_ledger(p, "m", RES, reg, run_id="run2", now=2000.0)
    ok2 = b["regions"][0]["status"] == "persistent" and b["regions"][0]["n_runs"] == 2
    c = update_ledger(p, "m", RES, [], run_id="run3", now=3000.0)
    c = update_ledger(p, "m", RES, [], run_id="run4", now=4000.0)
    ok3 = c["regions"][0]["status"] == "resolved"
    check("ledger: new -> persistent -> resolved", ok1 and ok2 and ok3,
          "%s/%s/%s" % (ok1, ok2, ok3))
    with open(p, "w") as f:
        f.write("{not json")
    d2 = update_ledger(p, "m", RES, reg, run_id="run5", now=5000.0)
    check("ledger: corrupt file does not lose the run", len(d2["regions"]) == 1)
    return ok1 and ok2 and ok3 and len(d2["regions"]) == 1


def case_real_ply(path):
    """Same two cases against a REAL Aurora map -- real density, real noise, real outliers."""
    from map_change import MapChangeManager as _M
    pts = []
    with open(path) as f:
        body = False
        for line in f:
            if not body:
                if line.strip() == "end_header":
                    body = True
                continue
            c = line.split()
            if len(c) < 3:
                continue
            z = float(c[2])
            if 0.06 <= z <= 1.0:
                pts.append((float(c[0]), float(c[1])))
    prior = np.array(pts, dtype=np.float64)
    print("  real prior: %d points in the map-assist height band" % len(prior))
    if len(prior) < 100:
        check("real ply: enough band points", False)
        return False
    # REAL VIEWPOINTS. The trajectory CSV beside the .ply is where the robot actually walked, so
    # every pose on it is by construction reachable, open floor, and inside the mapped area --
    # far better than guessing a spot from the point cloud's median, which lands in dense geometry.
    traj = _load_traj(os.path.join(os.path.dirname(path),
                                   os.path.basename(path).replace(".ply", "_traj.csv")))
    if len(traj) < 20:
        check("real ply: trajectory available", False, "%d poses" % len(traj))
        return False
    print("  real trajectory: %d poses" % len(traj))

    mgr = _M(prior, map_id="real", res_m=RES, range_m=RANGE_M, min_votes=6, min_region_cells=2)
    drive(mgr, prior, traj, solid=1)
    n = len(mgr.findings())
    check("real ply: self-consistent on real path -> zero findings", n == 0, "got %d" % n)

    # Plant in genuinely OPEN floor beside the real path, and -- the part the first version of this
    # test got wrong -- only at a spot the real trajectory actually OBSERVES with parallax. A spot
    # the robot walks straight past can never clear the parallax gate, so failing to detect it would
    # say nothing about the detector.
    wk = keys_of(prior, solid=1)
    spot, best_seen = None, (0, 0.0)
    for i in range(2, len(traj) - 2):
        x, y, th = traj[i]
        for side in (1.0, -1.0):
            for off in (0.8, 1.1, 1.5):
                px = x + off * math.cos(th + side * math.pi / 2)
                py = y + off * math.sin(th + side * math.pi / 2)
                if mgr.nearest_prior_m(px, py, max_r_m=0.7) is not None:
                    continue                    # too close to mapped geometry -> not open floor
                views = _views_of(traj, px, py, wk)
                sp = _spread(traj, views, px, py)
                best_seen = max(best_seen, (len(views), sp))
                if len(views) >= 15 and sp >= 60.0:
                    spot = (px, py, i, views)
                    break
            if spot:
                break
        if spot:
            break
    if spot is None:
        # NOT a detector failure: this trajectory never observes any open floor with enough
        # parallax, so the case is unrunnable here rather than failing. Reported, not hidden.
        print("  SKIP  no observable open spot on this path (best: %d poses, %.0f deg parallax)"
              % best_seen)
        return n == 0
    px, py, i0, views = spot
    print("  planted at (%.2f, %.2f): %d observing poses, %.0f deg of parallax"
          % (px, py, len(views), _spread(traj, views, px, py)))
    mgr2 = _M(prior, map_id="real", res_m=RES, range_m=RANGE_M, min_votes=6, min_region_cells=2)
    obj = box(px - 0.2, py - 0.2, px + 0.2, py + 0.2)
    drive(mgr2, np.vstack([prior, obj]), [traj[j] for j in views], solid=1)
    app = [r for r in mgr2.findings() if r["cls"] == "APPEARED"]
    ok = any(math.hypot(r["map_xy"][0] - px, r["map_xy"][1] - py) < 0.6 for r in app)
    check("real ply: planted object detected on real path", ok, "%d region(s)" % len(app))
    return n == 0 and ok


def _views_of(traj, px, py, wk=None, lo=0.6, hi=3.5):
    """Indices of poses that can actually SEE (px,py): in range, inside the depth cone, AND with
    clear line of sight. The occlusion check is not optional -- a spot can sit in open floor and
    still be behind a wall from every pose that happens to point at it, and a detector cannot be
    faulted for missing something the robot never saw."""
    out = []
    for j, (x, y, th) in enumerate(traj):
        d = math.hypot(px - x, py - y)
        if not (lo <= d <= hi):
            continue
        rel = math.degrees(math.atan2(py - y, px - x) - th)
        rel = (rel + 180.0) % 360.0 - 180.0
        if abs(rel) > FOV_DEG / 2.0:
            continue
        if wk is not None and _blocked(x, y, px, py, wk):
            continue
        out.append(j)
    return out


def _blocked(x0, y0, x1, y1, wk, res=RES):
    """March the segment; blocked if it enters an occupied cell before the last 0.25 m."""
    d = math.hypot(x1 - x0, y1 - y0)
    n = max(1, int(d / (res * 0.5)))
    for i in range(1, n):
        t = i / float(n)
        if d * (1.0 - t) < 0.25:
            break
        if (int(round((x0 + (x1 - x0) * t) / res)),
                int(round((y0 + (y1 - y0) * t) / res))) in wk:
            return True
    return False


def _spread(traj, idxs, px, py):
    """Angular spread of those viewpoints AROUND the spot -- the parallax actually available."""
    bs = sorted(math.degrees(math.atan2(traj[j][1] - py, traj[j][0] - px)) % 360.0 for j in idxs)
    if len(bs) < 2:
        return 0.0
    gaps = [bs[i + 1] - bs[i] for i in range(len(bs) - 1)] + [360.0 - bs[-1] + bs[0]]
    return 360.0 - max(gaps)


def _load_traj(path):
    """DATA,<wall_t>,<x>,<y>,<...> -- heading derived from motion, which is more reliable than the
    trailing column and is what the depth cone actually points along."""
    pts = []
    if not os.path.exists(path):
        return []
    with open(path) as f:
        for line in f:
            c = line.strip().split(",")
            if len(c) < 4 or c[0] != "DATA":
                continue
            try:
                pts.append((float(c[2]), float(c[3])))
            except ValueError:
                continue
    out = []
    for i in range(len(pts) - 1):
        dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        if math.hypot(dx, dy) < 0.02:          # stationary -> heading undefined, skip the pose
            continue
        out.append((pts[i][0], pts[i][1], math.atan2(dy, dx)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", default="")
    a = ap.parse_args()
    print("map_change selftest")
    case_appeared()
    case_null()
    case_body_fixed()
    case_vanished()
    case_ledger()
    if a.ply:
        if os.path.exists(a.ply):
            case_real_ply(a.ply)
        else:
            check("real ply: file exists", False, a.ply)
    if FAIL:
        print("SELFTEST-FAIL: %s" % ", ".join(FAIL))
        return 1
    print("SELFTEST-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
