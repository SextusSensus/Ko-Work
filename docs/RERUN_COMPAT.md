# Rerun cross-version compatibility (robot 0.23.1 write / workstation 0.33 read)

**Question (raised 2026-07-10):** the Booster K1 is pinned to **rerun-sdk 0.23.1** (the newest release
that still allows numpy 1.x — the robot's onnxruntime-TRT / opencv / rclpy ABI floor). The offline
ingest + reconstruction tools read the `.rrd` with **rerun >= 0.33** (`rrd_to_lerobot.read_rrd` uses
`rerun.experimental.RrdReader`, added in 0.33). Is a 0.23-written `.rrd` actually readable by the 0.33
pipeline, and does the P6.1 `pinhole()` call even work on 0.23?

## Verdict: VERIFIED WORKING — no compatibility problem for the pipeline as used.

Tested empirically on 2026-07-10 (`eval/compat/`): a `.rrd` written by the **robot's exact
`RerunSink`** under real **rerun 0.23.1 + numpy 1.26.4** was read by the **0.33.1** ingest path.

- **Robot side (0.23.1):** `RerunSink` logs Scalars / Image / DepthImage / TextLog / Boxes2D **and the
  P6.1 `rr.Pinhole(resolution=, focal_length=, principal_point=)`** with **zero faults**. The P6.1
  intrinsics addition is 0.23-safe. (It's also fault-latched — even a bad call would no-op, never crash
  the follow — and `intrinsics.json` is a sidecar backstop regardless.)
- **Workstation side (0.33.1):** `read_rrd` + `assemble_episode` load **all 7 scalar streams, the RGB
  images, and the depth frames**, producing a valid `(T, 7)` episode. `RRD-CROSSVER-OK`.

So P7.1 ingest (scalars + RGB) and P8 reconstruction (depth + intrinsics) both get what they need from a
real 0.23 `.rrd`.

## Why it works, and the honest caveat

Rerun only *guarantees* `.rrd` backward-compat across **adjacent** minor versions ("you can open a 0.23
`.rrd` in 0.24, but maybe not in 0.31" — [rerun #6410](https://github.com/rerun-io/rerun/issues/6410),
[release 0.23](https://github.com/rerun-io/rerun/releases/tag/0.23.0)). 0.23 → 0.33 is well outside that
window. It works anyway because **"all core data primitives continue to load — text, images, videos,
point clouds, scalars, and tensors"** via auto-migration on load, and the pipeline reads *exactly* those
core types. The types rerun warns may break across big jumps (AnnotationContext, ViewCoordinates) are
**not** read by the ingest path. Pinhole is the one camera-ish type — and it is doubly safe: it loaded
fine in the test, and its data is independently carried by the `intrinsics.json` sidecar.

## Load-bearing constraints (do not drift from these)

1. **Pin the workstation rerun to a version verified against 0.23** — currently **0.33.1** (`requirements-p7.txt`:
   `rerun-sdk>=0.33,<0.34`). Do **not** blindly bump past this without re-running the check below: a
   future rerun could drop 0.23-era chunk migration, silently breaking ingest of the whole existing
   run corpus.
2. **The robot stays on 0.23.1** (numpy-1.x floor). Never "upgrade the robot's rerun to match" — that
   breaks the follow stack's ABI.
3. **Keep `intrinsics.json` (P6.1 sidecar)** as the camera-model source of record — never depend solely
   on the `.rrd` Pinhole surviving a cross-version read.

## Re-verify (run when the workstation rerun version changes, or a rerun release lands)

Needs two interpreters: a rerun-0.23.1 one (numpy<2) and the workstation's (rerun>=0.33).

```
# 1) write with the robot's version (rerun==0.23.1 env):
python eval/compat/rrd_write_robotver.py /tmp/robot_ver.rrd
# 2) read with the workstation version (rerun>=0.33 env):
python eval/compat/rrd_read_wsver.py   /tmp/robot_ver.rrd     # -> RRD-CROSSVER-OK
```

A 0.23.1 env on a numpy-2 box: `conda create -n rerun023 python=3.11 "numpy<2" -y && \
conda run -n rerun023 pip install rerun-sdk==0.23.1`.
