#!/usr/bin/env python3
"""Bridge the Aurora's relocalized pose onto the odom topic the follow already consumes.

THE WHOLE IDEA, in one line: the follow's --localmap already reads a planar pose from
`booster_interface/msg/Odometer{x,y,theta}` on --odom-topic and fails closed when it is stale.
So "localize the robot in the pre-built Aurora map" reduces to: relocalize the Aurora in that
map, then republish its drift-free pose as that same Odometer message on a topic. Point the follow
at it with `--odom-topic /aurora_odom` and NOTHING in the safety-critical node changes -- the local
map simply runs on a global, drift-free pose instead of the robot's own drifting wheel odometry.

WHY THIS IS SAFE TO RUN NEXT TO A DRIVE (and why it can ship before the map ever touches the brake):
  * It NEVER feeds the brake directly. The Aurora map does not become phantom walls. The only thing
    that consumes this pose is _localmap_update / _localmap_clearance, whose invariant is that memory
    may only ever REDUCE clearance, never raise it. A wrong pose here can therefore over-brake
    (phantom obstacle) but can never release the brake on a real one -- it degrades to caution.
  * It FAILS CLOSED by simply going quiet. On any doubt -- relocalization lost, a bad pose read, a
    non-finite value -- it STOPS publishing. The consumer's 1.0 s freshness window then returns None,
    the local map contributes nothing, and the follow degrades to live-depth-only braking, which is
    exactly the behaviour with no Aurora at all. Fail-closed is the default, not an added path.
  * It is inert until run. Like every other script in robot/aurora/, it touches nothing in the follow
    stack; a drive is unaffected unless someone both runs this AND passes --odom-topic /aurora_odom.

THE ONE CALIBRATION THAT MATTERS: --yaw-offset-deg. The local map places a cell at
    wx = x + fwd*cos(th) - left*sin(th)   (fwd = robot-forward depth, left = robot-left)
so `th` must be the ROBOT's heading and (x,y) the robot's position, in one consistent frame. The
Aurora reports ITS OWN pose in the map frame; the rotation between "Aurora forward" and "robot
forward" about the vertical axis is a fixed mount property and must be subtracted, or every remembered
cell is placed at the wrong bearing. The mount TRANSLATION (a few cm offset) is a constant shift on a
robot-centric, 8 s-TTL buffer and is within noise -- ignore it. To measure the yaw offset: stand the
robot still, `python3 aurora_pose_probe.py`, note the heading; drive it straight forward ~1 m and see
which way the (x,y) moved -- atan2(dy,dx) minus the robot's known forward is the offset. Verify sign
on the robot: a correct offset makes a wall dead ahead land dead ahead in the LOCALMAP log lines.

VERIFY ON ROBOT. This needs rclpy + the booster_interface workspace (run_follow*.sh source it) and a
connected, relocalized Aurora. The SE3->planar axis mapping below (which two axes are the ground
plane, and yaw sign) is the Aurora map-frame convention and MUST be confirmed against a live
aurora_pose_probe dump before this is trusted -- it is marked and isolated in pose_to_planar().

  python3 aurora_odom_bridge.py map.vslam --yaw-offset-deg 0 [--topic /aurora_odom] [--rate 15] [ip]
"""
import argparse
import math
import sys
import time

import slamtec_aurora_sdk as sdk


def connect(want_ip):
    """Connect exactly like the other bring-up scripts: discovery first, then a string fallback."""
    s = sdk.AuroraSDK()
    dev = None
    try:
        found = s.discover_devices(timeout=5.0)
        print("discovered %d device(s)" % len(found))
        if found:
            dev = found[0]
    except Exception as e:  # noqa: BLE001
        print("discover error:", type(e).__name__, e)
    try:
        if dev is not None:
            s.connect(device_info=dev)
        else:
            addr = want_ip or "192.168.127.10"
            print("no discovery result; connecting by string:", addr)
            s.connect(connection_string=addr)
    except Exception as e:  # noqa: BLE001
        print("CONNECT-FAIL:", type(e).__name__, e)
        return None
    print("connected:", s.is_connected())
    return s


def upload_and_relocalize(s, path):
    """Push the saved map and relocalize in it. Returns True only on a confirmed relocalization."""
    mm = s.map_manager
    try:
        print("uploading %s -> device" % path)
        mm.start_upload_session(path)
        while mm.is_session_active():
            print("  status:", mm.query_session_status())
            time.sleep(1.0)
        print("UPLOAD-OK")
    except Exception as e:  # noqa: BLE001
        print("UPLOAD-FAIL:", type(e).__name__, e)
        try:
            mm.abort_session()
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        print("requiring relocalization (move the robot a little if it stalls)...")
        s.require_relocalization(timeout_ms=20000)
        print("RELOC-OK")
        return True
    except Exception as e:  # noqa: BLE001
        print("RELOC-FAIL (map may not match the space, or needs movement):", type(e).__name__, e)
        return False


def pose_to_planar(p, yaw_offset_rad):
    """Aurora SE3 pose -> (x, y, theta) planar, robot-forward heading. Returns None if unusable.

    ISOLATED ON PURPOSE: this is the ONLY place that encodes the Aurora map-frame convention, so the
    one thing that must be checked on the device lives in one function. The SDK's use_se3 pose gives a
    translation and a quaternion. The mapping below assumes the map frame is gravity-aligned with the
    ground plane spanned by (x, y) and yaw taken about z -- CONFIRM THIS against a live pose_probe dump
    (walk the robot in a known direction, watch which fields move) before trusting the bridge. If the
    device turns out to be y-up, swap the ground axes and take yaw about the up axis here; nothing else
    in the file changes."""
    try:
        # The SDK's SE3 pose exposes translation + quaternion; be defensive about the accessor shape,
        # since these scripts have been bitten before by trusting a doc summary over the real object.
        tx = float(getattr(p, "x", None) if hasattr(p, "x") else p.translation[0])
        ty = float(getattr(p, "y", None) if hasattr(p, "y") else p.translation[1])
        q = getattr(p, "quaternion", None) or getattr(p, "rotation", None) or getattr(p, "q", None)
        qx, qy, qz, qw = (float(q[0]), float(q[1]), float(q[2]), float(q[3]))
    except Exception:  # noqa: BLE001
        return None
    # yaw about z from the quaternion (REP-103 convention once the axis mapping above is confirmed)
    yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
    th = yaw - yaw_offset_rad
    # normalise to (-pi, pi] so the consumer never sees a wrapped jump
    th = math.atan2(math.sin(th), math.cos(th))
    if not (math.isfinite(tx) and math.isfinite(ty) and math.isfinite(th)):
        return None
    return tx, ty, th


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map", help="saved .vslam map to upload + relocalize in")
    ap.add_argument("ip", nargs="?", default=None, help="device IP (default: auto-discover)")
    ap.add_argument("--topic", default="/aurora_odom",
                    help="topic to publish Odometer on; point the follow's --odom-topic here")
    ap.add_argument("--rate", type=float, default=15.0, help="publish Hz (keep > 1 for freshness)")
    ap.add_argument("--yaw-offset-deg", type=float, default=0.0,
                    help="Aurora-forward minus robot-forward, degrees (see the header; MEASURE it)")
    a = ap.parse_args()

    # rclpy + the Booster interface are only present on the robot; import late so --help works anywhere.
    try:
        import rclpy
        from rclpy.node import Node
        from booster_interface.msg import Odometer
    except Exception as e:  # noqa: BLE001
        print("ROS import failed (%s) -- run this ON THE ROBOT with run_follow's workspace sourced" % e)
        return 2

    s = connect(a.ip)
    if s is None:
        return 1
    if not upload_and_relocalize(s, a.map):
        print("NOT RELOCALIZED -> refusing to publish (fail closed). The follow will use live depth only.")
        s.disconnect()
        return 1

    rclpy.init()
    node = Node("aurora_odom_bridge")
    pub = node.create_publisher(Odometer, a.topic, 10)
    yaw_off = math.radians(a.yaw_offset_deg)
    period = 1.0 / max(1.0, a.rate)
    print("PUBLISHING %s on %s at %.0f Hz (yaw offset %.1f deg). Ctrl-C to stop."
          % (Odometer.__name__, a.topic, a.rate, a.yaw_offset_deg))
    n = 0
    miss = 0
    try:
        while rclpy.ok():
            t0 = time.monotonic()
            planar = None
            try:
                p = s.get_current_pose(use_se3=True)
                planar = pose_to_planar(p, yaw_off)
            except Exception as e:  # noqa: BLE001
                planar = None
                if miss == 0:
                    print("pose read error (%s) -> going quiet; follow falls back to live depth"
                          % type(e).__name__)
            if planar is None:
                # FAIL CLOSED: publish nothing. Staleness > 1 s makes latest_odom() return None and the
                # local map contribute nothing -- the safe fallback, reached by simply not publishing.
                miss += 1
            else:
                m = Odometer()
                m.x, m.y, m.theta = float(planar[0]), float(planar[1]), float(planar[2])
                pub.publish(m)
                n += 1
                if miss:
                    print("relocalized pose recovered after %d missed frame(s)" % miss)
                    miss = 0
                if n == 1 or n % 150 == 0:
                    print("odom #%d: x=%.2f y=%.2f th=%.1fdeg" % (n, m.x, m.y, math.degrees(m.theta)))
            dt = time.monotonic() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
        try:
            s.disconnect()
        except Exception:  # noqa: BLE001
            pass
    print("stopped after %d published poses" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
