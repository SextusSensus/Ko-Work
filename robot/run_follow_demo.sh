#!/bin/bash
# SUPERVISED DEMO launcher for the K1 person-follow (P1.2). Same as run_follow.sh but (a) forces
# --profile demo (require_heartbeat + obstacle_brake + geofence on; see config/demo.yaml -- this also
# satisfies the fail-closed drive gate) and (b) PRE-COMPILES the bridge unconditionally so the first
# drive never blocks on g++. args: <mode preview|drive> [topic]; remaining "$@" still pass through
# and OVERRIDE the profile (precedence: defaults <- profile <- CLI).
#
# P1.9 (2026-09-08): the pre-compile now goes through the shared bridge_build.sh. Until this fix it
# was a private copy of the g++ line that built the SDK-transport bridge to the same output path
# run_follow.sh builds the ROS-transport bridge to. On the 2026-05 firmware loco RPC is answered
# ONLY by the ROS2 service /booster_rpc_service (the raw B1LocoClient channel returns 100), so every
# demo DRIVE ping-aborted -- and because the app re-pushes the .cpp on every launch, even a demo
# PREVIEW silently clobbered the working binary for the next session.
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: also source the Booster interface workspace so booster_interface/msg/Odometer imports for the
# OPTIONAL odometry recorder (--odom-topic). Best-effort; a guarded import means absence just disables it.
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# ONE NODE AT A TIME -- the same robot-side backstop as run_follow.sh (exit 5 = REFUSED).
_k1_old=$(pgrep -f 'follow_person_k1\.py'); _k1_rc=$?
if [ "$_k1_rc" -ne 1 ]; then   # 1 = no match; 0 = a node is running; anything else = pgrep failed (fail-closed)
  if [ "$_k1_rc" -eq 0 ]; then
    echo "[run_follow_demo] REFUSED: a follow node is already running (pid $(echo $_k1_old)) -- not starting a second one." >&2
  else
    echo "[run_follow_demo] REFUSED: pgrep failed (exit $_k1_rc) -- cannot confirm that no follow node is running." >&2
  fi
  exit 5
fi
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
# PRE-COMPILE the bridge UNCONDITIONALLY (the key difference from run_follow.sh) -- even in preview,
# so the demo dry-run proves the bridge is ready before anyone hits ARM (no compile-on-first-drive).
# The recipe itself is the SHARED one in bridge_build.sh (P1.9): this script used to carry its own
# g++ line writing the SAME path run_follow.sh writes, so a demo run -- preview included -- compiled
# the SDK transport over the ROS binary. HARD-REQUIRED helper: refuse to start if it is missing
# rather than falling back to a private copy of the recipe (fail-closed).
BRIDGE_BUILD=/home/booster/bridge_build.sh
[ -f "$BRIDGE_BUILD" ] || { echo "bridge_build.sh missing from /home/booster -- redeploy from the app (it is a hard-required helper)." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_demo] COMPILE FAILED"; exit 3; }
K1_LAUNCHER=run_follow_demo
. "$BRIDGE_BUILD"
k1_build_bridge   # exits 3 on failure; sets BRIDGE_BIN
# Belt-and-braces: a truncated/corrupt helper would source without defining the function, and the
# shell would sail past into an exec with an EMPTY --bridge. Refuse instead.
[ -x "${BRIDGE_BIN:-}" ] || { echo "bridge build produced no runnable binary (BRIDGE_BIN='${BRIDGE_BIN:-}') -- bridge_build.sh may be truncated or corrupt." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow_demo] COMPILE FAILED"; exit 3; }
# NOTE: no --stream here, so the node's decision log (common.log) is on STDOUT -- capture STDOUT into
# k1_follow.err (+ stderr merged) so an offloaded bundle is scoreable (see run_follow_capture.sh).
if [ "$MODE" = "drive" ]; then
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BRIDGE_BIN" --topic "$TOPIC" --profile demo "$@" > >(tee -p /home/booster/k1_follow.err) 2>&1
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" --profile demo "$@" > >(tee -p /home/booster/k1_follow.err) 2>&1
fi
