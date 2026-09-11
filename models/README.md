# models/

Model blobs for the K1 follow stack. **Not tracked raw in git** (they are large and
regenerable) — `*.onnx` / `*.engine` are gitignored and fetched/exported on demand.
The desktop app (`desktop/K1Finder.ps1`) stages whatever is present here to the robot;
if a blob is missing locally it falls back to the robot-side export path.

## Contents

| File | Role | How to obtain |
|---|---|---|
| `yolo11n-pose.onnx` | YOLO11n-pose model for the gesture lock trigger | `./fetch_models.sh pose` (or the robot's `stage_pose.py` exports it on first gesture use) |
| `osnet_x0_25_msmt17.onnx` | OSNet person-ReID engine (deep re-ID; **required for armed re-lock**) | `./fetch_models.sh osnet` — official architecture + official MSMT17 weights, exported by `export_osnet.py` |
| `wheels/` | offline pip-wheel closure for the optional Rerun feature | see `wheels/README.md` (regenerate per that recipe) |

Without the OSNet ONNX the follow still runs, but deep re-ID degrades to a colour
histogram and the node **auto-refuses armed re-lock** (`--arm-reacquire`); the app's
REID badge shows `HIST` red.

## fetch_models.sh
Needs internet **once** and a python3 with torch/ultralytics — i.e. run it **on the robot**
(or any dev box with those), then copy the results back here so `K1Finder.ps1` can stage
them to a fresh robot offline (`Ensure-ReidModel` / `Ensure-GestureModel` scp whatever is
in this folder).

```bash
models/fetch_models.sh          # everything
models/fetch_models.sh osnet    # just the ReID model
```

## OSNet provenance + the two robot-side steps
`export_osnet.py` downloads nothing itself — `fetch_models.sh` hands it the OFFICIAL
architecture (`KaiyangZhou/deep-person-reid`, self-contained, torch-only) and the OFFICIAL
`osnet_x0_25` MSMT17 checkpoint (`huggingface.co/kaiyangzhou/osnet`, the OSNet author's own
repo). It drops the dataset-specific classifier head (the follow reads the 512-d feature,
never the logits), exports with a **dynamic batch axis** — which the node's pre-warmed
`(1,2,4)` TRT batch set requires; a fixed-batch ONNX silently collapses it to batch 1 — and
refuses to write the file unless the ONNX matches PyTorch (`max|Δ| < 1e-3`, `cos > 0.9999`)
at batches 1/2/4 and the embedding is non-degenerate. Exact file sizes + sha256s are in the
script header.

On the robot:
1. install the ONNX at `/home/booster/reid/osnet_x0_25_msmt17.onnx` (the path
   `K1Finder.ps1` passes as `--reid-engine`; the app scp's it from here automatically), then
2. run `robot/stage_reid.py` **once** — it builds the TensorRT FP16 engines through the
   node's own `identity.ReidEngine` (same options, same `trt_engine_cache_path` = the model's
   directory) and prints the embedding + latency sanity checks. Skipping this doesn't break
   anything; it just moves a multi-second engine build into the first field follow.
