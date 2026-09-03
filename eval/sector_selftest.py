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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    a = ap.parse_args()
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

    print("")
    if fails:
        print("SECTOR-SELFTEST-FAIL %d: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("SECTOR-SELFTEST-OK sectors + gap-steer commitment + operator exclusion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
