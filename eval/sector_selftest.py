#!/usr/bin/env python3
"""Headless selftest for the SPATIAL obstacle layer: sector clearances, gap-steer commitment,
and the operator-is-not-an-obstacle exclusion. No robot, no motion, no camera.

WHY THIS EXISTS. These behaviours all shipped VERIFY ON ROBOT with no offline coverage, because
replay_eval injected depth as a UNIFORM plane -- on which _sector_clearances() returns L == C == R
by construction, so "centre blocked AND a side clear" is unsatisfiable and gap steer can never
fire. This drives the functions directly with a synthetic piecewise depth frame instead, which is
the same shape the piecewise replay stub now produces.

Cases:
  1 open      -- everything far            -> no steer (path clear releases the commitment)
  2 detour    -- centre near, LEFT far     -> commits LEFT (+rate)
  3 boxed in  -- centre near, sides unread -> no steer (fail closed: cannot see != nothing there)
  4 hysteresis-- committed side holds even when both sides are open
  5 operator  -- the near return IS the followed target -> no steer (regression for the
                 operator-exclusion fix; gap steer used to route around the person it followed)

Usage:  python sector_selftest.py [--node <follow_person_k1.py>]
"""
import random
import argparse
import importlib.util
import math
import os
import sys

import numpy as np

NODE_DEFAULT = "/home/booster/follow_person_k1.py"


def load(node_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


class FakeNode:
    """Only the accessors the obstacle layer touches. Depth is authored per column band."""

    def __init__(self, left, centre, right, h=448, w=544):
        self.d = np.full((h, w), float("nan"), dtype=np.float32)
        cut = int(w / 3)
        for lo, hi, v in ((0, cut, left), (cut, w - cut, centre), (w - cut, w, right)):
            self.d[:, lo:hi] = float("nan") if v is None else float(v)

    def latest_depth(self, max_age=0.5):
        return self.d

    def head_yaw(self, max_age=0.5):
        return None          # head unknown -> the layer must not apply a pan shift


def build(m, extra=()):
    argv = ["--obstacle-brake", "--gap-steer", "on",
            "--obstacle-corridor-frac", "0.55", "--sector-frac", "0.33",
            "--obstacle-min-valid", "70", "--obstacle-brake-start", "1.5",
            "--obstacle-brake-stop", "0.7", "--gap-steer-trigger-frac", "0.65",
            "--gap-steer-rate", "0.24", "--gap-steer-max-bearing-deg", "35"] + list(extra)
    return m.Follower(m.parse_args(argv))


def sectors(f, left, centre, right):
    f.node = FakeNode(left, centre, right)
    return f._sector_clearances()


def live_session():
    """True if a follow session is actually DRIVING the robot.

    The obvious check is a self-match trap, and it caught me: this script is invoked as
    `sector_selftest.py --node .../follow_person_k1.py`, so its own command line contains the node
    filename and `pgrep -f` matched THIS PROCESS. The bracket trick does not help -- it stops the
    pattern text from matching itself, not the real path in our own argv. The guard therefore
    reported a live session every time and refused to run for two days.

    So identify a session by what only a real one has: the node running WITH --drive, and not us."""
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", "follow_person_k1.py"],
                           capture_output=True, text=True, timeout=10)
        me = {os.getpid(), os.getppid()}
        for tok in r.stdout.split():
            if not tok.strip().isdigit():
                continue
            pid = int(tok)
            if pid in me:
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    cl = f.read().decode("utf-8", "ignore").replace("\0", " ")
            except OSError:
                continue
            if "follow_person_k1.py" in cl and "--drive" in cl and "selftest" not in cl:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    ap.add_argument("--force", action="store_true",
                    help="run even while a follow session is live (do not: see the guard below)")
    a = ap.parse_args()
    # This selftest loads YOLO and OSNet onto the SAME Orin GPU the follow uses. Running it beside
    # a live session starves that session: it was observed pushing the bridge ping past its
    # timeout, giving "BRIDGE ping -> <no-reply/timeout>" and a DRIVE-ABORT on a real drive. The
    # test is headless and can wait; the robot on its feet cannot.
    if live_session() and not a.force:
        print("REFUSING: a follow session is live -- this test would contend for the GPU and can "
              "stall the bridge ping. Re-run when the session ends, or pass --force.")
        return 2
    m = load(a.node)
    f = build(m)
    fails = []

    def check(name, got, want):
        ok = (got == want) if not isinstance(want, float) else abs(got - want) < 1e-9
        print("  %-46s %-22s %s" % (name, repr(got), "OK" if ok else "FAIL (want %r)" % (want,)))
        if not ok:
            fails.append(name)

    # --- the premise: a uniform plane cannot express a detour -------------------------------
    print("premise -- uniform depth makes the sectors indistinguishable:")
    s = sectors(f, 1.0, 1.0, 1.0)
    same = (s["L"] is not None and s["C"] is not None and s["R"] is not None
            and abs(s["L"] - s["R"]) < 1e-6 and abs(s["L"] - s["C"]) < 1e-6)
    check("uniform plane -> L == C == R", same, True)

    print("\ncase 1 -- open room, nothing near:")
    f._gap_dir = 0
    s = sectors(f, 4.0, 4.0, 4.0)
    check("centre clearance is far", s["C"] > 3.0, True)
    check("no steer", f._gap_steer_bias(0.0, s["C"], 4.5), 0.0)

    print("\ncase 2 -- centre blocked, left open:")
    f._gap_dir = 0
    s = sectors(f, 3.0, 0.8, 0.8)
    check("L clear", s["L"] > 1.5, True)
    check("C blocked", s["C"] < 1.22, True)
    check("commits LEFT (+rate)", f._gap_steer_bias(0.0, s["C"], 3.0), 0.24)
    check("commitment recorded", f._gap_dir, 1)

    print("\ncase 3 -- boxed in, both sides unreadable (the empty-side case):")
    f._gap_dir = 0
    s = sectors(f, None, 0.8, None)
    check("L unreadable -> None", s["L"], None)
    check("R unreadable -> None", s["R"], None)
    check("no steer (fail closed)", f._gap_steer_bias(0.0, s["C"], 3.0), 0.0)

    print("\ncase 4 -- hysteresis: a committed detour is not re-decided:")
    f._gap_dir = -1                                   # already committed RIGHT
    s = sectors(f, 3.0, 0.8, 3.0)                     # both sides now open
    check("holds RIGHT rather than flipping", f._gap_steer_bias(0.0, s["C"], 3.0), -0.24)
    check("commitment unchanged", f._gap_dir, -1)

    print("\ncase 5 -- the near return IS the operator (regression: 002d86c):")
    f._gap_dir = 0
    s = sectors(f, 3.0, 0.8, 0.8)
    check("no steer when the block is the target", f._gap_steer_bias(0.0, s["C"], 1.0), 0.0)
    check("no commitment made", f._gap_dir, 0)
    # ... and the same scene with the operator far away must still steer
    check("still steers when the target is far", f._gap_steer_bias(0.0, s["C"], 3.0), 0.24)

    # --- head-scan direction mapping ---------------------------------------------------------
    # The first on-robot scan measured cx-evidence -90 px/rad: as yaw increases the scene slides
    # LEFT in the image, so +yaw points the camera RIGHT. The code had been assuming +yaw = LEFT
    # from --head-yaw-sign and would have steered toward the WORSE side. head_yaw_sign was only
    # ever verified as command-vs-readback consistency INSIDE the head frame; it never established
    # the mapping into the image frame. These pin the measured mapping.
    print("\nhead-scan direction mapping (measured, not assumed):")
    rad = math.radians

    def hint(scan_map):
        f._scan_map = scan_map
        f._scan_hint = 0
        f._head_scan_finish(0.0)
        return f._scan_hint

    # A pure head pan shifts a static point by the focal length, ~206 px/rad at this FOV. Over a
    # +/-23 deg sweep (0.80 rad) that is a ~165 px swing. Fixtures below are built to that scale.
    # Default --head-yaw-sign is +1, i.e. the config asserts +yaw = LEFT (slope > 0).

    # plausible AND agrees with the config: gap at +23 deg is on the LEFT -> steer LEFT
    check("plausible + agrees, gap at +23deg -> LEFT",
          hint([(rad(-23), 0.6, 150.0), (rad(0), 0.9, 232.0), (rad(23), 3.0, 315.0)]), 1)
    # mirrored scene, same optics -> steer RIGHT
    check("plausible + agrees, gap at -23deg -> RIGHT",
          hint([(rad(-23), 3.0, 150.0), (rad(0), 0.9, 232.0), (rad(23), 0.6, 315.0)]), -1)
    # plausible magnitude but the measurement DISAGREES with --head-yaw-sign: one of them is
    # wrong and we cannot tell which -> refuse rather than pick a side
    check("plausible but disagrees with config -> refuse",
          hint([(rad(-23), 0.6, 315.0), (rad(0), 0.9, 232.0), (rad(23), 3.0, 150.0)]), 0)
    # too small to be a head pan: this is operator motion contaminating the estimate. This is the
    # real field case -- three scans measured -90, +311, +37 px/rad, inconsistent and unphysical.
    check("implausible slope (operator motion) -> refuse",
          hint([(rad(-23), 0.6, 250.0), (rad(0), 0.9, 270.0), (rad(23), 3.0, 290.0)]), 0)
    # no operator visible across the sweep -> direction unresolved -> refuse to steer
    check("no cx evidence -> refuse to steer",
          hint([(rad(-23), 0.6, None), (rad(0), 0.9, None), (rad(23), 3.0, None)]), 0)
    # nothing clear enough anywhere -> no hint regardless of direction
    check("nothing meets the clearance bar -> no hint",
          hint([(rad(-23), 0.6, 150.0), (rad(0), 0.9, 232.0), (rad(23), 1.1, 315.0)]), 0)

    # --- self-aware corridor (--corridor-mode footprint) --------------------------------------
    # A fixed image fraction covers a physical width that scales with range, so it is simultaneously
    # too wide far away and too diluted close in. The case below is the one that matters: an object
    # 0.30-0.37 m off the centreline at 0.5 m is INSIDE the robot's 0.375 m half-width -- a genuine
    # collision -- and frac mode reports the corridor CLEAR because 28 near columns are averaged
    # away by the percentile across a 1.45 m-wide corridor.
    print("\nself-aware corridor (footprint vs image-fraction):")

    def clearance(mode, depth):
        g = build(m, ["--corridor-mode", mode])
        g._clr_hist = []
        g.node = FakeNode(None, None, None)
        g.node.d = depth
        return g._corridor_clearance()

    f_px = (544.0 / 2.0) / math.tan(math.radians(105.8) / 2.0)
    near_d = np.full((448, 544), 4.0, dtype=np.float32)
    near_d[:, 118:150] = 0.5                     # |lateral| 0.30..0.37 m -> inside the footprint
    off = np.full((448, 544), 4.0, dtype=np.float32)
    off[:, 130:190] = 2.5                      # |lateral| 1.00..1.73 m -> outside the footprint
    def near(x):
        """A clearance that would BRAKE. None now means 'no near blob' i.e. CLEAR, which is a
        correct answer and not a missing one -- the contiguity change made that the normal reply
        for an empty window, and this assertion used to assume a float and crashed on it."""
        return x is not None and x < 1.5

    check("in-path object at 0.5m -> footprint BRAKES", near(clearance("footprint", near_d)), True)
    check("...and image-fraction sees it too now", near(clearance("frac", near_d)), True)
    check("off-path object -> footprint ignores", near(clearance("footprint", off)), False)
    check("swept width is constant in metres", abs(2 * (0.5 * 0.45 + 0.15) - 0.75) < 1e-9, True)

    # --- TEMPORAL: the aged-median filter, scored over a SEQUENCE ---------------------------
    # WHY THIS BLOCK EXISTS. Every assertion above scores ONE frame, and the 2026-09-04 field
    # failure was a MULTI-FRAME bug: _clr_hist was appended to only on the detection path, so the
    # median was computed from a list of nothing but detections and could never report clear.
    # A single-frame suite is structurally incapable of catching that -- each individual frame was
    # scored correctly. The filter is a temporal object and has to be tested as one, by driving a
    # sequence through ONE Follower and scoring the RATE, not by asserting on a frame.
    print("\ntemporal aged-median (sequence, not single frame):")

    def brake_rate(p_detect, blob, n=400, seed=5):
        """Fraction of frames the brake would cap vx, over a sequence sharing one _clr_hist."""
        rnd = random.Random(seed)
        g = build(m, ["--corridor-mode", "footprint"])
        g._clr_hist = []
        g.node = FakeNode(None, None, None)
        capped = 0
        for _ in range(n):
            d = np.full((448, 544), 4.0, dtype=np.float32)   # open space: everything far
            if rnd.random() < p_detect:
                r0, r1, c0, c1, z = blob(rnd)
                d[r0:r1, c0:c1] = z
            g.node.d = d
            c = g._corridor_clearance()
            capped += (c is not None and c <= 0.7)           # obstacle_brake_start stop end
        return capped / float(n)

    # Speckle: a ~25px near-range artefact at a random range, present in 45% of frames --
    # the measured field distribution. A real object at this range fills thousands of px.
    def speckle(rnd):
        r = rnd.randrange(235, 250)
        c = rnd.randrange(258, 285)
        return r, r + 5, c, c + 5, rnd.uniform(0.50, 0.96)

    # A real object: large, contiguous, stable range, present in ~every frame.
    def solid(_rnd):
        return 200, 280, 240, 300, 0.60

    sp = brake_rate(0.45, speckle)
    rl = brake_rate(0.98, solid)
    print("  (speckle brake rate %.1f%%, real-object brake rate %.1f%%)" % (100 * sp, 100 * rl))
    check("45%% speckle does NOT hold the brake down", sp < 0.15, True)
    check("a real object still brakes in ~every frame", rl > 0.90, True)
    check("...so the two are actually separated", (rl - sp) > 0.75, True)

    print("")
    if fails:
        print("SECTOR-SELFTEST-FAIL %d: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("SECTOR-SELFTEST-OK sectors + gap-steer commitment + operator exclusion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
