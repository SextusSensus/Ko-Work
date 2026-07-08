#!/bin/bash
# Launch the K1 person lock-and-handoff follow. args: <mode preview|drive> [topic]
# Marker = one-time lock onto the human at the marker, then follows THAT PERSON (YOLO) markerlessly;
# re-show the marker to re-seed/recover. preview = detect + print only (never moves). drive = walk to follow (ARM-gated by the app).
# Compiles loco_follow_bridge from source on first drive (verified g++ recipe).
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: also source the Booster interface workspace so booster_interface/msg/Odometer imports for the
# OPTIONAL odometry recorder (--odom-topic). Best-effort: absent -> odom recording self-disables, the
# follow is unaffected (the import is guarded in perception.CamNode).
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# Pre-flight (OPTIMIZATION_PLAN.md Phase 0.1): warn if the Orin GPU clocks aren't pinned. Pinning
# (sudo jetson_clocks) measured a ~25% loop-p99 cut and an ~86% pose-latency-tail cut on 2026-07-06.
# WARN-ONLY: this launcher never escalates privilege -- pin clocks deliberately (manual / NOPASSWD
# sudoers / systemd boot service) as a power+thermal decision.
_gmin=$(cat /sys/class/devfreq/17000000.gpu/min_freq 2>/dev/null)
_gmax=$(cat /sys/class/devfreq/17000000.gpu/max_freq 2>/dev/null)
if [ -n "$_gmin" ] && [ -n "$_gmax" ] && [ "$_gmin" != "$_gmax" ]; then
  echo "[run_follow] WARN: Orin GPU clocks NOT pinned (${_gmin}/${_gmax} Hz). Run 'sudo jetson_clocks' before a session for ~25% lower loop p99 + far lower pose-latency tail." >&2
fi
MODE="${1:-preview}"
TOPIC="${2:-/boostercamera/head/raw/rgb}"
shift 2 2>/dev/null || true   # remaining args ("$@") pass through to the node
                              # (e.g. --stream --standoff-m 1.2 --vx-max 0.18 from the Tracker page)
SDK=/home/booster/Workspace/booster_robotics_sdk
BIN=/home/booster/loco_follow_bridge
SRC=/home/booster/loco_follow_bridge.cpp
if [ "$MODE" = "drive" ]; then
  if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
    echo "[run_follow] compiling loco_follow_bridge ..."
    # Compile diagnostics -> k1_compile.err (the app tails THAT file on exit 3; k1_follow.err still
    # holds the PREVIOUS session here and would masquerade as the compile diagnosis). The BRIDGE-
    # prefixed marker passes the app's stderr whitelist so the failure also shows live.
    g++ -std=c++17 "$SRC" -I "$SDK/include" "$SDK/lib/aarch64/libbooster_robotics_sdk.a" -lfastrtps -lfastcdr -lpthread -o "$BIN" 2>/home/booster/k1_compile.err || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
    echo "[run_follow] compiled OK."
  fi
  # stderr (the node's log lines in --stream mode) must flow to the ssh pipe so K1Finder can show
  # them; tee keeps an on-robot copy too. (Previously 2>file swallowed every diagnostic.)
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BIN" --topic "$TOPIC" "$@" 2> >(tee /home/booster/k1_follow.err >&2)
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" "$@" 2> >(tee /home/booster/k1_follow.err >&2)
fi
