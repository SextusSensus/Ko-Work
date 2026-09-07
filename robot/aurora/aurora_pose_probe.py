#!/usr/bin/env python3
"""Standalone Aurora bring-up probe. Discovers the device, connects, reads live pose for 5 s.
Touches NOTHING in the follow stack. Run with the Aurora on the wired link and powered.

  python3 aurora_pose_probe.py [device_ip]   # omit IP to auto-discover (preferred)
"""
import sys, time
import slamtec_aurora_sdk as sdk

want = sys.argv[1] if len(sys.argv) > 1 else None
s = sdk.AuroraSDK()

# Discovery is how the official demos connect -- it finds the device on the link whatever its IP.
dev = None
try:
    found = s.discover_devices(timeout=5.0)
    print("discovered %d device(s)" % len(found))
    for d in found:
        print("  ", d)
    if found:
        dev = found[0]
except Exception as e:
    print("discover error:", type(e).__name__, e)

try:
    if dev is not None:
        s.connect(device_info=dev)
    else:
        ip = want or "192.168.127.10"
        print("no discovery result; trying connection_string", ip)
        s.connect(connection_string=ip)
except Exception as e:
    print("CONNECT-FAIL:", type(e).__name__, e); sys.exit(1)

print("connected:", s.is_connected())
t0 = time.monotonic(); n = 0
while time.monotonic() - t0 < 5.0:
    try:
        p = s.get_current_pose(use_se3=True)
        n += 1
        if n == 1 or n % 50 == 0:
            print("pose #%d: %s" % (n, p))
    except Exception as e:
        print("pose read error:", type(e).__name__, e); break
    time.sleep(0.01)
print("POSE-RATE %.1f Hz over 5s" % (n / 5.0))
try: s.disconnect()
except Exception: pass
