#!/usr/bin/env python3
"""Record an SfM (COLMAP) dataset from the Aurora -- images + poses that aurora_map_densifier.exe
turns into a dense 3D cloud. Works wherever the SDK connects (laptop now; robot once its link is up).

  python sfm_record.py --out FOLDER [--device IP] [--seconds N] [--quality raw|preview] [--stereo]

Stop early with Ctrl+C -- it flushes and closes the dataset cleanly. Walk the device slowly through
the space while recording; SfM needs many viewpoints of each surface.
"""
import argparse, os, signal, sys, time
from slamtec_aurora_sdk import (AuroraSDK, DATARECORDER_TYPE_COLMAP_DATASET,
                                DATARECORDER_TYPE_RAW_DATASET)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output folder for the COLMAP dataset")
    ap.add_argument("--device", default=None, help="device IP (default: discover, then 192.168.11.1)")
    ap.add_argument("--seconds", type=float, default=0.0, help="auto-stop after N s (0 = until Ctrl+C)")
    ap.add_argument("--quality", choices=("raw", "preview"), default="raw")
    ap.add_argument("--stereo", action="store_true", help="record both cameras (denser, larger)")
    ap.add_argument("--file-format", choices=("binary", "text", "all"), default="binary")
    ap.add_argument("--colmap", action="store_true",
                    help="record COLMAP format (what the densifier wants) instead of RAW. NOTE: this "
                         "device rejects COLMAP recording with error -2 (INVALID_ARGUMENT) on the "
                         "current firmware -- confirmed against the official SDK example. RAW works.")
    a = ap.parse_args()
    T = DATARECORDER_TYPE_COLMAP_DATASET if a.colmap else DATARECORDER_TYPE_RAW_DATASET
    os.makedirs(a.out, exist_ok=True)

    s = AuroraSDK()
    dev = None
    try:
        found = s.discover_devices(timeout=5.0)
        if found: dev = found[0]
    except Exception:
        pass
    try:
        if dev is not None and not a.device: s.connect(device_info=dev)
        else: s.connect(connection_string=a.device or "192.168.11.1")
    except Exception as e:
        print("CONNECT-FAIL:", type(e).__name__, e); return 1
    print("connected:", s.is_connected())

    try:
        s.controller.enable_map_data_syncing(True)   # REQUIRED before COLMAP recording (error -2 without it)
    except Exception as e:
        print("map-sync warning:", e)
    dr = s.data_recorder
    try:
        dr.set_option_string(T, "image_quality", a.quality)
        dr.set_option_bool(T, "stereo_recording", a.stereo)
        dr.set_option_string(T, "file_format", a.file_format)
    except Exception as e:
        print("option warning:", e)

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    try:
        dr.start_recording(T, a.out)
        print("RECORDING -> %s   (Ctrl+C to stop)" % a.out)
        t0 = time.monotonic()
        while not stop["now"]:
            time.sleep(1.0)
            try:
                kf = dr.query_status_int(T, "kf_count")
                print("  keyframes: %d   (%.0fs)" % (kf, time.monotonic() - t0))
            except Exception:
                pass
            if a.seconds and (time.monotonic() - t0) >= a.seconds:
                break
    finally:
        try:
            dr.stop_recording(T)
            print("RECORD-OK -> %s" % a.out)
        except Exception as e:
            print("stop error:", e)
        s.disconnect()
    return 0

if __name__ == "__main__":
    sys.exit(main())
