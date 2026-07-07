# bridge_cpp/ — CMake build for the loco_follow_bridge (P2.2)

The C++ locomotion **safety floor** (`../loco_follow_bridge.cpp`) is aarch64 (Booster K1
Orin). `CMakeLists.txt` mirrors the verified g++ recipe from `run_follow.sh` exactly — it
does **not** weaken the floor (IMPLEMENTATION_README.md invariant).

## Build (on the robot, or a matching cross-toolchain)
```bash
cd robot/bridge_cpp
cmake -B build -S .                 # or -DBOOSTER_SDK=/path/to/booster_robotics_sdk
cmake --build build                 # -> build/loco_follow_bridge
cp build/loco_follow_bridge /home/booster/loco_follow_bridge
```

## Why this kills compile-on-first-drive
`run_follow.sh` compiles only when the binary is missing or older than the `.cpp`
(`[ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]`) and always launches with `--bridge "$BIN"`.
So if a **pre-built** `loco_follow_bridge` newer than the source is present at
`/home/booster/`, the launcher skips its fallback compile and drives immediately. The
compile-on-first-drive path remains only as a safety fallback.

**Intended deploy flow (VERIFY ON ROBOT):** build here at deploy time and stage the binary,
instead of relying on the first-drive compile. Wiring the desktop app to run this build
step post-scp is a follow-up; today the launcher's guard already consumes a pre-staged
binary correctly.
