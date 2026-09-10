#!/bin/bash
# Launch the K1 person lock-and-handoff follow. args: <mode preview|drive> [topic]
# Marker = one-time lock onto the human at the marker, then follows THAT PERSON (YOLO) markerlessly;
# re-show the marker to re-seed/recover. preview = detect + print only (never moves). drive = walk to follow (ARM-gated by the app).
# Compiles the loco bridge from source on first drive, via the SHARED build step bridge_build.sh
# (P1.9, 2026-09-08). Transport choice and the g++/cmake recipes live THERE, in one copy: the
# 2026-05 firmware answers loco RPC only via the ROS2 /booster_rpc_service, and each launcher
# keeping its own recipe is how the demo/capture launchers ended up compiling the dead SDK
# transport over the working ROS binary this script had just built.
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
# AUTO-TUNE HINTS (2026-09-09). The workstation's Auto-Tune-Loop.ps1 pulls each
# finished run, runs eval/rerun_tune.py against it, and scp's the recommended
# YAML patch back to /home/booster/tune_hints/latest.yaml. This block logs its
# contents at the TOP of every follow's stderr so the operator sees the last
# run's tuning recommendations before starting the next one. ADVISORY ONLY --
# the launcher does NOT source or apply the patch (safety-critical config must
# not silently mutate; merge into a profile deliberately). Absent file -> no-op.
# STALENESS GUARD (2026-09-10, operator: "ensure auto improve never gets neglected"). The loop
# had been dead since the previous evening while five runs came and went, and nothing said so --
# this block printed whatever latest.yaml held, so stale hints were indistinguishable from fresh.
# The honest test is NOT file age (old hints are fine if no runs have happened since) but whether
# a run bundle exists that is NEWER than the hints: that proves the analyser never processed it.
# Loud, and on stderr right next to the launch line, so a broken loop cannot stay invisible.
if [ -f /home/booster/tune_hints/latest.yaml ]; then
  echo "==== TUNE HINTS from last run (advisory; not applied) ====" >&2
  sed 's/^/[tune-hints] /' /home/booster/tune_hints/latest.yaml >&2
  _hint_age=$(( $(date +%s) - $(stat -c %Y /home/booster/tune_hints/latest.yaml 2>/dev/null || echo 0) ))
  _newest_run=$(ls -1dt /home/booster/runs/*/ 2>/dev/null | head -1)
  if [ -n "$_newest_run" ]; then
    _run_t=$(stat -c %Y "$_newest_run" 2>/dev/null || echo 0)
    _hint_t=$(stat -c %Y /home/booster/tune_hints/latest.yaml 2>/dev/null || echo 0)
    if [ "$_run_t" -gt "$_hint_t" ]; then
      echo "[run_follow] TUNE-STALE: run bundle $(basename "$_newest_run") is NEWER than the latest tune hints (hints are ${_hint_age}s old)." >&2
      echo "[run_follow] TUNE-STALE: the auto-improve loop did not process the last run -- it is probably not running." >&2
      echo "[run_follow] TUNE-STALE: on the workstation check: Get-Content (Get-ChildItem 'runtime\\runs\\_autotune_logs\\autotune_*.log' | Sort LastWriteTime -Desc | Select -First 1)" >&2
      echo "[run_follow] TUNE-STALE: restart it by running desktop\\Auto-Tune-Service.ps1, or re-logon (Startup folder launches it)." >&2
    else
      echo "[run_follow] tune hints are CURRENT (newer than the last run bundle) -- auto-improve loop is alive." >&2
    fi
  fi
  echo "==== end TUNE HINTS ====" >&2
else
  echo "[run_follow] TUNE-STALE: /home/booster/tune_hints/latest.yaml is MISSING -- the auto-improve loop has never delivered hints to this robot." >&2
fi
# SESSION-LENGTH OVERRIDE (2026-09-10, operator: "it should be able to record for 2000 seconds").
# K1Finder passes --max-seconds 1200 on the CLI and CLI beats the config profile by design, so
# config/capture.yaml alone could not lengthen an app-launched run. argparse takes the LAST
# occurrence of a flag, so appending it AFTER "$@" wins over whatever the app sent.
# Explicit, not silent: the effective value is echoed to stderr on every launch so it sits in
# k1_follow.err right next to the launch line and can never be a mystery post-run.
# Override per-launch with K1_MAX_SEC=<seconds> if a specific run needs something different.
# 2000 s outlasts a battery, so in practice the run ends when the operator or the pack decides.
# NOT unlimited on purpose: 0 trips the node's fail-closed gate for a DRIVING session unless
# --allow-unbounded-drive is also passed, and keeping a finite cap leaves the runaway backstop in
# place. The operator deadman, obstacle brake and C++ staleness floor are all unaffected either way.
K1_MAX_SEC="${K1_MAX_SEC:-2000}"
echo "[run_follow] session cap: --max-seconds ${K1_MAX_SEC} (appended after app args; overrides any earlier --max-seconds)" >&2
MODE="${1:-preview}"
TOPIC="${2:-/boostercamera/head/raw/rgb}"
shift 2 2>/dev/null || true   # remaining args ("$@") pass through to the node
                              # (e.g. --stream --standoff-m 1.2 --vx-max 0.18 from the Tracker page)
if [ "$MODE" = "drive" ]; then
  # The bridge build lives in ONE place (bridge_build.sh) so no launcher can compile a different
  # transport over the binary another launcher just built -- see the P1.9 note at the top of that
  # file. HARD-REQUIRED: refuse to drive if it is missing rather than falling back to a private
  # copy of the recipe (fail-closed; the app deploys it alongside this script).
  BRIDGE_BUILD=/home/booster/bridge_build.sh
  [ -f "$BRIDGE_BUILD" ] || { echo "bridge_build.sh missing from /home/booster -- redeploy from the app (it is a hard-required helper)." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
  K1_LAUNCHER=run_follow
  . "$BRIDGE_BUILD"
  k1_build_bridge   # exits 3 on failure; sets BRIDGE_BIN
  # Belt-and-braces: a truncated/corrupt helper would source without defining the function, and
  # the shell would sail past into an exec with an EMPTY --bridge. Refuse instead.
  [ -x "${BRIDGE_BIN:-}" ] || { echo "bridge build produced no runnable binary (BRIDGE_BIN='${BRIDGE_BIN:-}') -- bridge_build.sh may be truncated or corrupt." > /home/booster/k1_compile.err; echo "BRIDGE compile FAILED - see /home/booster/k1_compile.err" >&2; echo "[run_follow] COMPILE FAILED"; exit 3; }
  # stderr (the node's log lines in --stream mode) must flow to the ssh pipe so K1Finder can show
  # them; tee keeps an on-robot copy too. (Previously 2>file swallowed every diagnostic.)
  exec python3 -u /home/booster/follow_person_k1.py --drive --bridge "$BRIDGE_BIN" --topic "$TOPIC" "$@" --max-seconds "$K1_MAX_SEC" 2> >(tee /home/booster/k1_follow.err >&2)
else
  exec python3 -u /home/booster/follow_person_k1.py --preview --topic "$TOPIC" "$@" --max-seconds "$K1_MAX_SEC" 2> >(tee /home/booster/k1_follow.err >&2)
fi
