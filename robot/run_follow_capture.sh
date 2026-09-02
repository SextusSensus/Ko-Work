#!/bin/bash
# CAPTURE launcher for the K1 person-follow (P6.1b). Same as run_follow_demo.sh but forces
# --profile capture -- demo-grade safety PLUS the full recording bundle (--rerun .rrd with RGB+depth+
# intrinsics, and planar odometry) that P7/P8 need. Pre-compiles the bridge so the first drive never
# blocks on g++. args: <mode preview|drive> [topic]; remaining "$@" pass through and OVERRIDE the
# profile (precedence: defaults <- profile <- CLI).
#
# NOTE (VERIFY ON ROBOT): --rerun + --drive adds loop cost -- confirm the on-Orin loop-cost gate passes
# at capture.yaml's rerun_image_every_n (RERUN_PLAN.md). rerun-sdk must be staged on the robot or the
# sink self-disables (no .rrd; follow still runs).
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: interface workspace for booster_interface/msg/Odometer (odometry recorder). Best-effort.
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# P6.2: reconcile-sweep BEFORE the node starts -- offload a crashed prior session's leftover bundle
# (no-op after a clean session). Best-effort; never blocks a launch.
[ -f /home/booster/offload_run.sh ] && bash /home/booster/offload_run.sh --reconcile >/dev/null 2>&1 || true
_gmin=$(cat /sys/class/devfreq/17000000.gpu/min_freq 2>/dev/null)
_gmax=$(cat /sys/class/devfreq/17000000.gpu/max_freq 2>/dev/null)
if [ -n "$_gmin" ] && [ -n "$_gmax" ] && [ "$_gmin" != "$_gmax" ]; then
  echo "[run_follow_capture] WARN: Orin GPU clocks NOT pinned (${_gmin}/${_gmax} Hz). Run 'sudo jetson_clocks' before a session for ~25% lower loop p99 + far lower pose-latency tail." >&2
fi
MODE="${1:-preview}"
TOPIC="${2:-/boostercamera/head/raw/rgb}"
shift 2 2>/dev/null || true   # remaining args ("$@") pass through and override the profile
# Booster SDK root, PROBED not hard-coded: the SDK drop moved (Workspace/booster_robotics_sdk ->
# Workspace/sdk_release) and `sudo ./install.sh` also installs include/ + lib/ under /usr/local, so a
# hard-coded root silently broke the bridge build (ld: cannot find libbooster_robotics_sdk.a).
# Takes the first root that has BOTH the loco header and the static lib; $BOOSTER_SDK wins if set.
# (Duplicated in run_follow{,_demo,_capture}.sh on purpose -- each launcher is deployed on its own and
# must stand alone; a shared helper that failed to deploy would break every launch.)
SDK_INC=""; SDK_LIB=""
for _r in "$BOOSTER_SDK" /home/booster/Workspace/booster_robotics_sdk /home/booster/Workspace/sdk_release /usr/local; do
  [ -n "$_r" ] && [ -f "$_r/include/booster/robot/b1/b1_loco_client.hpp" ] || continue
  for _l in "$_r/lib/$(uname -m)/libbooster_robotics_sdk.a" "$_r/lib/libbooster_robotics_sdk.a"; do
    [ -f "$_l" ] && { SDK_INC="$_r/include"; SDK_LIB="$_l"; break 2; }
  done
done
BIN=/home/booster/loco_follow_bridge
SRC=/home/booster/loco_follow_bridge.cpp
# PRE-COMPILE the bridge unconditionally (same as run_follow_demo.sh) -- no compile-on-first-drive.
if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
  echo "[run_follow_capture] pre-compiling loco_follow_bridge ..."
  [ -n "$SDK_LIB" ] || { echo "no Booster SDK found (need <root>/include/booster/robot/b1/b1_loco_client.hpp + <root>/lib/<arch>/libbooster_robotics_sdk.a; probed BOOSTER_SDK, ~/Workspace/booster_robotics_sdk, ~/Workspace/sdk_release, /usr/local)" > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_capture] COMPILE FAILED"; exit 3; }
  g++ -std=c++17 "$SRC" -I "$SDK_INC" "$SDK_LIB" -lfastrtps -lfastcdr -lpthread -o "$BIN" 2>/home/booster/k1_compile.err || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_capture] COMPILE FAILED"; exit 3; }
  echo "[run_follow_capture] compiled OK."
fi
# NOTE: this launcher does NOT pass --stream, so the node's decision log (TRACK/LOOP-MS via common.log)
# goes to STDOUT, not stderr. Capture STDOUT into k1_follow.err (+ stderr merged) so the offloaded
# bundle carries the decision lines the P6.4 scorer needs -- otherwise every capture run labels
# 'incomplete'. (run_follow.sh keeps stderr-only because the app launches it WITH --stream, where
# stdout is the binary frame protocol.)
if [ "$MODE" = "drive" ]; then
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BIN" --topic "$TOPIC" --profile capture "$@" > >(tee /home/booster/k1_follow.err) 2>&1
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" --profile capture "$@" > >(tee /home/booster/k1_follow.err) 2>&1
fi
