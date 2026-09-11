#!/bin/bash
# Shared loco-bridge build step for run_follow{,_demo,_capture}.sh. SOURCED, never executed.
#
# WHY THIS FILE EXISTS (P1.9, 2026-09-08). Every launcher used to carry its OWN copy of the g++
# line, and all three wrote the SAME output path /home/booster/loco_follow_bridge. run_follow.sh
# had been taught (2026-09-02) to build the ROS-transport twin there; the demo/capture launchers
# were never updated and kept compiling the SDK-transport source over it. Two consequences, both
# reproduced:
#   1. A demo/capture DRIVE ran the SDK-transport binary. On the 2026-05 K1 firmware loco RPC is
#      answered ONLY by the ROS2 service /booster_rpc_service -- the raw B1LocoClient channel
#      (rt/LocoApiTopic) times out code 100 -- so that binary ping-aborts every drive.
#   2. It clobbered the working binary for the NEXT session. Even a demo PREVIEW did it: the
#      pre-compile block runs before the mode test, and the app re-pushes loco_follow_bridge.cpp
#      on every launch, so the `-nt` guard fired every single time.
# One copy of the recipe is the fix: a launcher can no longer disagree with another about which
# transport it builds. The transports also now compile to DISTINCT paths (see below), so even a
# future divergence cannot overwrite one with the other.
#
# The old duplication was deliberate ("each launcher is deployed on its own and must stand
# alone"). That fear is answered rather than ignored: this helper is in the app's HARD-REQUIRED
# deploy list (a failed push aborts the launch loudly), and each launcher refuses to start -- with
# the same exit-3 / k1_compile.err / BRIDGE-marker contract -- if it is missing. Fail-closed both
# ways; never a silent fallback to a private copy of the recipe.

# Build the loco bridge and set BRIDGE_BIN to the binary to run. Exits 3 (the app's COMPILE FAILED
# contract) on any failure, so callers do not need to check a return code.
# In:  $K1_LAUNCHER -- log prefix, e.g. run_follow_demo. Out: $BRIDGE_BIN.
k1_build_bridge() {
  local tag="${K1_LAUNCHER:-bridge}"
  local err=/home/booster/k1_compile.err
  local ros_src=/home/booster/loco_follow_bridge_ros.cpp
  local sdk_src=/home/booster/loco_follow_bridge.cpp
  # DISTINCT output paths per transport, on purpose: the SDK build can never land on top of the
  # ROS build again (P1.9). Neither is the legacy shared name /home/booster/loco_follow_bridge --
  # a stale binary left there by an older launcher is now inert rather than silently executed.
  local ros_bin=/home/booster/loco_follow_bridge_ros
  local sdk_bin=/home/booster/loco_follow_bridge_sdk

  # BRIDGE TRANSPORT SELECTION (2026-09-02). The 2026-05 robot firmware answers loco RPC ONLY via
  # the ROS2 service /booster_rpc_service; the raw SDK channel (B1LocoClient over rt/LocoApiTopic)
  # times out 100 on every call. When the ROS-transport twin's source is present, build+use IT;
  # the SDK path below stays as the fallback for robots/firmware where the raw channel still
  # answers (do NOT delete it -- it is the only transport on older drops).
  if [ -f "$ros_src" ]; then
    # CONTENT-keyed, not mtime-keyed: the app re-pushes loco_follow_bridge_ros.cpp on EVERY
    # launch, so any mtime test rebuilds every single time -- a ~40 s cmake burning CPU on an
    # Orin that is already load-9+ and about to need every cycle for the camera pipeline.
    # Hash the source instead and rebuild only when it actually changed. The `strings|grep`
    # guard matters too: it is the only thing that can tell a ROS build from an SDK build that
    # some other tool staged at this path.
    local rh rh_file need
    rh=$(md5sum "$ros_src" 2>/dev/null | cut -d" " -f1)
    rh_file=/home/booster/.bridge_ros.md5
    need=0
    [ -x "$ros_bin" ] || need=1
    [ "$(cat "$rh_file" 2>/dev/null)" = "$rh" ] || need=1
    grep -aq booster_rpc_service "$ros_bin" 2>/dev/null || need=1
    if [ "$need" = 1 ]; then
      echo "[$tag] compiling loco_follow_bridge (ROS transport) ..."
      # Same diagnostics contract as the SDK path: k1_compile.err + BRIDGE stderr marker + exit 3.
      local bros=/home/booster/bridge_ros_build
      mkdir -p "$bros"
      cat > "$bros/CMakeLists.txt" <<'CML'
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
      { cmake -B "$bros/build" -S "$bros" && cmake --build "$bros/build"; } >"$err" 2>&1 \
        || { echo "BRIDGE compile FAILED - see $err" >&2; echo "[$tag] COMPILE FAILED"; exit 3; }
      install -m 755 "$bros/build/loco_follow_bridge_ros" "$ros_bin" \
        || { echo "BRIDGE install FAILED" >>"$err"; echo "BRIDGE compile FAILED - see $err" >&2; echo "[$tag] COMPILE FAILED"; exit 3; }
      : > "$err"   # success -> empty, matching the g++ path's contract
      echo "$rh" > "$rh_file"   # remember what this binary was built from
      echo "[$tag] compiled OK (ROS transport)."
    fi
    BRIDGE_BIN="$ros_bin"
    return 0
  fi

  # SDK-transport FALLBACK (no ROS twin deployed). Its own binary name -- see the ros_bin/sdk_bin
  # comment above.
  if [ ! -x "$sdk_bin" ] || [ "$sdk_src" -nt "$sdk_bin" ]; then
    echo "[$tag] compiling loco_follow_bridge (SDK transport) ..."
    # Booster SDK root, PROBED not hard-coded: the SDK drop moved (Workspace/booster_robotics_sdk
    # -> Workspace/sdk_release) and `sudo ./install.sh` also installs include/ + lib/ under
    # /usr/local, so a hard-coded root silently broke the bridge build (ld: cannot find
    # libbooster_robotics_sdk.a). Takes the first root that has BOTH the loco header and the
    # static lib; $BOOSTER_SDK wins if set.
    local sdk_inc="" sdk_lib="" _r _l
    for _r in "$BOOSTER_SDK" /home/booster/Workspace/booster_robotics_sdk /home/booster/Workspace/sdk_release /usr/local; do
      [ -n "$_r" ] && [ -f "$_r/include/booster/robot/b1/b1_loco_client.hpp" ] || continue
      for _l in "$_r/lib/$(uname -m)/libbooster_robotics_sdk.a" "$_r/lib/libbooster_robotics_sdk.a"; do
        [ -f "$_l" ] && { sdk_inc="$_r/include"; sdk_lib="$_l"; break 2; }
      done
    done
    # Compile diagnostics -> k1_compile.err (the app tails THAT file on exit 3; k1_follow.err still
    # holds the PREVIOUS session here and would masquerade as the compile diagnosis). The BRIDGE-
    # prefixed marker passes the app's stderr whitelist so the failure also shows live.
    [ -n "$sdk_lib" ] || { echo "no Booster SDK found (need <root>/include/booster/robot/b1/b1_loco_client.hpp + <root>/lib/<arch>/libbooster_robotics_sdk.a; probed BOOSTER_SDK, ~/Workspace/booster_robotics_sdk, ~/Workspace/sdk_release, /usr/local)" > "$err"; echo "BRIDGE compile FAILED - see $err" >&2; echo "[$tag] COMPILE FAILED"; exit 3; }
    g++ -std=c++17 "$sdk_src" -I "$sdk_inc" "$sdk_lib" -lfastrtps -lfastcdr -lpthread -o "$sdk_bin" 2>"$err" \
      || { echo "BRIDGE compile FAILED - see $err" >&2; echo "[$tag] COMPILE FAILED"; exit 3; }
    echo "[$tag] compiled OK (SDK transport)."
  fi
  BRIDGE_BIN="$sdk_bin"
}
