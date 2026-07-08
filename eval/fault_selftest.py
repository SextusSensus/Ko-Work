#!/usr/bin/env python3
"""P4.6 headless selftest: the persistent-fault -> STAND escalator. No robot, no motion.
Constructs a Follower and drives its _on_frame_error handler directly, asserting the escalation:
  - --stand-on-loss (default): each throw stands immediately.
  - --no-stand-on-loss + --fault-stand-k N: throws 1..N-1 only HOLD; throw N FORCES a stand.
  - --fault-stand-k 0: never a forced stand (disabled).
  - a clean frame resets the streak.

Usage:  python fault_selftest.py [--node <follow_person_k1.py>]
"""
import argparse, importlib.util, os, sys

NODE_DEFAULT = "/home/booster/follow_person_k1.py"


def load(node_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


def make(m, argv):
    f = m.Follower(m.parse_args(argv))
    c = {"stand": 0, "hold": 0}
    f._stand = lambda: c.__setitem__("stand", c["stand"] + 1)
    f._hold = lambda: c.__setitem__("hold", c["hold"] + 1)
    return f, c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    m = load(ap.parse_args().node)

    # 1. stand_on_loss ON (default): each throw stands immediately
    f, c = make(m, [])
    f._on_frame_error(Exception("x"))
    assert c == {"stand": 1, "hold": 0}, ("stand-on-loss immediate", c)

    # 2. stand_on_loss OFF + fault_stand_k=3: throws 1,2 HOLD; throw 3 FORCES a stand
    f, c = make(m, ["--no-stand-on-loss", "--fault-stand-k", "3"])
    f._on_frame_error(Exception("x")); f._on_frame_error(Exception("x"))
    assert c == {"stand": 0, "hold": 2}, ("pre-threshold holds", c)
    f._on_frame_error(Exception("x"))
    assert c["stand"] == 1, ("forced stand at k", c)

    # 3. fault_stand_k=0 + stand_on_loss OFF: never a forced stand
    f, c = make(m, ["--no-stand-on-loss", "--fault-stand-k", "0"])
    for _ in range(5):
        f._on_frame_error(Exception("x"))
    assert c == {"stand": 0, "hold": 5}, ("disabled", c)

    # 4. a clean frame resets the streak (as run() does), preventing escalation
    f, c = make(m, ["--no-stand-on-loss", "--fault-stand-k", "3"])
    f._on_frame_error(Exception("x")); f._on_frame_error(Exception("x"))
    f._frame_err_streak = 0
    f._on_frame_error(Exception("x")); f._on_frame_error(Exception("x"))
    assert c["stand"] == 0, ("clean-frame reset prevents escalation", c)

    print("FAULT-SELFTEST-OK (immediate stand-on-loss; forced stand at k; disabled at 0; reset on clean frame)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
