#!/bin/bash
# Fetch/export the model blobs into models/ (they are gitignored; see models/README.md).
# Needs internet ONCE and a python3 with torch/ultralytics -- i.e. run this ON THE ROBOT (or any
# dev box with those), then copy the results next to the app so K1Finder stages them offline.
#   models/fetch_models.sh            # everything
#   models/fetch_models.sh osnet      # just the ReID model
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
WHAT="${1:-all}"

if [ "$WHAT" = "all" ] || [ "$WHAT" = "pose" ]; then
  if [ -f "$DIR/yolo11n-pose.onnx" ]; then
    echo "[fetch_models] yolo11n-pose.onnx already present -- skipping."
  else
    echo "[fetch_models] exporting yolo11n-pose.onnx (ultralytics; needs internet once) ..."
    ( cd "$DIR" && python3 -c "from ultralytics import YOLO; YOLO('yolo11n-pose.pt').export(format='onnx')" )
    echo "[fetch_models] done -> $DIR/yolo11n-pose.onnx"
  fi
fi

if [ "$WHAT" = "all" ] || [ "$WHAT" = "osnet" ]; then
  if [ -f "$DIR/osnet_x0_25_msmt17.onnx" ]; then
    echo "[fetch_models] osnet_x0_25_msmt17.onnx already present -- skipping."
  else
    # OSNet has no ultralytics one-liner: pull the OFFICIAL architecture + the OFFICIAL MSMT17
    # checkpoint and export the ONNX ourselves (export_osnet.py verifies it against PyTorch and
    # keeps the batch axis dynamic, which the node's (1,2,4) TRT warm set needs). No pip installs:
    # osnet.py is self-contained (torch only). Provenance/hashes: see models/export_osnet.py.
    echo "[fetch_models] exporting osnet_x0_25_msmt17.onnx (needs internet once + torch) ..."
    T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
    curl -fsSL -o "$T/osnet.py" \
      "https://raw.githubusercontent.com/KaiyangZhou/deep-person-reid/master/torchreid/models/osnet.py"
    curl -fL -o "$T/osnet_x0_25_msmt17.pth" \
      "https://huggingface.co/kaiyangzhou/osnet/resolve/main/osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth"
    cp "$DIR/export_osnet.py" "$T/"
    ( cd "$T" && python3 export_osnet.py osnet_x0_25_msmt17.pth "$DIR/osnet_x0_25_msmt17.onnx" )
    echo "[fetch_models] done -> $DIR/osnet_x0_25_msmt17.onnx"
  fi
  echo "[fetch_models] NOTE: on the ROBOT, also run robot/stage_reid.py once after installing the"
  echo "[fetch_models]       .onnx at /home/booster/reid/ -- it pre-builds the TRT FP16 engines so the"
  echo "[fetch_models]       first field follow never stalls the control loop on an engine build."
fi
