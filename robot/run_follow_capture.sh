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
_gmin=$(cat /sys/class/devfreq/17000000.gpu/min_freq 2>/dev/null)
_gmax=$(cat /sys/class/devfreq/17000000.gpu/max_freq 2>/dev/null)
if [ -n "$_gmin" ] && [ -n "$_gmax" ] && [ "$_gmin" != "$_gmax" ]; then
  echo "[run_follow_capture] WARN: Orin GPU clocks NOT pinned (${_gmin}/${_gmax} Hz). Run 'sudo jetson_clocks' before a session for ~25% lower loop p99 + far lower pose-latency tail." >&2
fi
MODE="${1:-preview}"
TOPIC="${2:-/boostercamera/head/raw/rgb}"
shift 2 2>/dev/null || true   # remaining args ("$@") pass through and override the profile
SDK=/home/booster/Workspace/booster_robotics_sdk
BIN=/home/booster/loco_follow_bridge
SRC=/home/booster/loco_follow_bridge.cpp
# PRE-COMPILE the bridge unconditionally (same as run_follow_demo.sh) -- no compile-on-first-drive.
if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
  echo "[run_follow_capture] pre-compiling loco_follow_bridge ..."
  g++ -std=c++17 "$SRC" -I "$SDK/include" "$SDK/lib/aarch64/libbooster_robotics_sdk.a" -lfastrtps -lfastcdr -lpthread -o "$BIN" 2>/home/booster/k1_compile.err || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_capture] COMPILE FAILED"; exit 3; }
  echo "[run_follow_capture] compiled OK."
fi
if [ "$MODE" = "drive" ]; then
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BIN" --topic "$TOPIC" --profile capture "$@" 2> >(tee /home/booster/k1_follow.err >&2)
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" --profile capture "$@" 2> >(tee /home/booster/k1_follow.err >&2)
fi
