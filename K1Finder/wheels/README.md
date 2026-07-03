# Offline rerun-sdk wheel closure for the K1 (aarch64 / cp310)

This dir holds the pip wheels `Ensure-RerunWheels` (K1Finder.ps1) stages to `/home/booster/wheels`
and installs with `pip install --user --no-index`. The `.whl` files are **gitignored** (115 MB of
binaries) — regenerate them on any internet-connected PC with:

```powershell
python -m pip download --only-binary=:all: `
  --python-version 310 --implementation cp --abi cp310 --abi abi3 --abi none `
  --platform manylinux_2_31_aarch64 --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 `
  --dest . "rerun-sdk==0.23.1"
Remove-Item numpy-*.whl   # CRITICAL — see below
```

## ⚠️ Version pin + numpy exclusion (load-bearing, 2026-07-04)

- **`rerun-sdk==0.23.1` is PINNED.** It is the newest rerun release that allows numpy 1.x
  (`numpy>=1.23`). Every release from **0.23.2 onward requires `numpy>=2`**.
- The robot runs **numpy 1.26.3**, and its follow stack — onnxruntime 1.22 (TensorRT EP), cv2 4.11,
  rclpy — is ABI-bound to numpy 1.x. Installing numpy 2 **breaks the validated follow stack** for the
  sake of an optional observability feature. Never stage a numpy wheel from this dir.
- Install is `--user` (`~/.local`): the system `dist-packages` is not writable and must not be touched.
- The laptop side runs rerun 0.33.x — **a 0.33 viewer/reader opens 0.23 `.rrd`s fine** (validated).
  `k1_rerun.py` is API-compatible with both.

Expected contents after regeneration (numpy removed):
`rerun_sdk-0.23.1-cp39-abi3-manylinux_2_31_aarch64.whl`, `pyarrow-*-manylinux_2_28_aarch64.whl`,
`attrs-*.whl`, `pillow-*-aarch64.whl`, `typing_extensions-*.whl`.
