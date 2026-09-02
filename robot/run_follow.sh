#!/bin/bash
# Launch the K1 person lock-and-handoff follow. args: <mode preview|drive> [topic]
# Marker = one-time lock onto the human at the marker, then follows THAT PERSON (YOLO) markerlessly;
# re-show the marker to re-seed/recover. preview = detect + print only (never moves). drive = walk to follow (ARM-gated by the app).
# Compiles the loco bridge from source on first drive (ROS-transport preferred; see below).
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
# P6.1a: also source the Booster interface workspace so booster_interface/msg/Odometer imports for the
# OPTIONAL odometry recorder (--odom-topic). Best-effort: absent -> odom recording self-disables, the
# follow is unaffected (the import is guarded in perception.CamNode).
source /opt/booster/BoosterRos2Interface/install/setup.bash 2>/dev/null
cd /home/booster
# P6.2: reconcile-sweep BEFORE the node starts -- offload any leftover bundle from a crashed/killed
# prior session that never offloaded (a no-op after a clean session). Best-effort; never blocks a launch.
[ -f /home/booster/offload_run.sh ] && bash /home/booster/offload_run.sh --reconcile >/dev/null 2>&1 || true
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
ROS_SRC=/home/booster/loco_follow_bridge_ros.cpp
if [ "$MODE" = "drive" ]; then
  # BRIDGE TRANSPORT SELECTION (2026-09-02). The 2026-05 robot firmware answers loco RPC ONLY via
  # the ROS2 service /booster_rpc_service; the raw SDK channel (B1LocoClient over rt/LocoApiTopic)
  # times out 100 on every call, so the SDK-transport bridge ping-aborts every drive. When the
  # ROS-transport twin's source is present, build+use IT; the SDK path below stays as the fallback
  # for robots/firmware where the raw channel still answers. The `strings|grep` guard matters: the
  # app re-pushes loco_follow_bridge.cpp on EVERY launch, so mtime alone cannot tell whether $BIN
  # is the ROS build or a stale SDK build compiled over it -- only the content can.
  if [ -f "$ROS_SRC" ]; then
    NEED=0
    [ -x "$BIN" ] || NEED=1
    [ "$ROS_SRC" -nt "$BIN" ] && NEED=1
    grep -aq booster_rpc_service "$BIN" 2>/dev/null || NEED=1
    if [ "$NEED" = 1 ]; then
      echo "[run_follow] compiling loco_follow_bridge (ROS transport) ..."
      # Same diagnostics contract as the SDK path: k1_compile.err + BRIDGE stderr marker + exit 3.
      BROS=/home/booster/bridge_ros_build
      mkdir -p "$BROS"
      cat > "$BROS/CMakeLists.txt" <<'CML'
cmake_minimum_required(VERSION 3.16)
project(loco_follow_bridge_ros CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(rclcpp REQUIRED)
find_package(booster_interface REQUIRED)
add_executable(loco_follow_bridge_ros /home/booster/loco_follow_bridge_ros.cpp)
target_link_libraries(loco_follow_bridge_ros
  rclcpp::rclcpp
  booster_interface::booster_interface__rosidl_typesupport_cpp)
CML
      { cmake -B "$BROS/build" -S "$BROS" && cmake --build "$BROS/build"; } >/home/booster/k1_compile.err 2>&1 \
        || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
      install -m 755 "$BROS/build/loco_follow_bridge_ros" "$BIN" \
        || { echo "BRIDGE install FAILED" >>/home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
      : > /home/booster/k1_compile.err   # success -> empty, matching the g++ path's contract
      echo "[run_follow] compiled OK (ROS transport)."
    fi
  elif [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
    echo "[run_follow] compiling loco_follow_bridge ..."
    # Compile diagnostics -> k1_compile.err (the app tails THAT file on exit 3; k1_follow.err still
    # holds the PREVIOUS session here and would masquerade as the compile diagnosis). The BRIDGE-
    # prefixed marker passes the app's stderr whitelist so the failure also shows live.
    [ -n "$SDK_LIB" ] || { echo "no Booster SDK found (need <root>/include/booster/robot/b1/b1_loco_client.hpp + <root>/lib/<arch>/libbooster_robotics_sdk.a; probed BOOSTER_SDK, ~/Workspace/booster_robotics_sdk, ~/Workspace/sdk_release, /usr/local)" > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
    g++ -std=c++17 "$SRC" -I "$SDK_INC" "$SDK_LIB" -lfastrtps -lfastcdr -lpthread -o "$BIN" 2>/home/booster/k1_compile.err || { echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
    echo "[run_follow] compiled OK."
  fi
  # stderr (the node's log lines in --stream mode) must flow to the ssh pipe so K1Finder can show
  # them; tee keeps an on-robot copy too. (Previously 2>file swallowed every diagnostic.)
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BIN" --topic "$TOPIC" "$@" 2> >(tee /home/booster/k1_follow.err >&2)
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" "$@" 2> >(tee /home/booster/k1_follow.err >&2)
fi
