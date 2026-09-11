#!/bin/bash
# CAPTURE launcher for the K1 person-follow (P6.1b). Same as run_follow_demo.sh but forces
# --profile capture -- demo-grade safety PLUS the full recording bundle (--rerun .rrd with RGB+depth+
# intrinsics, and planar odometry) that P7/P8 need. Pre-compiles the bridge so the first drive never
# blocks on g++. args: <mode preview|drive> [topic]; remaining "$@" pass through and OVERRIDE the
# profile (precedence: defaults <- profile <- CLI).
#
# P1.9 (2026-09-08): the pre-compile now goes through the shared bridge_build.sh. Until this fix it
# was a private copy of the g++ line that built the SDK-transport bridge to the same output path
# run_follow.sh builds the ROS-transport bridge to. On the 2026-05 firmware loco RPC is answered
# ONLY by the ROS2 service /booster_rpc_service (the raw B1LocoClient channel returns 100), so every
# capture DRIVE ping-aborted -- and because the app re-pushes the .cpp on every launch, even a
# capture PREVIEW silently clobbered the working binary for the next session.
#
# NOTE (VERIFY ON ROBOT): --rerun + --drive adds loop cost -- confirm the on-Orin loop-cost gate passes
# at capture.yaml's rerun_image_every_n (RERUN_PLAN.md). rerun-sdk must be staged on the robot or the
# sink self-disables (no .rrd; follow still runs).
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: interface workspace for booster_interface/msg/Odometer (odometry recorder). Best-effort.
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# ONE NODE AT A TIME -- the same robot-side backstop as run_follow.sh (exit 5 = REFUSED).
_k1_old=$(pgrep -f 'follow_person_k1\.py'); _k1_rc=$?
if [ "$_k1_rc" -ne 1 ]; then   # 1 = no match; 0 = a node is running; anything else = pgrep failed (fail-closed)
  if [ "$_k1_rc" -eq 0 ]; then
    echo "[run_follow_capture] REFUSED: a follow node is already running (pid $(echo $_k1_old)) -- not starting a second one." >&2
  else
    echo "[run_follow_capture] REFUSED: pgrep failed (exit $_k1_rc) -- cannot confirm that no follow node is running." >&2
  fi
  exit 5
fi
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
# PRE-COMPILE the bridge unconditionally (same as run_follow_demo.sh) -- no compile-on-first-drive.
# Via the SHARED recipe in bridge_build.sh (P1.9); this script's own g++ line used to build the dead
# SDK transport over run_follow.sh's ROS binary. HARD-REQUIRED helper: refuse to start if it is
# missing rather than falling back to a private copy of the recipe (fail-closed).
BRIDGE_BUILD=/home/booster/bridge_build.sh
[ -f "$BRIDGE_BUILD" ] || { echo "bridge_build.sh missing from /home/booster -- redeploy from the app (it is a hard-required helper)." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_capture] COMPILE FAILED"; exit 3; }
K1_LAUNCHER=run_follow_capture
. "$BRIDGE_BUILD"
k1_build_bridge   # exits 3 on failure; sets BRIDGE_BIN
# Belt-and-braces: a truncated/corrupt helper would source without defining the function, and the
# shell would sail past into an exec with an EMPTY --bridge. Refuse instead.
[ -x "${BRIDGE_BIN:-}" ] || { echo "bridge build produced no runnable binary (BRIDGE_BIN='${BRIDGE_BIN:-}') -- bridge_build.sh may be truncated or corrupt." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_capture] COMPILE FAILED"; exit 3; }
# NOTE: this launcher does NOT pass --stream, so the node's decision log (TRACK/LOOP-MS via common.log)
# goes to STDOUT, not stderr. Capture STDOUT into k1_follow.err (+ stderr merged) so the offloaded
# bundle carries the decision lines the P6.4 scorer needs -- otherwise every capture run labels
# 'incomplete'. (run_follow.sh keeps stderr-only because the app launches it WITH --stream, where
# stdout is the binary frame protocol.)
if [ "$MODE" = "drive" ]; then
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BRIDGE_BIN" --topic "$TOPIC" --profile capture "$@" > >(tee -p /home/booster/k1_follow.err) 2>&1
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" --profile capture "$@" > >(tee -p /home/booster/k1_follow.err) 2>&1
fi
