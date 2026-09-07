#!/usr/bin/env python3
"""Prove the localization chain on a connected Aurora: upload map -> localization mode -> relocalize
-> real pose. Run ON THE ROBOT (or laptop) with the Aurora on the SDK link. No follow stack, no motion.

  python3 aurora_upload_reloc.py map.vslam [--conn 192.168.11.1]

VERIFIED API (device 2.1.1, 2026-09-07). The relocalization calls live on s.CONTROLLER, not on s:
  map_manager.start_upload_session(path)            # the uploaded map does NOT persist across an SDK
                                                    # session -- a fresh connect finds an empty device,
                                                    # so upload every session (this script + the bridge do)
  controller.require_pure_localization_mode()
  controller.require_relocalization()
  data_provider.get_relocalization_status() -> (state, ts)   # SUCCEED == 2 is the only "locked" state
Relocalization only SUCCEEDs once the device SEES the mapped space AND moves a little -- stationary or
pointed away, it sits at FAILED. The map frame is z-up (ground plane = x,y), confirmed on-device.
"""
import argparse
import sys
import time

import slamtec_aurora_sdk as sdk
from slamtec_aurora_sdk import data_types as dt

NM = {0: "NONE", 1: "IN_PROGRESS", 2: "SUCCEED", 3: "FAILED"}
SUCCEED = dt.DEVICE_RELOCALIZATION_STATUS_SUCCEED


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map")
    ap.add_argument("--conn", default="192.168.11.1", help="device connection string / IP")
    ap.add_argument("--watch-s", type=float, default=60.0, help="seconds to watch for a lock")
    a = ap.parse_args()

    s = sdk.AuroraSDK()
    dev = None
    try:
        found = s.discover_devices(timeout=5.0)
        print("discovered", len(found), "device(s)")
        if found:
            dev = found[0]
    except Exception as e:  # noqa: BLE001
        print("discover error:", type(e).__name__, e)
    try:
        if dev is not None:
            s.connect(device_info=dev)
        else:
            print("connecting by string:", a.conn)
            s.connect(connection_string=a.conn)
    except Exception as e:  # noqa: BLE001
        print("CONNECT-FAIL:", type(e).__name__, e)
        return 1
    print("connected:", s.is_connected())

    mm, ctl, dp = s.map_manager, s.controller, s.data_provider
    try:
        if not dp.get_all_map_info():
            print("uploading", a.map, "-> device")
            mm.start_upload_session(a.map)
            while mm.is_session_active():
                time.sleep(1.0)
        print("maps resident:", len(dp.get_all_map_info()))
    except Exception as e:  # noqa: BLE001
        print("UPLOAD-FAIL:", type(e).__name__, e)
        s.disconnect()
        return 1

    try:
        ctl.require_pure_localization_mode(timeout_ms=8000)
        ctl.require_relocalization(timeout_ms=8000)
        print("localization mode set; relocalization requested")
    except Exception as e:  # noqa: BLE001
        print("LOCALIZATION-FAIL:", type(e).__name__, e)
        s.disconnect()
        return 1

    print("MOVE the device slowly through the mapped space; watching for a lock...")
    t0 = time.monotonic()
    locked = False
    last = None
    while time.monotonic() - t0 < a.watch_s:
        try:
            st = dp.get_relocalization_status()[0]
            (tx, ty, tz), _q, _ = s.get_current_pose(use_se3=True)
            mag = abs(tx) + abs(ty) + abs(tz)
            if st != last:
                print("  reloc -> %s" % NM.get(st, st))
                last = st
            if st == SUCCEED and mag > 1e-6:
                print("RELOC-OK  pose=(%.3f, %.3f, %.3f)  [x,y ground plane; z up]" % (tx, ty, tz))
                locked = True
                break
        except Exception as e:  # noqa: BLE001
            print("poll err:", type(e).__name__, e)
            break
        time.sleep(0.2)
    if not locked:
        print("NO LOCK in %.0fs -- point the device at the mapped area and give it motion." % a.watch_s)
    s.disconnect()
    return 0 if locked else 1


if __name__ == "__main__":
    sys.exit(main())
