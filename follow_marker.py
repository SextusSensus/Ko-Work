#!/usr/bin/env python3
"""
Booster K1 — follow the ArUco marker (follow_marker.png) taped to your back.

STATUS: bench-testable on a laptop webcam right now. NOT yet hardware-validated
on the K1 — the camera grab and the SDK calls are isolated in get_frame() and
the Robot class so you can wire them to your real setup and tune before walking.

Loop: grab frame -> find marker -> (range, bearing) -> (vx, vyaw) -> walk().
Lost marker -> stop and hold. Ctrl-C -> stop, return to PREP.
"""

import time
import math
import cv2
import numpy as np

# ---------------------------------------------------------------------------
# TUNE THESE
# ---------------------------------------------------------------------------
MARKER_SIZE_M = 0.18      # measure the BLACK square after printing, set it here
STANDOFF_M    = 1.2       # how far behind you the robot holds
DEADBAND_M    = 0.20      # don't fidget within +/- this of standoff
HFOV_DEG      = 70.0      # horizontal field of view of the K1 head camera (check spec)

K_YAW = 0.9               # turn gain  (rad/s per rad of bearing error)
K_VX  = 0.35              # speed gain (m/s per m of range error)

VX_MIN, VX_MAX   = -0.10, 0.30    # forward clamp (keep low until calibrated)
VYAW_MIN, VYAW_MAX = -0.40, 0.40  # turn clamp
LOST_GRACE = 8            # frames with no marker before we stop
RATE_HZ = 10.0

# ---------------------------------------------------------------------------
# CAMERA  — replace get_frame() with the K1 head-camera source (ROS2 topic or
# SDK camera RPC). Default cv2.VideoCapture(0) lets you test on a laptop first.
# ---------------------------------------------------------------------------
_cap = cv2.VideoCapture(0)

def get_frame():
    ok, frame = _cap.read()
    return frame if ok else None

# ---------------------------------------------------------------------------
# ROBOT  — the only SDK-specific code. Method names follow the documented
# Booster Python interface; CONFIRM against your SDK version, they may differ.
# Set DRY_RUN=True to print commands instead of moving (use this first).
# ---------------------------------------------------------------------------
DRY_RUN = True

class Robot:
    def __init__(self, ip="192.168.10.102"):
        if DRY_RUN:
            self.c = None
            return
        import booster
        self.booster = booster
        self.c = booster.BoosterClient(ip)

    def enter_walk(self):
        if DRY_RUN:
            print("[dry] PREP -> wait 3s -> WALK"); return
        self.c.change_mode(self.booster.Mode.PREP)
        time.sleep(3.0)                      # MUST stabilize in PREP before WALK
        self.c.change_mode(self.booster.Mode.WALK)
        time.sleep(2.0)

    def walk(self, vx, vy, vyaw):
        if DRY_RUN:
            print(f"[dry] walk vx={vx:+.2f} vyaw={vyaw:+.2f}"); return
        self.c.walk(vx, vy, vyaw)            # (forward, lateral, angular)

    def stop_and_prep(self):
        if DRY_RUN:
            print("[dry] stop -> PREP"); return
        self.c.walk(0.0, 0.0, 0.0)
        self.c.change_mode(self.booster.Mode.PREP)

# ---------------------------------------------------------------------------
# PERCEPTION
# ---------------------------------------------------------------------------
_adict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
try:
    _detector = cv2.aruco.ArucoDetector(_adict, cv2.aruco.DetectorParameters())  # >=4.7
    def detect(gray):
        corners, ids, _ = _detector.detectMarkers(gray)
        return corners, ids
except AttributeError:
    def detect(gray):
        corners, ids, _ = cv2.aruco.detectMarkers(gray, _adict)                  # older
        return corners, ids

def target_from_frame(frame):
    """Return (range_m, bearing_rad) or None. Pinhole estimate, no calibration
    file needed. bearing > 0 = marker is to the RIGHT of image center."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    corners, ids = detect(gray)
    if ids is None or len(ids) == 0:
        return None

    c = corners[0].reshape(4, 2)                 # first marker, 4 corners
    cx_m = c[:, 0].mean()
    w_img = frame.shape[1]
    focal_px = (w_img / 2.0) / math.tan(math.radians(HFOV_DEG) / 2.0)

    # apparent marker width in px -> range via pinhole
    side_px = (np.linalg.norm(c[0] - c[1]) + np.linalg.norm(c[2] - c[3])) / 2.0
    rng = (MARKER_SIZE_M * focal_px) / max(side_px, 1.0)

    bearing = math.atan2((cx_m - w_img / 2.0), focal_px)
    return rng, bearing

# ---------------------------------------------------------------------------
# CONTROL
# ---------------------------------------------------------------------------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def main():
    robot = Robot()
    robot.enter_walk()
    period = 1.0 / RATE_HZ
    lost = 0
    print("following. Ctrl-C to stop.")
    try:
        while True:
            t0 = time.time()
            frame = get_frame()
            tgt = target_from_frame(frame) if frame is not None else None

            if tgt is None:
                lost += 1
                if lost >= LOST_GRACE:
                    robot.walk(0.0, 0.0, 0.0)      # lost: stop and hold
            else:
                lost = 0
                rng, bearing = tgt
                # turn toward marker. FLIP the sign if it turns the wrong way.
                vyaw = clamp(-K_YAW * bearing, VYAW_MIN, VYAW_MAX)
                # close the gap to standoff, with a deadband
                err = rng - STANDOFF_M
                vx = K_VX * err if abs(err) > DEADBAND_M else 0.0
                vx = clamp(vx, VX_MIN, VX_MAX)
                robot.walk(vx, 0.0, vyaw)

            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        robot.stop_and_prep()
        print("stopped.")

if __name__ == "__main__":
    main()
