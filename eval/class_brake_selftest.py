#!/usr/bin/env python3
"""Headless selftest for Plan A class-aware obstacle brake.

Contracts (docs/PLAN_A_CLASS_AWARE_BRAKE.md):
  A1  no depth clearance -> no class-only brake
  A2  class may only tighten (cap_class <= cap_geom); delta >= 0
  A6  flag off -> identical to geometry-only
  A7  followed operator (high IoU with track box) is not a class obstacle

No robot, no camera, no motion. Uses ROS import stubs off-robot.

Usage:  python eval/class_brake_selftest.py [--node robot/follow_person_k1.py]
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import types


NODE_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "robot", "follow_person_k1.py",
)


def _install_ros_stubs():
    if "rclpy" in sys.modules:
        return
    rclpy = types.ModuleType("rclpy")
    rclpy.node = types.ModuleType("rclpy.node")
    rclpy.executors = types.ModuleType("rclpy.executors")
    rclpy.qos = types.ModuleType("rclpy.qos")

    class Node:
        pass

    class SingleThreadedExecutor:
        pass

    class QoSProfile:
        def __init__(self, *a, **k):
            pass

    class ReliabilityPolicy:
        BEST_EFFORT = 1
        RELIABLE = 2

    class HistoryPolicy:
        KEEP_LAST = 1

    class DurabilityPolicy:
        VOLATILE = 1

    rclpy.node.Node = Node
    rclpy.executors.SingleThreadedExecutor = SingleThreadedExecutor
    rclpy.qos.QoSProfile = QoSProfile
    rclpy.qos.ReliabilityPolicy = ReliabilityPolicy
    rclpy.qos.HistoryPolicy = HistoryPolicy
    rclpy.qos.DurabilityPolicy = DurabilityPolicy
    rclpy.init = lambda *a, **k: None
    rclpy.shutdown = lambda *a, **k: None
    sys.modules["rclpy"] = rclpy
    sys.modules["rclpy.node"] = rclpy.node
    sys.modules["rclpy.executors"] = rclpy.executors
    sys.modules["rclpy.qos"] = rclpy.qos
    sm = types.ModuleType("sensor_msgs")
    sm.msg = types.ModuleType("sensor_msgs.msg")
    sm.msg.Image = type("Image", (), {})
    sys.modules["sensor_msgs"] = sm
    sys.modules["sensor_msgs.msg"] = sm.msg


def load(node_path):
    _install_ros_stubs()
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


class FakeRR:
    ok = False


def harness(m, argv):
    """Minimal object with Plan A methods bound; depth corridor stubbed."""
    a = m.parse_args(argv)
    f = object.__new__(m.Follower)
    f.a = a
    f.vx_max = float(getattr(a, "vx_max", 0.18) or 0.18)
    f._class_dets = []
    f._class_brake_info = None
    f._track_box = None
    f._last_frame_w = 640.0
    f._clr = 1.0  # metres; overridden per case

    def _corridor_clearance():
        return f._clr

    def _obstacle_memory(clr):
        return clr

    f._corridor_clearance = _corridor_clearance
    f._obstacle_memory = _obstacle_memory
    f._grade_vx_cap = m.Follower._grade_vx_cap.__get__(f, m.Follower)
    f._class_brake_delta = m.Follower._class_brake_delta.__get__(f, m.Follower)
    f._obstacle_vx_cap = m.Follower._obstacle_vx_cap.__get__(f, m.Follower)
    # silence Rerun side effects
    m.rerun_sink._RR = FakeRR()
    return f


def det(cls, cx, cy=200.0, conf=0.8, box=None, w=80.0, h=160.0):
    if box is None:
        box = (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)
    return {"box": box, "cx": cx, "cy": cy, "w": w, "h": h, "conf": conf, "cls": cls}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    args = ap.parse_args()
    m = load(args.node)

    # --- config / defaults ---
    d = vars(m.parse_args([]))
    assert d["obstacle_class_brake"] == "off", d["obstacle_class_brake"]
    assert d["obstacle_class_delta_person"] == 0.25
    assert "obstacle_class_min_iou" in d

    on = ["--obstacle-brake", "--obstacle-class-brake", "on",
          "--obstacle-brake-start", "1.15", "--obstacle-brake-stop", "0.7",
          "--obstacle-corridor-frac", "0.35",
          "--obstacle-class-delta-person", "0.25",
          "--obstacle-class-delta-furniture", "0.10",
          "--vx-max", "0.18"]
    off = ["--obstacle-brake", "--obstacle-class-brake", "off",
           "--obstacle-brake-start", "1.15", "--obstacle-brake-stop", "0.7",
           "--vx-max", "0.18"]

    # 1) grade helper
    f = harness(m, on)
    assert f._grade_vx_cap(1.20, 1.15, 0.7) is None
    assert f._grade_vx_cap(0.70, 1.15, 0.7) == 0.0
    mid = f._grade_vx_cap(0.925, 1.15, 0.7)
    assert mid is not None and 0.0 < mid < 0.18

    # 2) A6: flag off == geometry-only (identical cap)
    f_off = harness(m, off)
    f_on = harness(m, on)
    f_off._clr = f_on._clr = 1.00
    # bystander in corridor would tighten if on
    f_on._class_dets = [det(0, 320.0)]  # person at centre, 640-wide
    f_on._track_box = (10, 10, 50, 50)  # non-overlapping operator
    f_off._class_dets = list(f_on._class_dets)
    f_off._track_box = f_on._track_box
    cap_off, clr_off = f_off._obstacle_vx_cap(2.5)
    cap_on, clr_on = f_on._obstacle_vx_cap(2.5)
    assert clr_off == clr_on == 1.00
    assert cap_off is not None and cap_on is not None
    assert cap_on < cap_off, (cap_on, cap_off)  # class tightened
    # same scene with flag off must ignore class dets
    f_off2 = harness(m, off)
    f_off2._clr = 1.00
    f_off2._class_dets = []
    cap_bare, _ = f_off2._obstacle_vx_cap(2.5)
    assert cap_bare == cap_off

    # 3) A1: no clearance -> no cap even with class dets
    f = harness(m, on)
    f._clr = None
    f._class_dets = [det(0, 320.0)]
    cap, clr = f._obstacle_vx_cap(2.5)
    assert cap is None and clr is None

    # 4) A2: delta never negative; furniture delta raises start
    f = harness(m, on)
    f._clr = 1.10  # just inside geom start 1.15 -> small geom cap
    f._class_dets = [det(56, 320.0)]  # chair
    f._track_box = None
    cap_g_only = f._grade_vx_cap(1.10, 1.15, 0.7)
    cap, _ = f._obstacle_vx_cap(2.5)
    assert f._class_brake_info is not None
    assert f._class_brake_info["delta"] == 0.10
    assert cap is not None and cap_g_only is not None
    assert cap <= cap_g_only + 1e-9

    # 5) A7: operator IoU exclude — person overlapping track box ignored
    f = harness(m, on)
    f._clr = 1.00
    op = (280, 120, 360, 280)
    f._track_box = op
    f._class_dets = [det(0, 320.0, box=op)]
    delta, cls, name = f._class_brake_delta(2.5)
    assert delta == 0.0 and cls is None, (delta, cls, name)
    cap, _ = f._obstacle_vx_cap(2.5)
    assert f._class_brake_info is None
    # bystander beside operator still tightens
    f._class_dets = [det(0, 320.0, box=op), det(0, 300.0, box=(250, 100, 310, 260))]
    # second box may still IoU with op — use far-left bystander still in corridor
    # corridor frac 0.35 on 640: x0=208, x1=432. Put bystander at cx=250, small box.
    f._class_dets = [
        det(0, 320.0, box=op, conf=0.9),
        det(0, 250.0, box=(220, 100, 280, 260), conf=0.9),
    ]
    delta, cls, name = f._class_brake_delta(2.5)
    assert delta == 0.25 and cls == 0 and name == "person", (delta, cls, name)

    # 6) target-range exclusion still wins (nearest clr is the followed person)
    f = harness(m, on)
    f._clr = 2.0
    f._class_dets = [det(0, 320.0)]
    cap, clr = f._obstacle_vx_cap(target_range=2.1)  # clr > target - margin(0.4)
    assert cap is None and clr == 2.0

    # 7) empty dets -> geometry only
    f = harness(m, on)
    f._clr = 1.00
    f._class_dets = []
    cap_a, _ = f._obstacle_vx_cap(2.5)
    f2 = harness(m, off)
    f2._clr = 1.00
    cap_b, _ = f2._obstacle_vx_cap(2.5)
    assert cap_a == cap_b

    # 8) detect_classes API exists and is inert when detector not ok
    from perception import PersonDetector
    pd = PersonDetector.__new__(PersonDetector)
    pd.ok = False
    pd.conf = 0.35
    pd.model = None
    assert pd.detect_classes(None) == []

    print("CLASS-BRAKE-SELFTEST-OK")


if __name__ == "__main__":
    main()
