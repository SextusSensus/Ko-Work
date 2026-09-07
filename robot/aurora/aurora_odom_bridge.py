#!/usr/bin/env python3
"""Bridge the Aurora's relocalized pose onto the odom topic the follow already consumes.

THE WHOLE IDEA, in one line: the follow's --localmap already reads a planar pose from
`booster_interface/msg/Odometer{x,y,theta}` on --odom-topic and fails closed when it is stale.
So "localize the robot in the pre-built Aurora map" reduces to: relocalize the Aurora in that
map, then republish its drift-free pose as that same Odometer message on a topic. Point the follow
at it with `--odom-topic /aurora_odom` and NOTHING in the safety-critical node changes -- the local
map simply runs on a global, drift-free pose instead of the robot's own drifting wheel odometry.

THE FAIL-CLOSED GATE IS REAL (this is the fix for the 2026-09-07 council finding A1). The device
reports get_relocalization_status(); this bridge publishes a pose ONLY while that status is SUCCEED.
The instant relocalization is NONE / IN_PROGRESS / FAILED -- lost track, walked into unmapped space,
never locked -- it STOPS publishing. The consumer's 1.0 s freshness window then returns None, the
local map contributes nothing, and the follow degrades to live-depth-only braking (the behaviour with
no Aurora at all). Plus an implausible-jump guard: a per-frame step beyond a walk-speed bound is
dropped, so a relocalization snap can't be read as real travel. Fail-closed is the default path.

WHY THIS IS SAFE next to a drive (and why it can ship before the map ever touches the brake): it
NEVER feeds the brake directly. The only consumer is _localmap_update / _localmap_clearance, whose
invariant is that memory may only ever REDUCE clearance, never raise it. A wrong pose here can
over-brake (phantom obstacle) but can never release the brake on a real one -- it degrades to caution,
not collision. And it is inert until run: a drive is unaffected unless someone both runs this AND
passes --odom-topic /aurora_odom.

THE ONE CALIBRATION THAT MATTERS: --yaw-offset-deg. The local map places a cell at
    wx = x + fwd*cos(th) - left*sin(th)   (fwd = robot-forward depth, left = robot-left)
so `th` must be the ROBOT's heading and (x,y) the robot's position, in one consistent frame. The
Aurora reports ITS OWN pose in the map frame; the rotation between "Aurora forward" and "robot
forward" about the vertical axis is a fixed mount property and must be subtracted, or every remembered
cell is placed at the wrong bearing. Measure it with --verify-frame (below). The mount TRANSLATION (a
few cm) is a constant shift on a robot-centric, 8 s-TTL buffer and is within noise -- ignore it.

FRAME CHECK (council finding A3): pose_to_planar() assumes the map frame is gravity-aligned with the
ground plane spanned by (x,y) and yaw taken about z. Run `--verify-frame` FIRST on the robot: it locks,
then prints raw pose components while you move the robot a known straight line + 90 deg turn, so you
can confirm which two axes are the ground plane and that the up-axis stays ~constant. If the device is
y-up, swap the ground axes in pose_to_planar() -- it is isolated there and nothing else changes.

VERIFIED SDK FLOW (on device 2.1.1, 2026-09-07): map_manager.start_upload_session(path) -> wait;
controller.require_pure_localization_mode(); controller.require_relocalization();
data_provider.get_relocalization_status() -> (state, ts) with SUCCEED==2. (The old
aurora_upload_reloc.py called s.require_relocalization, which does NOT exist -- it is on s.controller.)

VERIFY ON ROBOT. Needs rclpy + the booster_interface workspace (run_follow*.sh source it), plus a
connected, relocalized Aurora. Inert until run.

  python3 aurora_odom_bridge.py map.vslam [--conn 192.168.11.1] [--topic /aurora_odom]
                                          [--rate 15] [--yaw-offset-deg 0] [--verify-frame]
"""
import argparse
import math
import sys
import time

import slamtec_aurora_sdk as sdk
from slamtec_aurora_sdk import data_types as dt

RELOC_SUCCEED = dt.DEVICE_RELOCALIZATION_STATUS_SUCCEED   # 2 -- the ONLY state we publish in
RELOC_NAMES = {0: "NONE", 1: "IN_PROGRESS", 2: "SUCCEED", 3: "FAILED"}


def connect(conn):
    """Discovery first (how the demos connect), then the connection string as a fallback."""
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
            print("no discovery result; connecting by string:", conn)
            s.connect(connection_string=conn)
    except Exception as e:  # noqa: BLE001
        print("CONNECT-FAIL:", type(e).__name__, e)
        return None
    print("connected:", s.is_connected())
    return s


def prepare(s, map_path):
    """Load the map (only if the device has none resident), enter pure-localization mode, and ask the
    device to relocalize. Returns True if the flow ran without error -- NOT that a lock was achieved;
    the lock is watched per-frame in the publish loop, because it can only happen once the device sees
    and moves through the mapped space."""
    mm, ctl, dp = s.map_manager, s.controller, s.data_provider
    try:
        if not dp.get_all_map_info():
            print("no resident map -> uploading", map_path)
            mm.start_upload_session(map_path)
            t0 = time.monotonic()
            while mm.is_session_active() and time.monotonic() - t0 < 180:
                time.sleep(1.0)
            print("upload done; maps resident:", len(dp.get_all_map_info()))
        else:
            print("device already has a resident map; skipping upload")
    except Exception as e:  # noqa: BLE001
        print("MAP-LOAD-FAIL:", type(e).__name__, e)
        return False
    try:
        ctl.require_pure_localization_mode(timeout_ms=8000)
        ctl.require_relocalization(timeout_ms=8000)
        print("pure-localization mode set; relocalization requested")
    except Exception as e:  # noqa: BLE001
        print("LOCALIZATION-MODE-FAIL:", type(e).__name__, e)
        return False
    return True


def pose_to_planar(p, yaw_offset_rad):
    """Aurora SE3 pose -> (x, y, theta) planar, robot-forward heading. Returns None if unusable.

    ISOLATED ON PURPOSE: the ONLY place that encodes the Aurora map-frame convention. The SDK's
    use_se3 pose is ((tx,ty,tz), (qx,qy,qz,qw), status). This assumes a z-up, gravity-aligned map so
    the ground plane is (x,y) and yaw is about z -- CONFIRM with --verify-frame before trusting it. If
    the device is y-up, swap the two ground axes here and take yaw about the up axis; nothing else in
    the file changes."""
    try:
        (tx, ty, _tz), (qx, qy, qz, qw), _st = p
        tx, ty = float(tx), float(ty)
        qx, qy, qz, qw = float(qx), float(qy), float(qz), float(qw)
    except Exception:  # noqa: BLE001
        return None
    yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
    th = yaw - yaw_offset_rad
    th = math.atan2(math.sin(th), math.cos(th))          # normalise to (-pi, pi]
    if not (math.isfinite(tx) and math.isfinite(ty) and math.isfinite(th)):
        return None
    return tx, ty, th


def verify_frame(s):
    """Bring-up helper (council A3): lock, then stream raw pose so the operator can read off which
    axes are the ground plane and measure the yaw offset by moving the robot a known path."""
    dp = s.data_provider
    print("VERIFY-FRAME: waiting for a lock, then move the robot a straight line + a 90 deg turn.")
    print("  watch: which of x/y move on the straight line (ground plane), z should stay ~constant;")
    print("  the heading change over the 90 deg turn tells you the yaw sign + offset.")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        try:
            rs = dp.get_relocalization_status()
            p = s.get_current_pose(use_se3=True)
            (tx, ty, tz), q, _ = p
            yaw = math.degrees(math.atan2(2.0 * (q[3] * q[2] + q[0] * q[1]),
                                          1.0 - 2.0 * (q[1] * q[1] + q[2] * q[2])))
            print("reloc=%-11s x=%+.3f y=%+.3f z=%+.3f yaw=%+.1fdeg"
                  % (RELOC_NAMES.get(rs[0], rs[0]), tx, ty, tz, yaw))
        except Exception as e:  # noqa: BLE001
            print("verify err:", e)
            break
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map", help="saved .vslam map to upload + relocalize in")
    ap.add_argument("--conn", default="192.168.11.1", help="device connection string (SDK IP)")
    ap.add_argument("--topic", default="/aurora_odom",
                    help="topic to publish Odometer on; point the follow's --odom-topic here")
    ap.add_argument("--rate", type=float, default=15.0, help="publish Hz (keep > 1 for freshness)")
    ap.add_argument("--yaw-offset-deg", type=float, default=0.0,
                    help="Aurora-forward minus robot-forward, degrees (measure with --verify-frame)")
    ap.add_argument("--max-step-m", type=float, default=0.20,
                    help="drop a frame whose x/y jumps more than this (reloc-snap guard)")
    ap.add_argument("--verify-frame", action="store_true",
                    help="bring-up: lock then stream raw pose to confirm axes + measure yaw offset")
    a = ap.parse_args()

    s = connect(a.conn)
    if s is None:
        return 1
    if not prepare(s, a.map):
        print("preparation failed -> refusing to publish (fail closed).")
        s.disconnect()
        return 1

    if a.verify_frame:
        verify_frame(s)
        s.disconnect()
        return 0

    # rclpy + the Booster interface are only present on the robot; import late so --help works anywhere.
    try:
        import rclpy
        from rclpy.node import Node
        from booster_interface.msg import Odometer
    except Exception as e:  # noqa: BLE001
        print("ROS import failed (%s) -- run this ON THE ROBOT with run_follow's workspace sourced" % e)
        s.disconnect()
        return 2

    dp = s.data_provider
    rclpy.init()
    node = Node("aurora_odom_bridge")
    pub = node.create_publisher(Odometer, a.topic, 10)
    yaw_off = math.radians(a.yaw_offset_deg)
    period = 1.0 / max(1.0, a.rate)
    print("PUBLISHING %s on %s at %.0f Hz (yaw offset %.1f deg) -- ONLY while relocalized. Ctrl-C to stop."
          % (Odometer.__name__, a.topic, a.rate, a.yaw_offset_deg))
    n = 0
    last_state = None
    last_xy = None
    try:
        while rclpy.ok():
            t0 = time.monotonic()
            publish = None
            try:
                state = dp.get_relocalization_status()[0]
            except Exception as e:  # noqa: BLE001
                state = None
                if last_state != "err":
                    print("reloc-status read error (%s) -> quiet" % type(e).__name__)
                last_state = "err"
            if state == RELOC_SUCCEED:
                try:
                    planar = pose_to_planar(s.get_current_pose(use_se3=True), yaw_off)
                except Exception:  # noqa: BLE001
                    planar = None
                if planar is not None:
                    if last_xy is not None:
                        step = math.hypot(planar[0] - last_xy[0], planar[1] - last_xy[1])
                        if step > a.max_step_m:
                            # reloc snap / implausible jump -> drop this frame, don't publish, don't
                            # advance last_xy so the NEXT frame is measured against the pre-jump pose.
                            print("JUMP %.2fm > %.2fm -> dropped (not published)" % (step, a.max_step_m))
                            planar = None
                    if planar is not None:
                        publish = planar
            # state transition logging (SUCCEED <-> not), so a lost lock is visible in the log
            if state != last_state:
                print("reloc-status -> %s%s" % (RELOC_NAMES.get(state, state),
                      "" if state == RELOC_SUCCEED else "  (going quiet: follow falls back to live depth)"))
                last_state = state
            if publish is not None:
                m = Odometer()
                m.x, m.y, m.theta = float(publish[0]), float(publish[1]), float(publish[2])
                pub.publish(m)
                last_xy = (publish[0], publish[1])
                n += 1
                if n == 1 or n % 150 == 0:
                    print("odom #%d: x=%.2f y=%.2f th=%.1fdeg" % (n, m.x, m.y, math.degrees(m.theta)))
            else:
                # not published this frame -> the consumer will stale out to None (fail closed)
                last_xy = last_xy if state == RELOC_SUCCEED else None
            dt_ = time.monotonic() - t0
            if dt_ < period:
                time.sleep(period - dt_)
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
