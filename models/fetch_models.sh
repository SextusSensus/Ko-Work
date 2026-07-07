#!/bin/bash
# Fetch/export the model blobs into models/ (they are gitignored; see models/README.md).
# Exports yolo11n-pose.onnx from ultralytics -- needs internet ONCE -- mirroring what the
# robot's robot/stage_pose.py does on first gesture use. Run from the repo root.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/yolo11n-pose.onnx" ]; then
  echo "[fetch_models] yolo11n-pose.onnx already present -- skipping."
else
  echo "[fetch_models] exporting yolo11n-pose.onnx (ultralytics; needs internet once) ..."
  ( cd "$DIR" && python3 -c "from ultralytics import YOLO; YOLO('yolo11n-pose.pt').export(format='onnx')" )
  echo "[fetch_models] done -> $DIR/yolo11n-pose.onnx"
fi
echo "[fetch_models] NOTE: osnet_x0_25_msmt17.onnx (optional deep re-ID) must be supplied out-of-band."
