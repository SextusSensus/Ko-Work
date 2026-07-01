#!/usr/bin/env python3
"""
follow_marker_k1.py  -- runs ON the Booster K1 robot.

Subscribes BEST_EFFORT to the K1 head camera ROS2 topic, converts NV12->BGR,
detects a DICT_4X4_50 ArUco marker, computes (range, bearing) with the pinhole
math/tunables from follow_marker.py, and prints one concise status line per
processed frame.

Modes (argparse):
  --preview   (DEFAULT)  detect + print only. NEVER drives. No bridge spawned.
  --drive                spawn ./loco_follow_bridge, ping/prep/walk, then stream
                         'v vx vy vyaw' at RATE_HZ with HARD-clamped velocities.

SAFETY (drive mode):
  * 'ping' must return OK before anything moves (verifies loco WITHOUT walking).
  * prep -> wait ~3s -> walk -> wait ~2s, then per-frame velocity stream.
  * Lost marker (LOST_GRACE frames) -> 'stop' (bridge issues MoveCommand 0,0,0).
  * No NEW frame for > --stall-seconds (~1s) -> 'stop'.
  * --max-seconds session watchdog -> auto 'stop' + clean shutdown.
  * If NO frame ever arrives (idle camera) we stay in a safe NO-FRAME loop and
    NEVER enter walk.
  * SIGINT / SIGTERM / any exception / ssh drop -> 'stop' then 'quit' to bridge,
    join, exit. The bridge itself does MoveCommand(0,0,0)+ChangeMode(kPrepare)
    on its own exit, so loco is always left safe.

Env (caller sources these before python3):
    source /opt/ros/humble/setup.bash
    source /opt/booster/BoosterRos2/install/setup.bash

Every printed line is flushed so the K1Finder app log updates live.
"""

import os
import sys
import time
import math
import signal
import argparse
import subprocess
import threading

import numpy as np
import cv2

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image


# ---------------------------------------------------------------------------
# TUNABLES (mirrored from follow_marker.py; small/safe defaults, arg-overridable)
# ---------------------------------------------------------------------------
DEF_MARKER_SIZE_M = 0.18      # BLACK square side after printing (measure it)
DEF_STANDOFF_M    = 1.2       # how far behind the target the robot holds
DEF_DEADBAND_M    = 0.20      # don't fidget within +/- this of standoff
DEF_HFOV_DEG      = 70.0      # K1 head camera horizontal FOV

DEF_K_YAW = 0.9               # turn gain  (rad/s per rad of bearing error)
DEF_K_VX  = 0.35              # speed gain (m/s per m of range error)

# HARD clamps -- intentionally smaller than follow_marker.py defaults.
DEF_VX_MIN, DEF_VX_MAX     = -0.06, 0.18
DEF_VYAW_MIN, DEF_VYAW_MAX = -0.30, 0.30
# Absolute ceilings the args can NEVER exceed (defence in depth).
HARD_VX_LIMIT   = 0.30
HARD_VYAW_LIMIT = 0.40

DEF_LOST_GRACE     = 8        # frames with no marker before we stop
DEF_RATE_HZ        = 10.0
DEF_STALL_SECONDS  = 1.0      # no NEW frame for this long -> stop
DEF_MAX_SECONDS    = 120.0    # session watchdog
# The processed /boostercamera/head/rgb topic stays at 0Hz on this robot; the
# raw topic is the one that actually delivers frames (subscriber-gated).
DEF_TOPIC          = "/boostercamera/head/raw/rgb"

ARUCO_DICT = cv2.aruco.DICT_4X4_50


def log(msg):
    """Print one status line, flushed, so the app log streams live."""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# NV12 -> BGR (matches stream_cam.py)
# ---------------------------------------------------------------------------
def to_bgr(msg):
    h, w, enc, step = msg.height, msg.width, msg.encoding.lower(), msg.step
    buf = np.frombuffer(bytes(msg.data), dtype=np.uint8)
    if enc == "nv12":
        yuv = buf[: (h * 3 // 2) * w].reshape((h * 3 // 2, w))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
    if enc in ("yuv420", "i420"):
        yuv = buf[: (h * 3 // 2) * w].reshape((h * 3 // 2, w))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    ch = step // w if w else 3
    img = buf[: h * step].reshape((h, step))[:, : w * ch].reshape((h, w, ch))
    if enc == "rgb8":  return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if enc == "rgba8": return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    if enc == "bgra8": return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if enc in ("mono8", "8uc1"): return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


# ---------------------------------------------------------------------------
# ArUco detector (works across cv2 >=4.7 and older API)
# ---------------------------------------------------------------------------
_adict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
try:
    _detector = cv2.aruco.ArucoDetector(_adict, cv2.aruco.DetectorParameters())  # >=4.7

    def detect(gray):
        corners, ids, _ = _detector.detectMarkers(gray)
        return corners, ids
except AttributeError:
    def detect(gray):
        corners, ids, _ = cv2.aruco.detectMarkers(gray, _adict)                  # older
        return corners, ids


def target_from_frame(frame, marker_size_m, hfov_deg):
    """Return (range_m, bearing_rad) or None. Pinhole estimate, no calibration
    file needed. bearing > 0 = marker is to the RIGHT of image center."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    corners, ids = detect(gray)
    if ids is None or len(ids) == 0:
        return None

    c = corners[0].reshape(4, 2)                 # first marker, 4 corners
    cx_m = c[:, 0].mean()
    w_img = frame.shape[1]
    focal_px = (w_img / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)

    # apparent marker width in px -> range via pinhole
    side_px = (np.linalg.norm(c[0] - c[1]) + np.linalg.norm(c[2] - c[3])) / 2.0
    rng = (marker_size_m * focal_px) / max(side_px, 1.0)

    bearing = math.atan2((cx_m - w_img / 2.0), focal_px)
    return rng, bearing


# ---------------------------------------------------------------------------
# Bridge wrapper -- talks to the compiled loco_follow_bridge over stdin/stdout.
# Used ONLY in --drive. Never imported/spawned in --preview.
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, path):
        self.path = path
        self.proc = None
        self._lock = threading.Lock()

    def start(self):
        self.proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )

    def _send(self, cmd):
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return None
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                return None
            return cmd

    def command_expect_ok(self, cmd, timeout=4.0):
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return False, "<bridge-dead>"
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                return False, "<write-failed:%s>" % e
        reply_box = {}

        def _read():
            try:
                reply_box["line"] = self.proc.stdout.readline()
            except Exception as e:  # noqa: BLE001
                reply_box["err"] = str(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if "line" not in reply_box:
            return False, "<no-reply/timeout>"
        line = (reply_box["line"] or "").strip()
        if not line:
            return False, "<eof>"
        parts = line.split()
        ok = len(parts) >= 3 and parts[0] == "OK" and parts[-1] == "0"
        return ok, line

    def send_velocity(self, vx, vy, vyaw):
        self._send("v %.4f %.4f %.4f" % (vx, vy, vyaw))

    def stop(self):
        self._send("stop")

    def quit(self):
        self._send("quit")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def shutdown(self, join_timeout=3.0):
        try:
            if self.alive():
                self.stop()
                self.quit()
                t0 = time.time()
                while self.alive() and (time.time() - t0) < join_timeout:
                    time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.alive():
                self.proc.terminate()
                t0 = time.time()
                while self.alive() and (time.time() - t0) < 1.0:
                    time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.alive():
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# ROS node -- subscribes BEST_EFFORT, stamps each NEW frame.
# ---------------------------------------------------------------------------
class CamNode(Node):
    def __init__(self, topics):
        super().__init__("k1_follow_marker")
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.VOLATILE,
        )
        # The K1 camera is flaky about WHICH topic it publishes on (sometimes
        # /head/rgb, sometimes /head/raw/rgb), so subscribe to all candidates and
        # process whichever delivers a frame.
        for t in topics:
            self.create_subscription(Image, t, self._cb, qos)
        self._lock = threading.Lock()
        self._latest = None        # newest BGR frame
        self._stamp = 0.0          # monotonic time it arrived
        self._seq = 0              # increments on every NEW frame
        self.frames_total = 0

    def _cb(self, msg):
        try:
            bgr = to_bgr(msg)
        except Exception:  # noqa: BLE001 -- never let a bad frame kill the node
            return
        if bgr is None:
            return
        with self._lock:
            self._latest = bgr
            self._stamp = time.monotonic()
            self._seq += 1
            self.frames_total += 1

    def take_if_new(self, last_seq):
        with self._lock:
            if self._seq != last_seq and self._latest is not None:
                return self._latest, self._seq, self._stamp
            return None, last_seq, self._stamp


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------
class Follower:
    def __init__(self, args):
        self.a = args
        self.drive = args.drive
        self.bridge = None
        self.node = None
        self.walking = False        # True only after a successful prep+walk
        self._stop_requested = False
        self._cleaned = False
        self._cleanup_lock = threading.Lock()
        self.t_start = time.monotonic()

        self.vx_min = max(args.vx_min, -HARD_VX_LIMIT)
        self.vx_max = min(args.vx_max,  HARD_VX_LIMIT)
        self.vyaw_min = max(args.vyaw_min, -HARD_VYAW_LIMIT)
        self.vyaw_max = min(args.vyaw_max,  HARD_VYAW_LIMIT)

    def install_signal_handlers(self):
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, frame):  # noqa: ARG002
        log("SIGNAL %d -> stopping" % signum)
        self._stop_requested = True

    def start_drive_chain(self):
        """Returns True if we successfully reached WALK; False to stay safe."""
        path = self.a.bridge
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not (os.path.exists(path) and os.access(path, os.X_OK)):
            log("DRIVE-ABORT bridge not found/executable: %s" % path)
            return False

        self.bridge = Bridge(path)
        try:
            self.bridge.start()
        except Exception as e:  # noqa: BLE001
            log("DRIVE-ABORT cannot spawn bridge: %s" % e)
            return False

        ok, reply = self.bridge.command_expect_ok("ping")
        log("BRIDGE ping -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT ping failed; not moving")
            return False

        if not self._wait_for_first_frame(self.a.startup_frame_wait):
            log("DRIVE-ABORT no camera frame within %.1fs; staying safe (no walk)"
                % self.a.startup_frame_wait)
            return False

        if self._stop_requested:
            return False

        ok, reply = self.bridge.command_expect_ok("prep", timeout=6.0)
        log("BRIDGE prep -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT prep failed")
            return False
        if self._sleep_interruptible(3.0):
            return False

        ok, reply = self.bridge.command_expect_ok("walk", timeout=6.0)
        log("BRIDGE walk -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT walk failed")
            return False
        if self._sleep_interruptible(2.0):
            return False

        self.walking = True
        log("DRIVE-ACTIVE following enabled (vx[%.2f,%.2f] vyaw[%.2f,%.2f])"
            % (self.vx_min, self.vx_max, self.vyaw_min, self.vyaw_max))
        return True

    def _wait_for_first_frame(self, timeout):
        t0 = time.monotonic()
        while (time.monotonic() - t0) < timeout:
            if self._stop_requested:
                return False
            rclpy.spin_once(self.node, timeout_sec=0.1)
            frame, _, _ = self.node.take_if_new(-1)
            if frame is not None or self.node.frames_total > 0:
                return True
        return self.node.frames_total > 0

    def _sleep_interruptible(self, secs):
        t0 = time.monotonic()
        while (time.monotonic() - t0) < secs:
            if self._stop_requested:
                return True
            rclpy.spin_once(self.node, timeout_sec=0.05)
        return False

    def run(self):
        rclpy.init(args=None)
        # subscribe to the requested topic + both head camera topics (deduped),
        # because the camera intermittently uses one or the other.
        topics = list(dict.fromkeys([self.a.topic,
                                     "/boostercamera/head/raw/rgb",
                                     "/boostercamera/head/rgb"]))
        self.node = CamNode(topics)
        log("MODE %s  topics=%s" % ("DRIVE" if self.drive else "PREVIEW", ",".join(topics)))

        if self.drive:
            if not self.start_drive_chain():
                self._cleanup()
                rclpy.shutdown()
                return

        period = 1.0 / max(1.0, self.a.rate_hz)
        last_seq = -1
        last_frame_mono = 0.0
        lost = 0
        ever_framed = False

        try:
            while not self._stop_requested:
                t0 = time.monotonic()

                if (t0 - self.t_start) >= self.a.max_seconds:
                    log("WATCHDOG max-seconds (%.0fs) reached -> stop" % self.a.max_seconds)
                    break

                rclpy.spin_once(self.node, timeout_sec=0.0)
                frame, last_seq, _ = self.node.take_if_new(last_seq)

                now = time.monotonic()
                if frame is not None:
                    ever_framed = True
                    last_frame_mono = now
                    self._process_frame(frame)
                    tgt = self._last_target
                    if tgt is None:
                        lost += 1
                        if self.walking and lost >= self.a.lost_grace:
                            self.bridge.send_velocity(0.0, 0.0, 0.0)
                    else:
                        lost = 0
                else:
                    if not ever_framed:
                        log("NO-FRAME")
                    else:
                        stalled = (now - last_frame_mono) > self.a.stall_seconds
                        if stalled:
                            log("NO-FRAME stall=%.1fs -> stop" % (now - last_frame_mono))
                            if self.walking:
                                self.bridge.send_velocity(0.0, 0.0, 0.0)

                if self.drive and self.walking and not self.bridge.alive():
                    log("BRIDGE died -> exiting (loco safed by bridge)")
                    break

                dt = time.monotonic() - t0
                if dt < period:
                    rem = period - dt
                    if rem > 0:
                        time.sleep(rem)
        except KeyboardInterrupt:
            pass
        except Exception as e:  # noqa: BLE001
            log("EXCEPTION %s -> stopping" % e)
        finally:
            self._cleanup()
            try:
                self.node.destroy_node()
            except Exception:  # noqa: BLE001
                pass
            try:
                rclpy.shutdown()
            except Exception:  # noqa: BLE001
                pass
            log("EXIT")

    _last_target = None

    def _process_frame(self, frame):
        tgt = target_from_frame(frame, self.a.marker_size_m, self.a.hfov_deg)
        self._last_target = tgt

        if tgt is None:
            log("NO-MARK")
            return

        rng, bearing = tgt
        bearing_deg = math.degrees(bearing)

        # turn toward marker (flip sign convention as in follow_marker.py)
        vyaw = clamp(-self.a.k_yaw * bearing, self.vyaw_min, self.vyaw_max)
        err = rng - self.a.standoff_m
        vx = self.a.k_vx * err if abs(err) > self.a.deadband_m else 0.0
        vx = clamp(vx, self.vx_min, self.vx_max)

        if self.drive and self.walking:
            self.bridge.send_velocity(vx, 0.0, vyaw)

        log("MARK range=%.2f bearing=%+05.1fdeg vx=%+.2f vyaw=%+.2f%s"
            % (rng, bearing_deg, vx, vyaw, "" if (self.drive and self.walking) else " [preview]"))

    def _cleanup(self):
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
        if self.bridge is not None:
            log("CLEANUP stop+quit bridge")
            self.bridge.shutdown()


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="K1 follow-marker (preview by default; --drive to walk).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true",
                      help="detect + print only, never drive (DEFAULT)")
    mode.add_argument("--drive", action="store_true",
                      help="actually walk: spawn bridge and stream velocities")

    p.add_argument("--topic", default=DEF_TOPIC)
    p.add_argument("--bridge", default="./loco_follow_bridge",
                   help="path to compiled loco_follow_bridge (drive mode)")

    p.add_argument("--marker-size-m", type=float, default=DEF_MARKER_SIZE_M)
    p.add_argument("--standoff-m", type=float, default=DEF_STANDOFF_M)
    p.add_argument("--deadband-m", type=float, default=DEF_DEADBAND_M)
    p.add_argument("--hfov-deg", type=float, default=DEF_HFOV_DEG)

    p.add_argument("--k-yaw", type=float, default=DEF_K_YAW)
    p.add_argument("--k-vx", type=float, default=DEF_K_VX)

    p.add_argument("--vx-min", type=float, default=DEF_VX_MIN)
    p.add_argument("--vx-max", type=float, default=DEF_VX_MAX)
    p.add_argument("--vyaw-min", type=float, default=DEF_VYAW_MIN)
    p.add_argument("--vyaw-max", type=float, default=DEF_VYAW_MAX)

    p.add_argument("--lost-grace", type=int, default=DEF_LOST_GRACE)
    p.add_argument("--rate-hz", type=float, default=DEF_RATE_HZ)
    p.add_argument("--stall-seconds", type=float, default=DEF_STALL_SECONDS)
    p.add_argument("--max-seconds", type=float, default=DEF_MAX_SECONDS)
    p.add_argument("--startup-frame-wait", type=float, default=25.0,
                   help="drive: max seconds to wait for first frame before walk")
    args = p.parse_args(argv)

    if not args.drive:
        args.preview = True
    return args


def main():
    args = parse_args(sys.argv[1:])
    f = Follower(args)
    f.install_signal_handlers()
    f.run()


if __name__ == "__main__":
    main()
