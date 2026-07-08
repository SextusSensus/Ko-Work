#!/bin/bash
# cam_health.sh -- K1 head-camera health check + escalating auto-recovery (runs ON the robot).
#
# WHY: the head camera intermittently STALLS -- the perception nodes stay alive (discovery lists
# them) but deliver ZERO frames on rgb+depth. The follow needs both >= a floor or it DRIVE-ABORTs.
# On 2026-07-08 this survived TWO power-cycles AND a perception-daemon restart, because the MIPI
# capture layer (tegra VI/NVCSI/RTCPU) initialises only at boot and a userspace restart never
# touches it. So the recovery ladder here is deliberately bounded: try the ONE cheap userspace
# lever once, re-verify, and if that fails STOP and tell the operator the capture is wedged below
# userspace (power-cycle + cool-down / hardware check) instead of silently retrying into a
# DRIVE-ABORT. See the k1-camera-troubleshooting notes.
#
# Usage:
#   cam_health.sh                 DETECT only    -> prints CAM-HEALTHY / CAM-STALLED   (exit 0 / 1)
#   cam_health.sh --recover       detect; if stalled, restart perception ONCE, re-verify, escalate
#                                 (exit 0 recovered/healthy, 1 stalled[detect-only], 2 WEDGED)
# Recovery needs sudo: pass the password in the SUDO_PW env var (the app sets it), else it relies
# on passwordless sudo. Non-motion; only reads camera topics + restarts a daemon.
#
# Options:  --floor HZ (default 8.0, matches the node's DRIVE-WAIT floor)   --win SECS (default 12)
set -u
FLOOR=8.0; WIN=12; RECOVER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --recover) RECOVER=1 ;;
    --floor)   FLOOR="$2"; shift ;;
    --win)     WIN="$2"; shift ;;
    *) echo "cam_health: unknown arg '$1'" >&2 ;;
  esac
  shift
done

source /opt/ros/humble/setup.bash 2>/dev/null
source /opt/booster/BoosterRos2/install/setup.bash 2>/dev/null
RGB=/boostercamera/head/raw/rgb
DEPTH=/boostercamera/head/depth

# ros2 topic hz average over WIN secs -> a bare number (0 if silent). The +3 gives hz time to print.
measure() { timeout $((WIN + 3)) ros2 topic hz "$1" 2>/dev/null | awk '/average rate:/{r=$3} END{print r+0}'; }

# A STALE ros2 CLI discovery daemon reports topics/nodes empty even while they publish (this bit us
# repeatedly during diagnosis) -- refresh it before every measurement so a stale daemon never reads
# as a false stall.
refresh_discovery() { ros2 daemon stop >/dev/null 2>&1; ros2 daemon start >/dev/null 2>&1; sleep 3; }

# Echo "<rgb_hz> <depth_hz> <ok 0|1>".
check() {
  refresh_discovery
  local r d
  r=$(measure "$RGB"); d=$(measure "$DEPTH")
  awk -v r="$r" -v d="$d" -v f="$FLOOR" 'BEGIN{printf "%s %s %d", r, d, ((r>=f && d>=f)?1:0)}'
}

echo "CAM-CHECK floor=${FLOOR}Hz win=${WIN}s recover=${RECOVER}"
read -r R D OK <<<"$(check)"
echo "CAM-RATES rgb=${R}Hz depth=${D}Hz"
if [ "$OK" = "1" ]; then echo "CAM-HEALTHY"; exit 0; fi
echo "CAM-STALLED rgb=${R} depth=${D} (need >=${FLOOR}Hz each)"
[ "$RECOVER" = "1" ] || exit 1

# --- Recovery ladder: one userspace lever, then escalate. ---
echo "CAM-RECOVER restarting booster-daemon-perception (userspace lever) ..."
if [ -n "${SUDO_PW:-}" ]; then
  echo "$SUDO_PW" | sudo -S systemctl restart booster-daemon-perception 2>/dev/null
else
  sudo systemctl restart booster-daemon-perception 2>/dev/null
fi
sleep 35   # nodes relaunch + subscriber-gated capture warmup
read -r R D OK <<<"$(check)"
echo "CAM-RATES-AFTER rgb=${R}Hz depth=${D}Hz"
if [ "$OK" = "1" ]; then echo "CAM-RECOVERED via perception restart"; exit 0; fi

echo "CAM-WEDGED perception restart did NOT restore frames."
echo "  The MIPI capture (tegra VI/NVCSI/RTCPU) inits only at boot; a userspace restart cannot reset it."
echo "  ACTION: full power-cycle + short cool-down. If it recurs across cold boots, the head-camera"
echo "  capture needs a hardware/firmware check. This is a platform issue, NOT a follow-code fault."
exit 2
