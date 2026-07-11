# K1 Phases 7–8 — Windows-Native Desktop Setup Guide

Target: the **desktop** workstation (Ryzen 9 5900X / 64 GB / RTX 3080 Ampere, sm_86) running **Windows 11
native** (not WSL2). This box does P7 (LeRobot-shaped ingest + small ACT policy train + ONNX export) and
P8 (Open3D RGBD reconstruction). **No TensorRT here** — the FP16 engine is built on the Jetson.

Produced by a web-verified research pass (3 research lenses + 2 adversarial verifiers, 2026-07-08).

> **Verification legend:**
> - ✅ **version-verified** against PyPI / download.pytorch.org / official docs on 2026-07-08, and/or by
>   reading the repo source.
> - ⚠️ **best-effort pin — confirm on install:** correct at research time, but these index pages drift;
>   do the final visual check on the desktop.

**Automation:** [`desktop/Setup-Workstation.ps1`](../desktop/Setup-Workstation.ps1) creates a venv and
installs a requirements file; [`requirements-p7.txt`](../requirements-p7.txt) /
[`requirements-p8.txt`](../requirements-p8.txt) carry the pins. Torch is installed separately (see §2).
Get run bundles onto this box with [`desktop/Sync-Runs.ps1`](../desktop/Sync-Runs.ps1) from the laptop.

---

## 1. Prerequisites

**Install Python 3.12 (64-bit)** from python.org (check "Add to PATH"). Confirm `python --version` →
`3.12.x`.

Python 3.12 is the **keystone pin**, and the binding constraint is **Open3D**, not lerobot: Open3D
0.19.0 (latest stable) ships a Windows `cp312` wheel but **no `cp313` Windows wheel** ✅. Python 3.12
also satisfies torch (`>=3.10`), pyarrow, opencv, onnxruntime, and rerun-sdk. Going to 3.13/3.14 loses
Open3D on Windows and forces a source build. **Do not use 3.13+.**

**NVIDIA driver:** a recent Game Ready / Studio driver (≥ 566 is comfortable for the CUDA 12.x runtime).
torch bundles its own CUDA libraries, so **no CUDA Toolkit install is needed** — nothing here compiles
from source.

**One venv or two?**
- **P7 (training) and P8 (reconstruction) share ONE venv.** They co-install cleanly (numpy 2.x, scipy,
  pyarrow, opencv, onnx). The only rule is "install `opencv-contrib-python` OR `opencv-python`, never
  both" — not a P7-vs-P8 clash. ✅
- **lerobot, if ever wanted, needs its OWN throwaway venv.** lerobot main pins `torch>=2.7,<2.12` and
  `torchvision<0.27`, which **directly conflicts** with the P7 pin (torch 2.12.1 / torchvision 0.28.0)
  ✅. Per §2 you **don't need lerobot at all**, so this is optional and off the critical path.

**numpy 2.x is correct here.** Do **not** inherit the robot's `numpy==1.26.3` pin — that lives only in
`robot/requirements.txt` / `robot/pyproject.toml` as a Jetson ABI floor (onnxruntime-TRT/opencv/rclpy)
✅. On the desktop numpy 2.x is required (rerun-sdk 0.33 pulls `numpy>=2`) and supported by every
package below.

---

## 2. P7 install — ingest, train, ONNX export

> **REQUIRED (and missing from naive kits): `rerun-sdk >= 0.33`.** `eval/rrd_to_lerobot.py:47` does
> `from rerun.experimental import RrdReader` inside `read_rrd()`, and `main()` (line 351) calls
> `read_rrd()` **unconditionally, before** the lerobot-vs-RAW branch (line 359). `rrd_loop_stats.py:8`
> documents that `RrdReader` needs **rerun-sdk ≥ 0.33** (the robot's pinned 0.23.1 lacks it). **Without
> rerun-sdk ≥ 0.33 the ingest tool ImportErrors before writing anything — the RAW fallback avoids
> _lerobot_, not _rerun_.** ✅ repo-verified. ⚠️ Confirm a `cp312 win_amd64` rerun-sdk 0.33 wheel resolves.

Run in order, in the Python 3.12 venv:

```powershell
py -3.12 -m venv C:\k1\venv
C:\k1\venv\Scripts\activate
python -m pip install --upgrade pip
```

**Step 1 — CUDA PyTorch FIRST, from the cu126 index (Ampere-safe):**

```powershell
pip install torch==2.12.1+cu126 torchvision==0.28.0+cu126 --index-url https://download.pytorch.org/whl/cu126
```

- Install torch **before** anything else so nothing drags in a CPU or wrong-CUDA build. ✅
- **You MUST pass `--index-url .../cu126`.** A bare `pip install torch` on the 2.12 line now serves the
  **CUDA-13.0** wheel by default (Blackwell, needs driver ≥ 580) — wrong for a 3080. cu126 (CUDA 12.6)
  fully supports Ampere sm_86 and is the conservative pin. ✅
- ⚠️ **Confirm on the box:** eyeball `https://download.pytorch.org/whl/cu126/torch/` for
  `torch-2.12.1+cu126` `cp312 win_amd64`. If absent, the verified fallback pair is
  **`torch==2.11.0+cu126 torchvision==0.27.1+cu126`**. Do **not** pin torch 2.13 on Windows yet.
  `cu128` (CUDA 12.8) is an equally valid Ampere alternative.

**Step 2 — the rest of P7 (from `requirements-p7.txt`):**

```powershell
pip install -r requirements-p7.txt
```

which pins `rerun-sdk>=0.33,<0.34`, `numpy>=2.0,<3`, `scipy`, `pyarrow`, `imageio` + `imageio-ffmpeg==0.6.0`
(its Windows wheel **bundles `ffmpeg.exe` ~60 MB** — no system ffmpeg, no PATH surgery ✅), `onnx`, and
`onnxruntime==1.27.0` (CPU is sufficient for the P7.3a torch-vs-ONNX parity check; the FP16 TRT engine is
a Jetson artifact) ✅.

**lerobot-on-Windows verdict — the load-bearing decision:**

> **Do NOT install the lerobot library. Use the repo's RAW-layout path instead — Windows-native is NOT a
> blocker.**

- **The ingest tool already writes RAW without lerobot.** ✅ `try_write_lerobot()` (line 295) wraps the
  `lerobot` import in try/except and returns `False` on ImportError; `main()` then calls `write_raw()`
  (line 362), which imports only numpy, pyarrow, and imageio (+ stdlib) and emits a **parquet + mp4 +
  meta-json** layout mirroring LeRobot v3 field-for-field (validated on a real robot `.rrd` per
  `docs/LEROBOT_EXPORT.md`). This fully satisfies P7.1 ingest and feeds P7.2 training.
- **So "does lerobot install on Windows?" gates nothing.** The RAW path needs no lerobot, and installing
  it would fight the training env (torch conflict above).
- **If you ever want the canonical lerobot writer** (not needed for P7.1/P7.2): a separate throwaway venv
  with `torch<2.12`. ⚠️ Caveat: the tool imports the **legacy** `lerobot.common.datasets.lerobot_dataset`
  path; recent lerobot moved to `lerobot.datasets`, so on lerobot 0.6.0 that import may fail (caught →
  silent RAW fallback). Verify the import against the installed version before relying on it.

---

## 3. P8 install — Open3D reconstruction + AprilTag

Same venv as P7. Install from `requirements-p8.txt`:

```powershell
pip install -r requirements-p8.txt
```

which pins `open3d==0.19.0` + `opencv-contrib-python==5.0.0.93` (+ numpy/scipy).

- **AprilTag: use OpenCV's built-in `cv2.aruco` AprilTag dictionaries — zero extra dependency.**
  `opencv-contrib-python` ships `cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)` (and
  16h5/25h9/36h10), and you already need OpenCV for P8. ✅ Recommended path.
  - **Install `opencv-contrib-python` OR `opencv-python`, never both** — they clobber each other's `cv2`.
    For P8 pick **contrib** (a superset with aruco).
  - **Optional dedicated detector**, only if aruco proves insufficient: on Windows prefer
    **`pyapriltags==3.4.3.1`** (AprilRobotics apriltag3; `py3-none-win_amd64` wheel, no compiler). ⚠️
    **Avoid `pupil-apriltags` on Windows** — its `cp312 win_amd64` wheel is not guaranteed, so pip may
    fall to a source build.
- **Open3D on Windows is CPU-only** — there is no CUDA Open3D Windows wheel. ✅ Fine and intended: with
  64 GB RAM the CPU handles RGBD odometry, pose-graph loop closure, and ScalableTSDF integration for
  offline reconstruction (just not real-time). `open3d-cpu` is Linux-only — on Windows use `open3d`. **Do
  not** pull in RTAB-Map (heavy C++/Qt, no clean pip path, redundant with Open3D).

> ⚠️ **opencv 5.0.0.93** is the latest (2026-07-02, `cp37-abi3 win_amd64`, runs on 3.12). To stay on the
> mature 4.x aruco API, `opencv-contrib-python>=4.11,<5` is an equally valid pin — both expose the
> AprilTag dictionaries.

**No TensorRT anywhere** — the FP16 engine is a Jetson artifact. Do **not** install `onnxruntime-gpu`
here either: its 1.27.0 GPU wheels are CUDA-13 only, and you don't need GPU ONNX for a parity check. ✅

---

## 4. Smoke tests

Run each in the activated venv; all should print without error.

```powershell
# 1) CUDA is live on the 3080 (MUST print True + the device name; cuda == 12.6):
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))"

# 2) The bundled ffmpeg the RAW mp4 writer needs -- encode a 1s test mp4 via imageio:
python -c "import numpy as np, imageio, imageio_ffmpeg as f; print('ffmpeg:', f.get_ffmpeg_exe()); w=imageio.get_writer('smoke.mp4', format='FFMPEG', codec='libx264', fps=10); [w.append_data((np.random.rand(64,64,3)*255).astype('uint8')) for _ in range(10)]; w.close(); print('mp4 OK')"

# 3) Open3D imports and a trivial TSDF volume integrates (CPU):
python -c "import open3d as o3d; v=o3d.pipelines.integration.ScalableTSDFVolume(voxel_length=0.01, sdf_trunc=0.04, color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8); print('open3d', o3d.__version__, 'TSDF OK')"

# 4) AprilTag detect via OpenCV aruco (no extra dep):
python -c "import cv2, numpy as np; d=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11); img=cv2.aruco.generateImageMarker(d,0,200); c,ids,_=cv2.aruco.ArucoDetector(d).detectMarkers(cv2.cvtColor(img,cv2.COLOR_GRAY2BGR)); print('AprilTag ids:', None if ids is None else ids.ravel())"

# 5) The ingest reader imports (proves rerun-sdk >= 0.33 -- the dep naive kits miss):
python -c "from rerun.experimental import RrdReader; import onnxruntime as ort; print('RrdReader + onnxruntime', ort.__version__, 'OK')"
```

If test 1 prints `False`, you got a CPU torch wheel — reinstall from the cu126 index. Test 2/4 failing
means the wrong opencv variant or a missing ffmpeg wheel. Test 5 failing means rerun-sdk is missing or
< 0.33.

---

## 5. Known Windows gotchas + open items

1. **`pip install torch` gives the wrong build.** Bare install serves the CUDA-13.0 wheel (Blackwell) on
   the 2.12 line — wrong for Ampere and needs driver ≥ 580. Always pass `--index-url .../cu126`, and
   install torch **first**. ✅
2. **rerun-sdk ≥ 0.33 is mandatory for ingest.** The `.rrd` reader runs before the lerobot/RAW branch, so
   no fallback avoids it. ✅ repo-verified. ⚠️ Confirm the `cp312 win_amd64` 0.33 wheel resolves.
3. **opencv variant collision.** Install `opencv-contrib-python` **only** (you need `cv2.aruco`); never
   alongside `opencv-python`. Likewise exactly one of `onnxruntime` / `onnxruntime-gpu` — here CPU
   `onnxruntime`. ✅
4. **Open3D is CPU-only on Windows and caps at Python 3.12.** That's the reason for the 3.12 keystone pin
   and why RTAB-Map / GPU-TSDF are out of scope. CPU TSDF on 64 GB is sufficient. ✅
5. ⚠️ **Final index re-check on the actual desktop.** No network was available during research to
   `pip`-resolve the exact `torch 2.12.1+cu126` + `torchvision 0.28.0+cu126` + `open3d 0.19.0` +
   `rerun-sdk 0.33` + `numpy 2.x` set together in one 3.12 env. Do the visual wheel check for torch
   (fallback 2.11.0+cu126 / 0.27.1+cu126), confirm rerun-sdk 0.33 and open3d 0.19.0 cp312 wheels, then run
   the §4 smoke tests. Everything is version-verified as of 2026-07-08 but these pages drift.
