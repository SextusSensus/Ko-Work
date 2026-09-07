#!/usr/bin/env python3
"""Pull the live map off a connected Aurora and save it as a .vslam file.
Run with the Aurora plugged into THIS machine and powered.

  python save_vslam.py [device_ip] [out.vslam]
  (device_ip default: auto-discover, then 192.168.11.1 factory default)
"""
import sys, time
import slamtec_aurora_sdk as sdk

ip  = sys.argv[1] if len(sys.argv) > 1 else None
out = sys.argv[2] if len(sys.argv) > 2 else "map.vslam"
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
        addr = ip or "192.168.11.1"
        print("connecting by string:", addr)
        s.connect(connection_string=addr)
except Exception as e:
    print("CONNECT-FAIL:", type(e).__name__, e); sys.exit(1)
print("connected:", s.is_connected())

mm = s.map_manager
try:
    print("starting download session ->", out)
    mm.start_download_session(out)          # device -> local .vslam
    while mm.is_session_active():
        st = mm.query_session_status()
        print("  status:", st)
        time.sleep(1.0)
    print("SAVE-OK", out)
except Exception as e:
    print("SAVE-FAIL:", type(e).__name__, e)
    try: mm.abort_session()
    except Exception: pass
finally:
    s.disconnect()
