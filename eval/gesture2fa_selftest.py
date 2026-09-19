#!/usr/bin/env python3
"""Headless selftest for the crowd 2FA lock (2026-09-11).

Covers, with a FAKE pose model and a controllable clock (no robot, no camera, no motion):
  S1  step spec parsing refuses empty / unknown / repeated steps
  S2  finger counting on synthetic 21-landmark hands (open 5, fist 0, three 3, low-conf None)
  S3  the body sequence R_UP,BOTH_UP,L_UP,DOWN by ONE body locks, and only on completion
  S4  wrong order never locks; after the gap reset a correct sequence still works
  S5  two bodies completing together are refused, and nothing lingers
  S6  a sequence slower than --gesture-2fa-total-s resets
  S7  the finger countdown F5..F0 locks with a hand counter; a skipped count never locks;
      countdown steps without a hand model disable the trigger (fail-closed)
  S8  a non-stationary state runs nothing and clears every partial sequence
  N1  node defaults are inert (switch floors 0, aruco trigger); --profile crowd loads; a
      finger-step config without --hand-model is REFUSED; auto re-lock with gesture2fa is REFUSED
  N3  end-to-end through Follower._process_frame: the sequence seeds, a stranger copying it after a
      loss is refused by identity, the operator re-seeds
  N2  _associate: with --switch-anchor-floor a stranger on another tracker id below the floor is
      NOT followed when the bound body is absent; the bound body itself keeps the normal floor;
      floor 0 reproduces the old behaviour (documents the hole)

Usage:  python eval/gesture2fa_selftest.py [--node robot/follow_person_k1.py]
"""
import argparse
import importlib.util
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NODE_DEFAULT = os.path.join(ROOT, "robot", "follow_person_k1.py")
sys.path.insert(0, os.path.join(ROOT, "robot"))


def _install_ros_stubs():
    if "rclpy" in sys.modules:
        return
    rclpy = types.ModuleType("rclpy")
    rclpy.node = types.ModuleType("rclpy.node"); rclpy.executors = types.ModuleType("rclpy.executors")
    rclpy.qos = types.ModuleType("rclpy.qos")
    rclpy.node.Node = type("Node", (), {})
    rclpy.executors.SingleThreadedExecutor = type("SingleThreadedExecutor", (), {})
    rclpy.qos.QoSProfile = type("QoSProfile", (), {"__init__": lambda self, *a, **k: None})
    rclpy.qos.ReliabilityPolicy = type("ReliabilityPolicy", (), {"BEST_EFFORT": 1, "RELIABLE": 2})
    rclpy.qos.HistoryPolicy = type("HistoryPolicy", (), {"KEEP_LAST": 1})
    rclpy.qos.DurabilityPolicy = type("DurabilityPolicy", (), {"VOLATILE": 1})
    rclpy.init = lambda *a, **k: None; rclpy.shutdown = lambda *a, **k: None
    for n, m in (("rclpy", rclpy), ("rclpy.node", rclpy.node), ("rclpy.executors", rclpy.executors),
                 ("rclpy.qos", rclpy.qos)):
        sys.modules[n] = m
    sm = types.ModuleType("sensor_msgs"); sm.msg = types.ModuleType("sensor_msgs.msg")
    sm.msg.Image = type("Image", (), {})
    sys.modules["sensor_msgs"] = sm; sys.modules["sensor_msgs.msg"] = sm.msg


_install_ros_stubs()
import common          # noqa: E402
import triggers        # noqa: E402
import handcount       # noqa: E402

common.log = lambda *a, **k: None
triggers.log = lambda *a, **k: None
handcount.log = lambda *a, **k: None

FAILS = []


def check(name, cond, detail=""):
    print("  %-70s %s" % (name, "OK" if cond else "FAIL " + str(detail)))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- fakes
class Clock:
    t = 1000.0

    @classmethod
    def monotonic(cls):
        return cls.t

    @classmethod
    def tick(cls, dt=0.1):
        cls.t += dt
        return cls.t

    perf_counter = monotonic


class FakeKpts:
    def __init__(self, data):
        self.data = data


class FakeBoxes:
    def __init__(self, xyxy):
        self.xyxy = xyxy


class FakeResult:
    def __init__(self, kd, boxes):
        self.keypoints = FakeKpts(kd)
        self.boxes = FakeBoxes(boxes)


class FakePose:
    """Returns the poses scripted for the NEXT predict() call."""

    def __init__(self):
        self.next = []      # list of (17x3 array, box)

    def predict(self, frame, verbose=False):
        if not self.next:
            return [FakeResult(np.zeros((0, 17, 3)), np.zeros((0, 4)))]
        kd = np.stack([k for k, _ in self.next]); bx = np.array([b for _, b in self.next], dtype=float)
        return [FakeResult(kd, bx)]


class FakeHand:
    ok = True

    def __init__(self):
        self.n = None

    def count(self, frame, wrist_xy, sw):
        return self.n


def person(tid, x1, y1=100.0, w=80.0, h=200.0):
    return {"box": (x1, y1, x1 + w, y1 + h), "cx": x1 + w / 2, "cy": y1 + h / 2, "w": w, "h": h,
            "conf": 0.9, "track_id": tid}


def pose_for(p, l_up=False, r_up=False):
    """17x3 keypoints for a person box: shoulders at 25% height, wrists up (10%) or down (60%)."""
    x1, y1, x2, y2 = p["box"]; w = x2 - x1; h = y2 - y1
    k = np.zeros((17, 3), dtype=float)
    k[common.KP_L_SHOULDER] = (x1 + 0.35 * w, y1 + 0.25 * h, 0.9)
    k[common.KP_R_SHOULDER] = (x1 + 0.65 * w, y1 + 0.25 * h, 0.9)
    k[common.KP_L_WRIST] = (x1 + 0.35 * w, y1 + (0.08 if l_up else 0.6) * h, 0.9)
    k[common.KP_R_WRIST] = (x1 + 0.65 * w, y1 + (0.08 if r_up else 0.6) * h, 0.9)
    return k


STEP_POSE = {"R_UP": dict(r_up=True), "L_UP": dict(l_up=True), "BOTH_UP": dict(l_up=True, r_up=True),
             "DOWN": dict(), "ONE_UP": dict(r_up=True)}


def make_args(**over):
    a = types.SimpleNamespace(
        gesture_kp_conf=0.35, gesture_kp_margin_frac=0.05, iou_min=0.2, gesture_owner_margin=0.15,
        gesture_debug=False, gesture_hold_floor=3, gesture_dwell_s=0.0, gesture_hold=6,
        gesture_2fa_steps="R_UP,BOTH_UP,L_UP,DOWN", gesture_2fa_step_s=0.25, gesture_2fa_gap_s=1.0,
        gesture_2fa_total_s=8.0, gesture_every_n=1)
    for k, v in over.items():
        setattr(a, k, v)
    return a


def make_trigger(hand=None, **over):
    fake = FakePose()
    t = triggers.SequenceGestureTrigger("fake.onnx", make_args(**over), every_n=1, hand=hand,
                                        _model_override=fake)
    return t, fake


FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def run_frames(t, fake, persons, poses, n, state=common.S_SEARCH, hand=None, counts=None, dt=0.1):
    """Feed n frames of the given poses; return the list of hints (None or LockHint)."""
    out = []
    for i in range(n):
        fake.next = poses
        if hand is not None and counts is not None:
            hand.n = counts
        out.append(t.detect(FRAME, persons, state, 640, 480))
        Clock.tick(dt)
    return out


def seq_frames(t, fake, p, steps, per=4, state=common.S_SEARCH, hand=None, counts=None):
    hints = []
    for s, c in zip(steps, counts or [None] * len(steps)):
        pose = pose_for(p, **STEP_POSE.get(s, dict(r_up=True)))
        hints += run_frames(t, fake, [p], [(pose, p["box"])], per, state=state, hand=hand, counts=c)
    return hints


# ---------------------------------------------------------------- tests
def test_steps():
    print("S1 step spec")
    ok = True
    for bad in ("", "R_UP", "R_UP,FOO", "R_UP,R_UP", "F5,F5,F4"):
        try:
            triggers.parse_2fa_steps(bad); ok = False
        except ValueError:
            pass
    check("bad specs refused", ok)
    check("default parses", triggers.parse_2fa_steps(triggers.DEFAULT_2FA_STEPS) == ["R_UP", "BOTH_UP", "L_UP", "DOWN"])
    check("countdown needs hand", triggers.needs_hand_model(triggers.parse_2fa_steps(triggers.COUNTDOWN_2FA_STEPS)))
    check("body steps need no hand", not triggers.needs_hand_model(["R_UP", "DOWN"]))


def hand_landmarks(extended):
    """Synthetic upright hand: wrist at (100,200), palm 60 px. extended = set of finger names."""
    lm = np.zeros((21, 3)); lm[:, 2] = 0.9
    lm[handcount.WRIST] = (100, 200, 0.9)
    cols = {"index": 80, "middle": 95, "ring": 110, "pinky": 125}
    idx = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12), "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}
    for f, (mcp, pip, dip, tip) in idx.items():
        x = cols[f]
        lm[mcp] = (x, 150, 0.9); lm[pip] = (x, 130, 0.9)
        if f in extended:
            lm[dip] = (x, 112, 0.9); lm[tip] = (x, 95, 0.9)
        else:
            lm[dip] = (x, 140, 0.9); lm[tip] = (x, 152, 0.9)      # curled back toward the palm
    lm[handcount.THUMB_CMC] = (75, 185, 0.9); lm[handcount.THUMB_MCP] = (60, 170, 0.9)
    if "thumb" in extended:
        lm[handcount.THUMB_IP] = (42, 158, 0.9); lm[handcount.THUMB_TIP] = (25, 148, 0.9)
    else:
        lm[handcount.THUMB_IP] = (72, 160, 0.9); lm[handcount.THUMB_TIP] = (85, 152, 0.9)   # across the palm
    return lm


def test_fingers():
    print("S2 finger counting")
    check("open hand = 5", handcount.count_fingers(hand_landmarks({"thumb", "index", "middle", "ring", "pinky"})) == 5)
    check("fist = 0", handcount.count_fingers(hand_landmarks(set())) == 0)
    check("three = 3", handcount.count_fingers(hand_landmarks({"index", "middle", "ring"})) == 3)
    check("one = 1", handcount.count_fingers(hand_landmarks({"index"})) == 1)
    lo = hand_landmarks({"index"}); lo[handcount.INDEX_TIP][2] = 0.1
    check("low-conf landmark -> None", handcount.count_fingers(lo) is None)
    check("short array -> None", handcount.count_fingers(np.zeros((5, 3))) is None)
    hc = handcount.HandCounter("", _model_override=None)
    check("no model file -> ok False, count None", (not hc.ok) and hc.count(FRAME, (100, 100), 40) is None)


def test_body_sequence():
    print("S3 body sequence locks only on completion")
    t, fake = make_trigger()
    p = person(7, 200)
    hints = seq_frames(t, fake, p, ["R_UP", "BOTH_UP", "L_UP", "DOWN"])
    locks = [h for h in hints if h is not None]
    check("exactly one lock hint", len(locks) == 1, len(locks))
    check("hint names the body that did it", locks and locks[0].owner_tid == 7 and locks[0].point_kind == "gesture")
    check("no hint before the last step", all(h is None for h in hints[:12]))
    check("sequence state cleared after lock", 7 not in t._seq)


def test_wrong_order():
    print("S4 wrong order never locks; recovers after reset")
    t, fake = make_trigger()
    p = person(7, 200)
    hints = seq_frames(t, fake, p, ["R_UP", "L_UP", "BOTH_UP", "DOWN"], per=6)
    check("wrong order -> no lock", all(h is None for h in hints))
    Clock.tick(3.0)                                     # let any partial state time out
    run_frames(t, fake, [p], [(pose_for(p), p["box"])], 2)
    hints = seq_frames(t, fake, p, ["R_UP", "BOTH_UP", "L_UP", "DOWN"])
    check("correct sequence after reset locks", sum(h is not None for h in hints) == 1)


def test_two_bodies():
    print("S5 two bodies together are refused")
    t, fake = make_trigger()
    p7, p9 = person(7, 100), person(9, 400)
    hints = []
    for s in ["R_UP", "BOTH_UP", "L_UP", "DOWN"]:
        poses = [(pose_for(p7, **STEP_POSE[s]), p7["box"]), (pose_for(p9, **STEP_POSE[s]), p9["box"])]
        hints += run_frames(t, fake, [p7, p9], poses, 4)
    check("simultaneous completion -> refused", all(h is None for h in hints))
    check("nothing lingers", not t._seq)
    hints = seq_frames(t, fake, p7, ["R_UP", "BOTH_UP", "L_UP", "DOWN"])
    check("a solo sequence afterwards locks", sum(h is not None for h in hints) == 1)


def test_timeout():
    print("S6 total timeout resets")
    t, fake = make_trigger(gesture_2fa_total_s=2.0, gesture_2fa_gap_s=5.0)
    p = person(7, 200)
    h1 = seq_frames(t, fake, p, ["R_UP", "BOTH_UP"], per=4)      # 0.8 s
    Clock.tick(2.0)                                             # 2.8 s since start > total 2.0
    h2 = seq_frames(t, fake, p, ["L_UP", "DOWN"], per=4)
    check("too slow -> no lock", all(h is None for h in h1 + h2))


def test_countdown():
    print("S7 finger countdown")
    hand = FakeHand()
    t, fake = make_trigger(hand=hand, gesture_2fa_steps="F5,F4,F3,F2,F1,F0")
    p = person(7, 200)
    check("trigger enabled with a hand counter", t.ok)
    hints = seq_frames(t, fake, p, ["ONE_UP"] * 6, hand=hand, counts=[5, 4, 3, 2, 1, 0])
    check("5-4-3-2-1-fist locks once", sum(h is not None for h in hints) == 1)
    t, fake = make_trigger(hand=hand, gesture_2fa_steps="F5,F4,F3,F2,F1,F0", gesture_2fa_gap_s=0.5)
    hints = seq_frames(t, fake, p, ["ONE_UP"] * 5, hand=hand, counts=[5, 3, 2, 1, 0])
    check("skipping 4 -> no lock", all(h is None for h in hints))
    t, fake = make_trigger(hand=hand, gesture_2fa_steps="F5,F4,F3,F2,F1,F0")
    hints = seq_frames(t, fake, p, ["BOTH_UP"] * 6, hand=hand, counts=[5, 4, 3, 2, 1, 0])
    check("both hands up is not a countdown hand", all(h is None for h in hints))
    t, fake = make_trigger(hand=None, gesture_2fa_steps="F5,F4,F3,F2,F1,F0")
    check("countdown without a hand model -> trigger disabled", not t.ok)
    check("disabled trigger returns nothing", t.detect(FRAME, [p], common.S_SEARCH, 640, 480) is None)


def test_state_gate():
    print("S8 state gate")
    t, fake = make_trigger()
    p = person(7, 200)
    seq_frames(t, fake, p, ["R_UP", "BOTH_UP"])
    check("partial sequence in progress", 7 in t._seq)
    h = run_frames(t, fake, [p], [(pose_for(p, l_up=True), p["box"])], 3, state=common.S_TRACK)
    check("TRACK runs nothing", all(x is None for x in h) and not t.ran_inference)
    check("TRACK clears partial sequences", not t._seq)


# ---------------------------------------------------------------- node-level
def load_node(path):
    spec = importlib.util.spec_from_file_location("follow_person_k1", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


def test_node_config(m):
    print("N1 node config")
    d = vars(m.parse_args([]))
    check("defaults inert", d["switch_anchor_floor"] == 0.0 and d["switch_anchor_frames"] == 0
          and d["lock_trigger"] == "aruco" and d["hand_model"] == "")
    c = vars(m.parse_args(["--profile", "crowd"]))
    check("crowd profile", c["lock_trigger"] == "gesture2fa" and c["switch_anchor_floor"] > 0
          and c["auto_reacquire"] is False and c["arm_reacquire"] is False and c["search_on_loss"] is False)
    for argv, name in ((["--lock-trigger", "gesture2fa", "--gesture-2fa-steps", "F5,F4,F3,F2,F1,F0"], "finger steps w/o hand model refused"),
                       (["--lock-trigger", "gesture2fa", "--gesture-2fa-steps", "R_UP,R_UP"], "bad steps refused"),
                       (["--profile", "crowd", "--auto-reacquire"], "auto re-lock with gesture2fa refused")):
        try:
            m.parse_args(argv); check(name, False, "accepted")
        except SystemExit:
            check(name, True)


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def unit(v):
    v = np.asarray(v, dtype=float); return v / (np.linalg.norm(v) + 1e-9)


def feat_with_sim(anchor, s):
    """A unit vector whose cosine with `anchor` is s."""
    o = unit(np.array([anchor[1], -anchor[0]] + [0.0] * (len(anchor) - 2)))
    return unit(s * anchor + np.sqrt(max(0.0, 1 - s * s)) * o)


def assoc_harness(m, argv):
    a = m.parse_args(argv)
    f = object.__new__(m.Follower)
    f.a = a
    anchor = unit(np.array([1.0, 0.2, 0.1, 0.05]))
    f.seed = m.Seed((300, 100, 380, 300), (340, 200), anchor, 0.9)
    f.seed.track_id = 1
    f.seed.gallery = None
    f.sim_fn = cos
    f.feat_fn = lambda frame, box: None
    f.tracker = object()
    return f, anchor


def test_switch_floor(m):
    print("N2 _associate switch floor")
    base = ["--appearance", "osnet", "--anchor-floor", "0.35"]
    for floor, expect_followed, name in ((0.0, True, "floor 0: stranger at 0.45 is followed (old hole)"),
                                         (0.55, False, "floor 0.55: stranger at 0.45 on another id NOT followed")):
        f, anchor = assoc_harness(m, base + ["--switch-anchor-floor", str(floor)])
        stranger = person(2, 300); stranger["_ph"] = feat_with_sim(anchor, 0.45)
        best, cost, sim, second, hist, locked = f._associate(FRAME, [stranger], 640, 480)
        check(name, (best is not None) == expect_followed)
    f, anchor = assoc_harness(m, base + ["--switch-anchor-floor", "0.55"])
    bound = person(1, 300); bound["_ph"] = feat_with_sim(anchor, 0.40)
    best, *_ = f._associate(FRAME, [bound], 640, 480)
    check("bound id at 0.40 keeps the normal floor (turned operator not dropped)", best is not None and best["track_id"] == 1)
    other = person(2, 100); other["_ph"] = feat_with_sim(anchor, 0.80)
    best, *_ = f._associate(FRAME, [bound, other], 640, 480)
    check("bound id preferred over a strong other id", best is not None and best["track_id"] == 1)
    best, *_ = f._associate(FRAME, [other], 640, 480)
    check("another id at 0.80 (>= floor) may be followed", best is not None and best["track_id"] == 2)


# ---------------------------------------------------------------- end-to-end (FSM)
def test_e2e(m):
    """N3: the whole acquisition path in the real Follower._process_frame -- trigger -> hint ->
    --seed-frames streak -> _try_seed -> _seed_from -> TRACK -- then loss -> REACQUIRE, a stranger
    who copies the sequence is refused by identity, and the operator re-seeds. Synthetic frames
    (operator painted red, stranger blue) so the default HS appearance separates them; a fake pose
    model scripts the gestures; the replay harness supplies the stub node and a fake clock."""
    print("N3 end-to-end through Follower._process_frame")
    import copy
    import time as _t
    sys.path.insert(0, HERE)
    import replay_eval as rv
    clock = rv.FakeClock()
    real_mono = _t.monotonic
    _t.monotonic = clock.monotonic
    logs = []
    cap = lambda msg: logs.append(str(msg))
    saved = (m.log, triggers.log, triggers.time)
    m.log = cap; triggers.log = cap
    triggers.time = _t          # the unit tests above swapped in their own Clock; use the node's fake clock
    try:
        argv = ["--preview", "--topic", "/t", "--lock-trigger", "gesture2fa", "--gesture-model", "none.onnx",
                "--gesture-2fa-step-s", "0.25", "--gesture-2fa-gap-s", "1.5", "--gesture-dwell-s", "0",
                "--reseed-anchor-floor", "0.55", "--switch-anchor-floor", "0.55", "--switch-anchor-frames", "8",
                "--no-search-on-loss", "--lost-grace", "4"]
        a = m.parse_args(argv)
        f = m.Follower(a)
        f.node = rv.StubNode(clock, 10.0)
        scene = {"persons": []}

        class Det:
            ok = True

            def detect(self, frame):
                return [copy.deepcopy(p) for p in scene["persons"]]

        f.det = Det()
        pose_now = {}

        class Pose:
            def predict(self, frame, verbose=False):
                ps = [(pose_for(p, **pose_now.get(p["_who"], {})), p["box"]) for p in scene["persons"]]
                if not ps:
                    return [FakeResult(np.zeros((0, 17, 3)), np.zeros((0, 4)))]
                return [FakeResult(np.stack([k for k, _ in ps]), np.array([b for _, b in ps], dtype=float))]

        f._lock_trigger = triggers.SequenceGestureTrigger("none.onnx", a, every_n=1, _model_override=Pose())

        op = dict(person(None, 100), _who="op"); st = dict(person(None, 420), _who="st")
        colors = {"op": (0, 0, 220), "st": (220, 60, 0)}

        def frame_of(ps):
            img = np.full((480, 640, 3), 90, dtype=np.uint8)
            for p in ps:
                x1, y1, x2, y2 = [int(v) for v in p["box"]]
                img[y1:y2, x1:x2] = colors[p["_who"]]
            return img

        def step(n, ps, poses):
            scene["persons"] = ps
            pose_now.clear(); pose_now.update(poses)
            for _ in range(n):
                clock.tick(0.1)
                f._process_frame(frame_of(ps))

        def do_seq(who, ps):
            for s_ in ["R_UP", "BOTH_UP", "L_UP", "DOWN"]:
                step(5, ps, {who: STEP_POSE[s_]})

        step(3, [op, st], {})
        check("starts in SEARCH", f.state == m.S_SEARCH, f.state)
        do_seq("op", [op, st])
        step(4, [op, st], {})                           # latch window: >= seed_frames consecutive hints
        check("operator's sequence SEEDS (latched hint reaches seed_frames)", f.state == m.S_TRACK and f.seed is not None,
              (f.state, [l for l in logs if "2FA" in l or "SEED" in l][-4:]))
        op_tid = f.seed.track_id if f.seed is not None else None
        check("seeded on the operator's body", op_tid is not None and f.seed.box[0] < 300, f.seed.box if f.seed else None)

        step(12, [st], {})                             # operator gone -> loss
        check("operator out of view -> REACQUIRE (stranger NOT followed)", f.state == m.S_REACQUIRE, f.state)
        n0 = len(logs)
        do_seq("st", [st])
        step(6, [st], {})
        check("stranger copying the sequence is REFUSED by identity",
              f.state == m.S_REACQUIRE and any("identity-mismatch" in l for l in logs[n0:]),
              (f.state, [l for l in logs[n0:] if "SEED" in l or "2FA" in l][-3:]))
        check("latch drops after the window, no lingering hint",
              f._lock_trigger._latch is None or clock.now <= f._lock_trigger._latch["until"])

        op2 = dict(person(None, 100), _who="op")
        step(3, [op2, st], {})
        do_seq("op", [op2, st])
        step(4, [op2, st], {})
        check("operator re-seeds with the sequence", f.state == m.S_TRACK and f.seed is not None and f.seed.box[0] < 300,
              (f.state, [l for l in logs if "SEED" in l or "2FA" in l][-4:]))
    finally:
        _t.monotonic = real_mono
        m.log, triggers.log, triggers.time = saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    args = ap.parse_args()
    triggers.time = Clock; handcount.time = Clock
    test_steps(); test_fingers(); test_body_sequence(); test_wrong_order(); test_two_bodies()
    test_timeout(); test_countdown(); test_state_gate()
    m = load_node(args.node)
    test_node_config(m); test_switch_floor(m); test_e2e(m)
    if FAILS:
        print("GESTURE2FA-SELFTEST-FAIL %d: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("GESTURE2FA-SELFTEST-OK sequence lock + finger count + identity switch floor")


if __name__ == "__main__":
    main()
