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

# Booster robotics SDK root. PROBED, not assumed: the SDK drop moved
# (Workspace/booster_robotics_sdk -> Workspace/sdk_release) and `sudo ./install.sh` also installs
# include/ + lib/ under /usr/local, so a hard-coded root breaks the build on a re-imaged robot.
# Override with -DBOOSTER_SDK=/path/to/sdk to pin one explicitly.
set(BOOSTER_SDK "" CACHE PATH "Booster robotics SDK root (empty = probe the known locations)")
set(_sdk_candidates "${BOOSTER_SDK}"
                    "/home/booster/Workspace/booster_robotics_sdk"
                    "/home/booster/Workspace/sdk_release"
                    "/usr/local")
set(BOOSTER_INC "")
set(BOOSTER_LIB "")
foreach(_root IN LISTS _sdk_candidates)
  if(_root STREQUAL "" OR NOT EXISTS "${_root}/include/booster/robot/b1/b1_loco_client.hpp")
    continue()
  endif()
  foreach(_lib "${_root}/lib/${CMAKE_SYSTEM_PROCESSOR}/libbooster_robotics_sdk.a"
               "${_root}/lib/libbooster_robotics_sdk.a")
    if(EXISTS "${_lib}")
      set(BOOSTER_INC "${_root}/include")
      set(BOOSTER_LIB "${_lib}")
      break()
    endif()
  endforeach()
  if(NOT BOOSTER_LIB STREQUAL "")
    break()
  endif()
endforeach()

if(BOOSTER_LIB STREQUAL "")
  message(FATAL_ERROR "Booster SDK not found. Need <root>/include/booster/robot/b1/b1_loco_client.hpp "
                      "+ <root>/lib/[${CMAKE_SYSTEM_PROCESSOR}/]libbooster_robotics_sdk.a; probed: ${_sdk_candidates}\n"
                      "Pass -DBOOSTER_SDK=/path/to/sdk.")
endif()
message(STATUS "Booster SDK: ${BOOSTER_INC} + ${BOOSTER_LIB}")

# The source stays flat at robot/loco_follow_bridge.cpp so the app deploys it to /home/booster/
# unchanged; this CMakeLists lives one level down in bridge_cpp/.
add_executable(loco_follow_bridge "${CMAKE_CURRENT_SOURCE_DIR}/../loco_follow_bridge.cpp")
target_include_directories(loco_follow_bridge PRIVATE "${BOOSTER_INC}")
target_link_libraries(loco_follow_bridge PRIVATE "${BOOSTER_LIB}" fastrtps fastcdr pthread)
