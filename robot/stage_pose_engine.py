#!/usr/bin/env python3
"""stage_pose_engine.py -- runs ON the Booster K1 Orin (P4.2b). One-shot: build the TensorRT FP16
engine for the gesture pose model from the LOCAL yolo11n-pose.pt, then A/B its latency vs the .onnx.

WHY on-robot: TRT engines are DEVICE-SPECIFIC (built for this Orin's sm87 + this TRT version), so
they must be built here, not shipped from the PC. Uses the already-present /home/booster/yolo11n-pose.pt
-> NO internet needed (offline-capable). Produces /home/booster/yolo11n-pose.engine; the follow node's
--gesture-model can then point at it. Ultralytics runs a .engine with pre/post processing IDENTICAL to
the .onnx path -- only the forward pass moves to TRT FP16, so gesture behaviour is unchanged bar FP16
numerics (validate on-robot: does the hand-raise still lock, faster).

Prints one STAGE-ENGINE-OK / STAGE-ENGINE-FAIL line + a POSE-LAT A/B line, exits 0/1.
Run manually:  python3 /home/booster/stage_pose_engine.py
"""
import os
import sys
import time

OUT_DIR = "/home/booster"
PT     = os.path.join(OUT_DIR, "yolo11n-pose.pt")
ONNX   = os.path.join(OUT_DIR, "yolo11n-pose.onnx")
ENGINE = os.path.join(OUT_DIR, "yolo11n-pose.engine")


def _median_ms(model, n=12):
    """Median end-to-end predict ms on a dummy frame (full ultralytics pipeline: pre+infer+post)."""
    import numpy as np
    dummy = np.zeros((480, 640, 3), dtype=np.uint8)
    model.predict(dummy, verbose=False)          # warm (build/load kernels off the timing)
    ts = []
    for _ in range(n):
        t0 = time.monotonic()
        model.predict(dummy, verbose=False)
        ts.append((time.monotonic() - t0) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2]


def main():
    try:
        from ultralytics import YOLO
    except Exception as e:  # noqa: BLE001
        print("STAGE-ENGINE-FAIL ultralytics import failed (%s)" % e)
        return 1
    if not os.path.exists(PT):
        print("STAGE-ENGINE-FAIL missing %s (needed to build the engine)" % PT)
        return 1
    os.chdir(OUT_DIR)
    if not os.path.exists(ENGINE):
        try:
            # half=True -> FP16 (mirrors the ReID TRT FP16 path). Device-specific build (~1-3 min).
            YOLO(PT).export(format="engine", half=True)
        except Exception as e:  # noqa: BLE001
            print("STAGE-ENGINE-FAIL export failed (%s)" % e)
            return 1
    if not os.path.exists(ENGINE):
        print("STAGE-ENGINE-FAIL no engine produced")
        return 1
    # A/B latency: .onnx (CUDA EP) vs .engine (TRT FP16), SAME ultralytics pipeline -> apples-to-apples.
    try:
        lat_engine = _median_ms(YOLO(ENGINE, task="pose"))
        lat_onnx = _median_ms(YOLO(ONNX, task="pose")) if os.path.exists(ONNX) else float("nan")
        print("POSE-LAT onnx=%.1fms engine=%.1fms (median of 12, dummy 480x640)"
              % (lat_onnx, lat_engine))
    except Exception as e:  # noqa: BLE001
        print("STAGE-ENGINE-OK %s (built; A/B latency skipped: %s)" % (ENGINE, e))
        return 0
    print("STAGE-ENGINE-OK %s" % ENGINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
