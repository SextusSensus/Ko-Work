#!/usr/bin/env python3
"""stage_pose.py -- runs ON the Booster K1. One-shot: make the gesture pose model exist.

Downloads YOLO11n-pose and exports it to ONNX next to itself
(/home/booster/yolo11n-pose.onnx), which is the path the follow node's
--gesture-model points at. Needs internet ONCE to fetch yolo11n-pose.pt;
after that the .onnx is fully local/offline (per the offline-capable rule).

K1Finder deploys this with the other follow helpers and runs it (only when the
model is missing) the first time you enable Gesture lock / A-B. It prints exactly
one STAGE-POSE-OK / STAGE-POSE-FAIL line and exits 0/1 so the GUI can react.

Run manually:  python3 /home/booster/stage_pose.py
"""
import os
import sys

OUT_DIR = "/home/booster"
ONNX = os.path.join(OUT_DIR, "yolo11n-pose.onnx")


def main():
    if os.path.exists(ONNX):
        print("STAGE-POSE-OK already-present %s" % ONNX)
        return 0
    try:
        from ultralytics import YOLO
    except Exception as e:  # noqa: BLE001
        print("STAGE-POSE-FAIL ultralytics import failed (%s) -- install it or pre-stage the .onnx" % e)
        return 1
    try:
        os.chdir(OUT_DIR)
        # YOLO() auto-downloads yolo11n-pose.pt on first use (needs internet once),
        # then export() writes yolo11n-pose.onnx beside it.
        YOLO("yolo11n-pose.pt").export(format="onnx")
    except Exception as e:  # noqa: BLE001
        print("STAGE-POSE-FAIL export failed (%s) -- check internet for the one-time .pt download" % e)
        return 1
    if os.path.exists(ONNX):
        print("STAGE-POSE-OK exported %s" % ONNX)
        return 0
    print("STAGE-POSE-FAIL no ONNX produced")
    return 1


if __name__ == "__main__":
    sys.exit(main())
