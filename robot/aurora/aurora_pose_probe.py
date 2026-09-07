#!/usr/bin/env python3
"""Standalone Aurora bring-up probe. Connects, reads pose, checks relocalization -- touches nothing
in the follow stack. Run ONLY with the Aurora plugged into the USB-ethernet link and powered.

  python3 aurora_pose_probe.py [device_ip]      # default: SLAMTEC factory 192.168.11.1

Proves the online architecture end to end before any node code is written:
  1. the SDK reaches the device over the wired link
  2. get_current_pose() streams live 6DOF pose
  3. (if a map is loaded) relocalization reports a status
"""
import sys, time
import slamtec_aurora_sdk as sdk

ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.1"
s = sdk.AuroraSDK()
try:
    s.connect(connection_string=ip)
except TypeError:
    s.connect(ip)                      # signature varies across point releases
print("connected:", s.is_connected())

t0 = time.monotonic()
n = 0
while time.monotonic() - t0 < 5.0:     # 5 s of pose, report rate + a sample
    try:
        p = s.get_current_pose(use_se3=True)
        n += 1
        if n == 1 or n % 30 == 0:
            print("pose #%d: %s" % (n, p))
    except Exception as e:
        print("pose read error:", type(e).__name__, e); break
    time.sleep(0.01)
print("POSE-RATE %.1f Hz over 5s" % (n / 5.0))
s.disconnect()
