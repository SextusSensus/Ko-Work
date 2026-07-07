# Build the loco_follow_bridge -- the C++ locomotion safety floor (P2.2).
# Mirrors the verified g++ recipe from run_follow.sh EXACTLY (do not weaken -- this is the
# safety floor, IMPLEMENTATION_README.md invariant). The goal is to build at DEPLOY time so
# the first drive never blocks on g++; run_follow.sh's guard then skips its fallback compile
# because the pre-built binary is newer than the .cpp.
#
#   cmake -B build -S . [-DBOOSTER_SDK=/path/to/sdk]
#   cmake --build build            # -> build/loco_follow_bridge
#
# The bridge is aarch64 (Booster K1 Orin), so this builds ON the robot (or a matching
# cross-toolchain). Verified recipe == g++ -std=c++17 <src> -I <sdk>/include
# <sdk>/lib/aarch64/libbooster_robotics_sdk.a -lfastrtps -lfastcdr -lpthread.
cmake_minimum_required(VERSION 3.16)
project(loco_follow_bridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Booster robotics SDK root (override with -DBOOSTER_SDK=...); default = the on-robot path.
set(BOOSTER_SDK "/home/booster/Workspace/booster_robotics_sdk" CACHE PATH "Booster robotics SDK root")
set(BOOSTER_LIB "${BOOSTER_SDK}/lib/aarch64/libbooster_robotics_sdk.a")

if(NOT EXISTS "${BOOSTER_LIB}")
  message(FATAL_ERROR "Booster SDK static lib not found: ${BOOSTER_LIB}\n"
                      "Pass -DBOOSTER_SDK=/path/to/booster_robotics_sdk (must contain lib/aarch64/ + include/).")
endif()

# The source stays flat at robot/loco_follow_bridge.cpp so the app deploys it to /home/booster/
# unchanged; this CMakeLists lives one level down in bridge_cpp/.
add_executable(loco_follow_bridge "${CMAKE_CURRENT_SOURCE_DIR}/../loco_follow_bridge.cpp")
target_include_directories(loco_follow_bridge PRIVATE "${BOOSTER_SDK}/include")
target_link_libraries(loco_follow_bridge PRIVATE "${BOOSTER_LIB}" fastrtps fastcdr pthread)
