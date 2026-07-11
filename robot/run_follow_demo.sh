#!/bin/bash
# SUPERVISED DEMO launcher for the K1 person-follow (P1.2). Same as run_follow.sh but (a) forces
# --profile demo (require_heartbeat + obstacle_brake + geofence on; see config/demo.yaml -- this also
# satisfies the fail-closed drive gate) and (b) PRE-COMPILES the bridge unconditionally so the first
# drive never blocks on g++. args: <mode preview|drive> [topic]; remaining "$@" still pass through
# and OVERRIDE the profile (precedence: defaults <- profile <- CLI).
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: also source the Booster interface workspace so booster_interface/msg/Odometer imports for the
# OPTIONAL odometry recorder (--odom-topic). Best-effort; a guarded import means absence just disables it.
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# P6.2: reconcile-sweep BEFORE the node starts -- offload a crashed prior session's leftover bundle
# (no-op after a clean session). Best-effort; never blocks a launch.
[ -f /home/booster/offload_run.sh ] && bash /home/booster/offload_run.sh --reconcile >/dev/null 2>&1 || true
# Pre-flight (OPTIMIZATION_PLAN.md Phase 0.1): warn if the Orin GPU clocks aren't pinned. Pinning
# (sudo jetson_clocks) measured a ~25% loop-p99 cut and an ~86% pose-latency-tail cut on 2026-07-06.
# WARN-ONLY: this launcher never escalates privilege -- pin clocks deliberately (manual / NOPASSWD
# sudoers / systemd boot service) as a power+thermal decision.
_gmin=$(cat /sys/class/devfreq/17000000.gpu/min_freq 2>/dev/null)
_gmax=$(cat /sys/class/devfreq/17000000.gpu/max_freq 2>/dev/null)
if [ -n "$_gmin" ] && [ -n "$_gmax" ] && [ "$_gmin" != "$_gmax" ]; then
  echo "[run_follow_demo] WARN: Orin GPU clocks NOT pinned (${_gmin}/${_gmax} Hz). Run 'sudo jetson_clocks' before a session for ~25% lower loop p99 + far lower pose-latency tail." >&2
fi
MODE="${1:-preview}"
TOPIC="${2:-/boostercamera/head/raw/rgb}"
shift 2 2>/dev/null || true   # remaining args ("$@") pass through and override the profile
SDK=/home/booster/Workspace/booster_robotics_sdk
BIN=/home/booster/loco_follow_bridge
SRC=/home/booster/loco_follow_bridge.cpp
# PRE-COMPILE the bridge UNCONDITIONALLY (the key difference from run_follow.sh) -- even in preview,
# so the demo dry-run proves the bridge is ready before anyone hits ARM (no compile-on-first-drive).
# Same verified g++ recipe + exit-3 + k1_compile.err + BRIDGE marker contract the app already tails.
if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
  echo "[run_follow_demo] pre-compiling loco_follow_bridge ..."
  g++ -std=c++17 "$SRC" -I "$SDK/include" "$SDK/lib/aarch64/libbooster_robotics_sdk.a" -lfastrtps -lfastcdr -lpthread -o "$BIN" 2>/home/booster/k1_compile.err || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_demo] COMPILE FAILED"; exit 3; }
  echo "[run_follow_demo] compiled OK."
fi
# NOTE: no --stream here, so the node's decision log (common.log) is on STDOUT -- capture STDOUT into
# k1_follow.err (+ stderr merged) so an offloaded bundle is scoreable (see run_follow_capture.sh).
if [ "$MODE" = "drive" ]; then
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BIN" --topic "$TOPIC" --profile demo "$@" > >(tee /home/booster/k1_follow.err) 2>&1
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" --profile demo "$@" > >(tee /home/booster/k1_follow.err) 2>&1
fi
