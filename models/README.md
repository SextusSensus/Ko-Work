# models/

Model blobs for the K1 follow stack. **Not tracked raw in git** (they are large and
regenerable) — `*.onnx` / `*.engine` are gitignored and fetched/exported on demand.
The desktop app (`desktop/K1Finder.ps1`) stages whatever is present here to the robot;
if a blob is missing locally it falls back to the robot-side export path.

## Contents

| File | Role | How to obtain |
|---|---|---|
| `yolo11n-pose.onnx` | YOLO11n-pose model for the gesture lock trigger | `./fetch_models.sh` (or the robot's `stage_pose.py` exports it on first gesture use) |
| `osnet_x0_25_msmt17.onnx` | OSNet person-ReID engine (optional; deep re-ID) | supply out-of-band; the follow degrades to a colour histogram without it |
| `wheels/` | offline pip-wheel closure for the optional Rerun feature | see `wheels/README.md` (regenerate per that recipe) |

## fetch_models.sh
Exports `yolo11n-pose.onnx` from ultralytics (needs internet once; mirrors what the
robot's `robot/stage_pose.py` does). Run from the repo root:

```bash
models/fetch_models.sh
```
