#!/bin/bash
# Stream JPEG frames from a ROS2 camera topic to stdout (length-prefixed).
# ROS log noise is redirected to a file so it never back-pressures the SSH pipe.
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
exec python3 /home/booster/stream_cam.py "$1" "$2" "$3" 2>/home/booster/k1_stream.err
