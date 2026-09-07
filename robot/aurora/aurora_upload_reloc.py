#!/usr/bin/env python3
"""Push a saved .vslam onto a connected Aurora, relocalize, and confirm live pose.
Run ON THE ROBOT with the Aurora mounted on the wired link and powered.
Touches NOTHING in the follow stack -- standalone bring-up, no motion.

  python3 aurora_upload_reloc.py map.vslam [device_ip]
  (device_ip default: auto-discover, then 192.168.127.10 as seen on the robot's wired link)
"""
import sys, time
import slamtec_aurora_sdk as sdk

if len(sys.argv) < 2:
    print("usage: aurora_upload_reloc.py <map.vslam> [device_ip]"); sys.exit(2)
path = sys.argv[1]
ip = sys.argv[2] if len(sys.argv) > 2 else None
s = sdk.AuroraSDK()

dev = None
try:
    found = s.discover_devices(timeout=5.0)
    print("discovered", len(found), "device(s)")
    if found: dev = found[0]
except Exception as e:
    print("discover error:", type(e).__name__, e)

try:
    if dev is not None:
        s.connect(device_info=dev)
    else:
        addr = ip or "192.168.127.10"
        print("connecting by string:", addr)
        s.connect(connection_string=addr)
except Exception as e:
    print("CONNECT-FAIL:", type(e).__name__, e); sys.exit(1)
print("connected:", s.is_connected())

mm = s.map_manager
try:
    print("uploading", path, "-> device")
    mm.start_upload_session(path)                 # local .vslam -> device
    while mm.is_session_active():
        print("  status:", mm.query_session_status())
        time.sleep(1.0)
    print("UPLOAD-OK")
except Exception as e:
    print("UPLOAD-FAIL:", type(e).__name__, e)
    try: mm.abort_session()
    except Exception: pass
    s.disconnect(); sys.exit(1)

# relocalize in the uploaded map, then sample pose
try:
    print("requiring relocalization...")
    s.require_relocalization(timeout_ms=15000)
    print("RELOC-OK")
except Exception as e:
    print("RELOC-FAIL (map may not match the space, or needs movement):", type(e).__name__, e)

t0 = time.monotonic(); n = 0
while time.monotonic() - t0 < 5.0:
    try:
        p = s.get_current_pose(use_se3=True); n += 1
        if n == 1 or n % 50 == 0: print("pose #%d: %s" % (n, p))
    except Exception as e:
        print("pose read error:", type(e).__name__, e); break
    time.sleep(0.01)
print("POSE-RATE %.1f Hz" % (n / 5.0))
s.disconnect()
