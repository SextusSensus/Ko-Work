#!/bin/bash
# Launch the Booster loco command client, reading commands on stdin.
source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
cd /home/booster/Workspace/booster_robotics_sdk/build
exec ./b1_loco_example_client "${1:-127.0.0.1}"
