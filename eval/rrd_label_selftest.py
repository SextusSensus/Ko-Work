#!/usr/bin/env python3
"""Headless selftest for rrd_label.py's RGB <-> depth pairing and the gate's depth-pairing check.
No .rrd, no models, no GPU: drives pair_depth() and validate_geometry() directly.

WHY THIS EXISTS. rrd_label paired every labelled RGB frame with depth by frame_idx and no age limit, so
a frame could take a FUTURE depth frame (before the first depth, or because depth's frame_idx is only
the RGB seq current when it arrived) or one from seconds earlier -- a different view after a yaw or a
camera stall. The floor/wall checks cannot see that (a floor looks the same from any heading).

Cases:
  1 pairing  -- the newest depth that arrived no later than the RGB frame; never a later one; the age
                bound is inclusive at DEPTH_MAX_AGE_S and rejects past it; no prior depth -> none
  2 contract -- DEPTH_MAX_AGE_S equals the node's robot/common.py DEPTH_FRESH_S (read with ast)
  3 gate     -- clean geometry + enough pairing -> PASS; the same geometry with pairing below
                MIN_DEPTH_PAIRED_FRAC -> INCONCLUSIVE (never PASS); no counts -> INCONCLUSIVE; a failed
                check still FAILs; gate_version 3; the report stays strict JSON

Usage:  python rrd_label_selftest.py
"""
import ast
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rrd_label as L  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok  " + msg)


# ---- 1 pairing (binary-exact stamps, so the bound is tested exactly) ----
dwall, dkeys = [10.0, 10.5, 11.25], [100, 105, 115]
check(L.pair_depth(10.5, dwall, dkeys) == (105, 0.0), "a depth stamped at the same instant pairs")
check(L.pair_depth(10.75, dwall, dkeys) == (105, 0.25), "the newest EARLIER depth pairs, never the later one")
check(L.pair_depth(9.75, dwall, dkeys) == (None, None), "no depth before the frame -> none (not the first depth)")
check(L.pair_depth(11.0, dwall, dkeys) == (105, 0.5), "age exactly DEPTH_MAX_AGE_S is accepted")
check(L.pair_depth(11.125, dwall, dkeys) == (None, 0.625), "older than DEPTH_MAX_AGE_S is rejected, age reported")
check(L.pair_depth(None, dwall, dkeys) == (None, None), "an unstamped RGB frame gets no depth")
check(L.pair_depth(10.5, [], []) == (None, None), "no depth at all -> none")

# ---- 2 the bound is the node's own contract ----
src = open(os.path.join(HERE, "..", "robot", "common.py"), encoding="utf-8").read()
fresh = [ast.literal_eval(n.value) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
         and any(getattr(t, "id", None) == "DEPTH_FRESH_S" for t in n.targets)]
check(fresh == [L.DEPTH_MAX_AGE_S], "DEPTH_MAX_AGE_S == robot/common.py DEPTH_FRESH_S (%s)" % fresh)

# ---- 3 the gate ----
rng = np.random.default_rng(0)
H = 0.99


def clean_geometry(h=H, n=2000):
    """A flat floor at -h and a wall standing on it, as [X right, Z forward, Yup] points."""
    L._VAL["floor"].clear()
    L._VAL["wall"].clear()
    X, Z = rng.uniform(-1.5, 1.5, n), rng.uniform(0.8, 4.0, n)
    L._VAL["floor"].append(np.stack([X, Z, -h + rng.normal(0.0, 0.01, n)], axis=1))
    L._VAL["wall"].append(np.stack([X, np.full(n, 4.0), rng.uniform(-h + 0.02, 1.5, n)], axis=1))


def counts(paired, n=100):
    return {"labelled_frames": n, "paired": paired, "dropped_too_old": n - paired,
            "dropped_no_prior_depth": 0, "max_age_s": L.DEPTH_MAX_AGE_S}


clean_geometry()
v = L.validate_geometry(H, counts(92))
check(v["gate_version"] == 3, "gate_version is 3")
check(v["status"] == "PASS", "clean geometry + 92%% paired -> PASS (got %s)" % v["status"])
check(v["checks"]["depth_pairing"]["frac_paired"] == 0.92, "the paired fraction is recorded")
json.dumps(v, allow_nan=False)

clean_geometry()
v = L.validate_geometry(H, counts(84))
check(v["status"] == "INCONCLUSIVE" and v["checks"]["depth_pairing"]["status"] == "INCONCLUSIVE",
      "the same geometry with 84% paired -> INCONCLUSIVE, never PASS")
check(all(v["checks"][k]["status"] == "PASS" for k in ("floor", "scale", "walls")),
      "... while every geometry check passed (the gate could not have caught it)")

clean_geometry()
v = L.validate_geometry(H)
check(v["status"] == "INCONCLUSIVE" and v["checks"]["depth_pairing"]["status"] == "SKIP",
      "no pairing counts -> SKIP -> INCONCLUSIVE (fail-closed)")

clean_geometry()
v = L.validate_geometry(0.5, counts(84))
check(v["status"] == "FAIL", "a failed check (camera height 0.5 m) still FAILs when pairing is also sparse")
json.dumps(v, allow_nan=False)

print("RRD-LABEL-SELFTEST-OK")
