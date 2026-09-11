# ============================================================================
#  K1 Finder - Booster K1 discovery, SSH/files, live camera view & control
#  Native Windows desktop app (PowerShell + Windows Forms, zero dependencies)
#
#  Tabs:
#    1. Discover    - scan the LAN, find/rank the K1, verify reachability
#    2. SSH         - SSH terminal, passwordless key, file upload (scp)
#    3. Live        - live MJPEG-over-SSH stream from the K1 head camera
#    4. Control     - clickable menu of all loco commands (drives the SDK CLI)
#    5. Files       - robot SDK file browser
#    6. Tracker     - marker-seeded person-follow cockpit
#    7. Local Map   - interactive 3D short-horizon occupancy (WebView2 / browser)
#
#  Verified against the robot's own SDK (booster_robotics_sdk):
#    - Loco CLI: b1_loco_example_client <iface>; loopback 127.0.0.1 reaches the
#      on-robot loco service (gft returns real transforms).
#    - Camera: /boostercamera/head/rgb (sensor_msgs/Image, NV12); streamed via a
#      ROS2->JPEG pump over SSH. Camera must be publishing for frames to appear.
#    - SSH via Windows OpenSSH; password fed through SSH_ASKPASS.
# ============================================================================

Set-StrictMode -Off
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ---- Known K1 facts / config ------------------------------------------------
$K1_DEFAULT_IP    = '192.168.1.81'
$script:ReidEngine = '/home/booster/reid/osnet_x0_25_msmt17.onnx'   # OSNet ONNX on the robot (used by --appearance osnet)
$script:GestureModel = '/home/booster/yolo11n-pose.onnx'           # YOLO11n-pose ONNX (run via onnxruntime CUDA EP -- much faster than the .pt/torch path, same backend as OSNet). Local yolo11n-pose.onnx next to the app is auto-scp'd by Ensure-GestureModel (offline). For MAX speed export a TRT .engine ON the Orin later.
$script:VoiceRec = $null                                            # System.Speech recognizer handle while voice commands are ON
# --- Rerun (rerun.io) observability (Phase 3) ---
$script:RerunDir       = '/home/booster/rerun'                      # on-robot .rrd output dir (--rerun-dir)
$script:RerunWheelDir  = '/home/booster/wheels'                     # offline pip --find-links dir for the rerun-sdk closure
$script:RerunViewerExe = $null                                      # laptop viewer exe (auto-detected by Find-RerunViewer; falls back to `python -m rerun`, then the web viewer)
$script:RerunWebViewerVer = '0.23.1'                                # MUST match the robot's pinned rerun-sdk (the .rrd format is version-locked)
$script:RerunWebViewer    = ('https://app.rerun.io/version/{0}/' -f $script:RerunWebViewerVer)  # no-install 'Open .rrd' fallback: loads the .rrd LOCALLY in-browser (nothing uploaded), so SmartScreen/Defender have nothing to block
$K1_SSH_USER      = 'booster'
# SECURITY: the robot SSH password is NEVER stored in this repo. It used to be a literal here, which
# published it to git history and to the GitHub remote -- removing it from the tree does NOT unpublish
# it, so the password MUST still be rotated on the robot (docs/SECURITY_CREDENTIALS.md).
# It now comes from the K1PW environment variable, and ONLY from there.
# When K1PW is not set the app still launches (preview mode / UI is usable); SSH-dependent actions
# will fail with an authentication error at call time rather than a startup refusal. Set K1PW before
# an SSH action:  $env:K1PW = '<password>'  (this session)  or  setx K1PW "<password>"  (persist).
$K1_SSH_PASS      = $env:K1PW
$K1_LOCO_IFACE    = '127.0.0.1'
$SCRIPT_DIR       = if ($PSScriptRoot) { $PSScriptRoot }
                    elseif ($MyInvocation.MyCommand.Path) { Split-Path -Parent $MyInvocation.MyCommand.Path }
                    else { (Get-Location).Path }
# P2.1 reorg: the app lives in desktop/; its deploy SOURCES moved to robot/ (scp DEST stays flat
# /home/booster/...). ROBUSTLY locate robot/ (contains follow_person_k1.py + common.py) by walking
# up from the app dir and trying <dir>/robot and <dir> at each level -- resilient to the launch CWD,
# a copied app, or a still-flat layout, so the deploy doesn't false-fail on a path quirk.
$ROBOT_DIR = $null
$__d = $SCRIPT_DIR
for ($__i = 0; ($__i -lt 5) -and $__d -and (-not $ROBOT_DIR); $__i++) {
    foreach ($__c in @((Join-Path $__d 'robot'), $__d)) {
        if ((Test-Path (Join-Path $__c 'follow_person_k1.py')) -and (Test-Path (Join-Path $__c 'common.py'))) {
            $ROBOT_DIR = $__c; break
        }
    }
    $__d = Split-Path -Parent $__d
}
if (-not $ROBOT_DIR) { $ROBOT_DIR = Join-Path (Split-Path -Parent $SCRIPT_DIR) 'robot' }   # best-effort default
$REPO_ROOT        = Split-Path -Parent $ROBOT_DIR
$MODELS_DIR       = Join-Path $REPO_ROOT 'models'
$LAST_TARGET_FILE = Join-Path $SCRIPT_DIR 'last_target.txt'
$WORK             = Join-Path $env:TEMP 'k1finder'
New-Item -ItemType Directory -Force -Path $WORK | Out-Null

$script:RobotIP  = $K1_DEFAULT_IP
$script:SshUser  = $K1_SSH_USER
$script:SshPass  = $K1_SSH_PASS

# ---- SSH auth: KEY AUTH ONLY (2026-09-09) ------------------------------------
# Password machinery removed at operator request -- "no need for password should just be able to
# click start". SSH uses whatever the user's OpenSSH resolves: ~/.ssh/id_ed25519, id_rsa, agent
# keys, ~/.ssh/config Host aliases -- the normal Windows-OpenSSH stack. BatchMode=yes below fails
# fast on missing auth instead of hanging on a prompt inside a scp/ssh call the UI is waiting on.
#
# The $K1_SSH_PASS / $script:SshPass variables are kept (may be empty) because a few child scripts
# still take a -Pass parameter; passing an empty string means those children also skip password
# machinery and use whatever SSH resolves. No askpass helper is written and no SSH_ASKPASS is set,
# so ssh never gets asked for a password even if K1PW happens to be in the environment.
$env:DISPLAY = 'localhost:0.0'
$SSH_OPTS = @('-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=8',
              '-o','ServerAliveInterval=5','-o','BatchMode=yes')

# String form of $SSH_OPTS for the call sites that build a command line instead of an argv array.
# Kept in sync with $SSH_OPTS above.
function Get-SshOptString {
    '-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o ServerAliveInterval=5 -o BatchMode=yes'
}

# Start a process and RELIABLY return its exit code. GOTCHA: Start-Process -PassThru leaves
# $p.ExitCode = $null after WaitForExit(<ms>) UNLESS the OS handle is cached before the process
# exits -- so touch $p.Handle first. Without this, every exit-code decision silently reads $null
# and fail-closes even on success (this is exactly what broke the deploy). Returns the exit code,
# or -1 if the process hangs past $TimeoutMs (then killed).
function Invoke-Proc {
    param([string]$Exe, [string[]]$ProcArgs, [int]$TimeoutMs = 20000)
    $p = Start-Process $Exe -ArgumentList $ProcArgs -NoNewWindow -PassThru
    try { $null = $p.Handle } catch {}
    if ($p.WaitForExit($TimeoutMs)) { return [int]$p.ExitCode }
    try { $p.Kill() } catch {}
    return -1
}

# ---- Deploy robot-side helper scripts (idempotent) --------------------------
$script:Deployed = $false
$script:DeployErr = $null   # last deploy failure reason (honest message: local-missing vs push-failed)
function Deploy-RobotFiles {
    param([string]$ip)
    $files = @('stream_cam.py','common.py','run_stream.sh','run_loco.sh')  # common.py: stream_cam imports to_bgr (P3.y)
    foreach ($f in $files) {
        $src = Join-Path $ROBOT_DIR $f
        if (-not (Test-Path $src)) { return $false }
        $args = $SSH_OPTS + @($src, ("{0}@{1}:/home/booster/{2}" -f $script:SshUser, $ip, $f))
        $p = Start-Process scp.exe -ArgumentList $args -NoNewWindow -PassThru
        $null = $p.WaitForExit(20000)
    }
    $script:Deployed = $true
    return $true
}

# Deploy person-follow helpers (python + bridge source + launcher). run_follow.sh
# compiles the loco bridge from source on the robot on first DRIVE, through the shared
# bridge_build.sh (verified recipe; ROS transport preferred -- see that file's P1.9 note).
# follow_person_k1.py = lock-and-handoff: marker is a one-time lock onto the person, then YOLO-follows that person.
function Deploy-FollowFiles {
    param([string]$ip)
    $script:DeployErr = $null
    # Hard-required helpers: the follow node imports every one of these, so a local-missing OR a
    # failed push must abort the launch (fail-closed) with an HONEST reason. Invoke-Proc gives a
    # reliable exit code (Start-Process -PassThru does not -- see helper). A non-zero here means
    # the robot refused/timed-out the copy, NOT that a local file is missing -- name which case.
    # bridge_build.sh is hard-required too (P1.9): it is the ONE copy of the bridge build recipe and
    # every launcher sources it and refuses to start without it (exit 3), so a failed push must abort
    # the launch HERE rather than surface later on the robot as "COMPILE FAILED".
    foreach ($f in @('follow_person_k1.py','common.py','bridge.py','tracking.py','identity.py','rerun_sink.py','perception.py','triggers.py','calibration.py','map_change.py','loco_follow_bridge.cpp','loco_follow_bridge_ros.cpp','bridge_build.sh','run_follow.sh','run_follow_demo.sh','stage_pose.py')) {
        $src = Join-Path $ROBOT_DIR $f
        if (-not (Test-Path $src)) { $script:DeployErr = ("local helper file not found: {0}" -f $src); return $false }
        $rc = Invoke-Proc scp.exe ($SSH_OPTS + @($src, ("{0}@{1}:/home/booster/{2}" -f $script:SshUser, $ip, $f)))
        if ($rc -ne 0) { $script:DeployErr = ("scp of {0} to {1} failed (exit {2}) -- is the robot booted and reachable over SSH?" -f $f, $ip, $rc); return $false }
    }
    # Config layer (P1.1): the node loads /home/booster/config/defaults.yaml and FAIL-CLOSES
    # without it, so defaults.yaml is hard-required like the files above; the profiles are
    # best-effort. mkdir the flat config dir first (mirrors the reid/ mkdir pattern).
    $cfgDir = Join-Path $ROBOT_DIR 'config'
    $defaults = Join-Path $cfgDir 'defaults.yaml'
    if (-not (Test-Path $defaults)) { $script:DeployErr = ("local config not found: {0}" -f $defaults); return $false }
    $null = Invoke-Proc ssh.exe ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser, $ip), 'mkdir -p /home/booster/config')) 10000
    # defaults.yaml is hard-required (node fail-closes without it): a failed/timed-out push must
    # abort the launch, not leave a stale config in place and report success.
    $rc = Invoke-Proc scp.exe ($SSH_OPTS + @($defaults, ("{0}@{1}:/home/booster/config/defaults.yaml" -f $script:SshUser, $ip)))
    if ($rc -ne 0) { $script:DeployErr = ("scp of config/defaults.yaml to {0} failed (exit {1}) -- the node fail-closes without it." -f $ip, $rc); return $false }
    foreach ($prof in @('dev.yaml', 'demo.yaml', 'field.yaml', 'capture.yaml')) {
        $ps = Join-Path $cfgDir $prof
        if (Test-Path $ps) { $null = Invoke-Proc scp.exe ($SSH_OPTS + @($ps, ("{0}@{1}:/home/booster/config/{2}" -f $script:SshUser, $ip, $prof))) }
    }
    # BEST-EFFORT extras (P6.1b/P6.2): the capture launcher + post-run offload assembler. NOT follow
    # imports -- a missing local copy or failed push must NOT block a launch (unlike the hard-required
    # helpers above). run_follow_capture.sh forces --profile capture; offload_run.sh bundles a finished
    # run for Pull-Run.ps1 / auto-offload.
    foreach ($f in @('run_follow_capture.sh', 'offload_run.sh')) {
        $src = Join-Path $ROBOT_DIR $f
        if (Test-Path $src) { $null = Invoke-Proc scp.exe ($SSH_OPTS + @($src, ("{0}@{1}:/home/booster/{2}" -f $script:SshUser, $ip, $f))) }
    }
    # k1_rerun.py is BEST-EFFORT (review fix): Rerun is never a launch dependency -- the node
    # degrades to a no-op sink when the module is absent (follow_person_k1.py _NullRR), so a
    # missing local copy or failed push must not block the follow like the hard-required files do.
    $rr = Join-Path $ROBOT_DIR 'k1_rerun.py'
    if (Test-Path $rr) { $null = Invoke-Proc scp.exe ($SSH_OPTS + @($rr, ("{0}@{1}:/home/booster/k1_rerun.py" -f $script:SshUser, $ip))) }
    # cam_health.sh is BEST-EFFORT too: it's the camera-stall detect/recover aid (Ensure-Cameras /
    # the "Fix Cameras" action run it), NOT a follow import, so a missing copy must not block a launch.
    $ch = Join-Path $ROBOT_DIR 'cam_health.sh'
    if (Test-Path $ch) { $null = Invoke-Proc scp.exe ($SSH_OPTS + @($ch, ("{0}@{1}:/home/booster/cam_health.sh" -f $script:SshUser, $ip))) }
    # P6.2a: stamp the deploying repo's short SHA to /home/booster/DEPLOY_VERSION so run-offload
    # manifests (offload_run.sh) carry a real git_version instead of 'nogit' -- dataset/run provenance
    # for P7. BEST-EFFORT: git absent, not a repo, or a failed push just leaves the manifest 'nogit';
    # the deploy still succeeds. No 2>redirect on the native git call (PS 5.1 wraps native stderr).
    try {
        $sha = (git -C $ROBOT_DIR rev-parse --short HEAD | Select-Object -First 1)
        if ($sha -and ($sha -match '^[0-9a-fA-F]{4,40}$')) {
            $null = Invoke-Proc ssh.exe ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser, $ip), ("printf '%s' '{0}' > /home/booster/DEPLOY_VERSION" -f $sha)))
        }
    } catch { }
    return $true
}

# ---- Gesture pose-model staging (auto, idempotent) --------------------------
# PRESENT / MISSING / UNKNOWN for $path on the robot. Only a COMPLETED answer (ssh exit 0 carrying the
# marker) is PRESENT or MISSING. A timeout, an ssh failure (exit 255: unreachable, auth, busy link) or
# empty output is UNKNOWN -- "could not ask" is never reported as "absent". (2026-09-10: an unanswered
# probe during an Auto-Tune .rrd pull became "OSNet ReID engine isn't on the robot" while it was.)
# UNKNOWN is retried twice with a short backoff. Single-quoted remote test: no embedded double quotes,
# so it is safe through Start-Process arg quoting. A unique temp file per try, so a probe that is still
# being killed can never hold the next one's file. With $size >= 0, PRESENT also needs that byte count.
function Get-RobotFileState([string]$ip,[string]$path,[long]$size=-1){
    if($size -ge 0){ $test = 'test -f ''{0}'' && [ $(stat -c%s ''{0}'') -eq {1} ] && echo PRESENT || echo MISSING' -f $path,$size }
    else { $test = 'test -f ''{0}'' && echo PRESENT || echo MISSING' -f $path }
    for($i=0; $i -lt 3; $i++){
        if($i -gt 0){ Start-Sleep -Milliseconds (1000 * $i) }
        $tmp = Join-Path $env:TEMP ('k1_rf_{0}.txt' -f ([IO.Path]::GetRandomFileName() -replace '\.',''))
        try{
            $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), $test)) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
            try{ $null = $p.Handle }catch{}
            if(-not $p.WaitForExit(12000)){ try{ $p.Kill() }catch{}; $null = $p.WaitForExit(2000); continue }
            $out = [string](Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
            if($p.ExitCode -eq 0 -and $out -match 'PRESENT'){ return 'PRESENT' }
            if($p.ExitCode -eq 0 -and $out -match 'MISSING'){ return 'MISSING' }
        }catch{}
        finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
    }
    return 'UNKNOWN'
}
function Test-RobotFile([string]$ip,[string]$path){ return ((Get-RobotFileState $ip $path) -eq 'PRESENT') }

# Copy $local to $remote on the robot WITHOUT ever exposing a partial file: scp to "$remote.tmp", check
# its byte count, then mv it over $remote (atomic on one filesystem). A timed-out scp is killed and only
# the .tmp is lost -- the in-place scp this replaces could truncate a working model. Returns the state of
# $remote afterwards: PRESENT (right size), MISSING, or UNKNOWN (could not confirm).
function Copy-ToRobotAtomic([string]$ip,[string]$local,[string]$remote,[int]$TimeoutMs=120000){
    $len = (Get-Item $local).Length
    $tmpR = "$remote.tmp"
    $rdir = (Split-Path $remote -Parent) -replace '\\','/'
    $rc = Invoke-Proc ssh.exe ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("mkdir -p '{0}'" -f $rdir))) 10000
    if($rc -ne 0){ return 'UNKNOWN' }
    $rc = Invoke-Proc scp.exe ($SSH_OPTS + @($local, ("{0}@{1}:{2}" -f $script:SshUser,$ip,$tmpR))) $TimeoutMs
    if($rc -ne 0){
        Add-LogTrack ('scp of {0} failed (exit {1}) -- the robot copy was not touched.' -f (Split-Path $local -Leaf), $rc) $amber
        return (Get-RobotFileState $ip $remote)
    }
    if((Get-RobotFileState $ip $tmpR $len) -ne 'PRESENT'){ return (Get-RobotFileState $ip $remote) }
    $rc = Invoke-Proc ssh.exe ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("mv -f '{0}' '{1}'" -f $tmpR,$remote))) 10000
    if($rc -ne 0){ return 'UNKNOWN' }
    return (Get-RobotFileState $ip $remote $len)
}

# Make the YOLO11n-pose model exist on the robot before a gesture / A-B follow. Priority:
#   1) already present;  2) a local copy (matching basename) next to the app -> atomic copy (OFFLINE-safe);
#   3) export it on the robot via the deployed stage_pose.py (downloads the .pt ONCE -> needs internet).
# Returns PRESENT, MISSING, or UNKNOWN (the robot did not answer: nothing is staged or exported, so a
# busy link can never start a copy over a working model). On anything but PRESENT the node still
# launches and SAFELY falls back to the ArUco marker if the model really is absent. Never throws.
function Ensure-GestureModel([string]$ip){
    $remote = $script:GestureModel
    $st = Get-RobotFileState $ip $remote
    if($st -eq 'PRESENT'){ Add-LogTrack ('Gesture model present: {0}' -f $remote) $green; return 'PRESENT' }
    if($st -eq 'UNKNOWN'){ Add-LogTrack ('Could not reach the robot at {0} to check the gesture model -- not staging.' -f $ip) $amber; return 'UNKNOWN' }
    $local = Join-Path $MODELS_DIR (Split-Path $remote -Leaf)
    if(Test-Path $local){
        Add-LogTrack ('Staging gesture model ({0}) to robot...' -f (Split-Path $local -Leaf)) $accent
        $st = Copy-ToRobotAtomic $ip $local $remote
        if($st -eq 'PRESENT'){ Add-LogTrack 'Gesture model staged (scp, size verified).' $green; return 'PRESENT' }
        if($st -eq 'UNKNOWN'){ return 'UNKNOWN' }
    }
    Add-LogTrack 'No local model -> exporting on the robot (ultralytics, needs internet once)...' $amber
    $tmp = Join-Path $env:TEMP ('k1_stage_{0}.txt' -f ([IO.Path]::GetRandomFileName() -replace '\.',''))
    try{
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), 'python3 /home/booster/stage_pose.py')) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
        try{ $null = $p.Handle }catch{}
        if(-not $p.WaitForExit(240000)){ try{ $p.Kill() }catch{}; $null = $p.WaitForExit(2000); Add-LogTrack 'Robot export timed out (killed).' $amber }
        $r = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
        if($r){ Add-LogTrack ('stage_pose: {0}' -f ($r.Trim())) $accent }
    }catch{ Add-LogTrack ('Robot export failed: {0}' -f $_) $red }
    finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
    $st = Get-RobotFileState $ip $remote
    if($st -eq 'PRESENT'){ Add-LogTrack 'Gesture model exported on robot.' $green; return 'PRESENT' }
    Add-LogTrack 'Gesture model unavailable -> follow uses the ArUco marker (safe). Stage yolo11n-pose.onnx on the robot to enable gesture.' $amber
    return $st
}

# P4.2b: prefer the TRT engine for the gesture pose model WHEN it is on the robot. Its presence is
# the deliberate opt-in (built once by stage_pose_engine.py on the Orin -- engines are device-specific
# so they can't ship from here). Ultralytics runs the .engine with pre/post processing IDENTICAL to the
# .onnx; only the forward pass moves to TRT FP16 (~2x faster pose: 21->10ms measured 2026-07-08). This
# is re-resolved every launch, so deleting the engine cleanly reverts to the .onnx. Called BEFORE
# Ensure-GestureModel + Get-TrackExtraArgs so both the staging check and the launch flag see the choice.
function Resolve-GestureModel([string]$ip){
    $engine = '/home/booster/yolo11n-pose.engine'
    $est = Get-RobotFileState $ip $engine
    if($est -eq 'PRESENT'){
        $script:GestureModel = $engine
        Add-LogTrack 'Gesture model: TRT engine (yolo11n-pose.engine, FP16 ~2x faster pose).' $green
    } else {
        $script:GestureModel = '/home/booster/yolo11n-pose.onnx'
        if($est -eq 'UNKNOWN'){ Add-LogTrack 'Could not check the robot for the TRT pose engine -- using yolo11n-pose.onnx (about 2x slower pose).' $amber }
    }
}

# Make the OSNet ReID ONNX exist on the robot before an --appearance osnet follow. Priority:
#   1) already present;  2) a local copy (matching basename) next to the app -> atomic copy (OFFLINE-safe).
# NO robot-side export branch (OSNet has no ultralytics one-liner -> pre-stage the .onnx). Returns
# PRESENT, MISSING, or UNKNOWN (the robot did not answer: nothing is staged). On anything but PRESENT the
# node still launches and SAFELY falls back to a colour histogram if the engine really is absent (the
# REID badge shows HIST red and the node's arm-gate auto-refuses armed re-lock). Never throws.
function Ensure-ReidModel([string]$ip){
    $remote = $script:ReidEngine
    $st = Get-RobotFileState $ip $remote
    if($st -eq 'PRESENT'){ Add-LogTrack ('ReID engine present: {0}' -f $remote) $green; return 'PRESENT' }
    if($st -eq 'UNKNOWN'){ Add-LogTrack ('Could not reach the robot at {0} to check the ReID engine -- not staging (a busy link is not a missing file).' -f $ip) $amber; return 'UNKNOWN' }
    $local = Join-Path $MODELS_DIR (Split-Path $remote -Leaf)
    if(Test-Path $local){
        Add-LogTrack ('Staging ReID engine ({0}) to robot...' -f (Split-Path $local -Leaf)) $accent
        $st = Copy-ToRobotAtomic $ip $local $remote
        if($st -eq 'PRESENT'){ Add-LogTrack 'ReID engine staged (scp, size verified).' $green; return 'PRESENT' }
        if($st -eq 'UNKNOWN'){ return 'UNKNOWN' }
    }
    Add-LogTrack ('ReID engine unavailable ({0}) -> deep re-ID falls back to histogram; armed re-lock auto-refused. Pre-stage {1} on the robot (or next to the app) to enable OSNet.' -f $remote, (Split-Path $remote -Leaf)) $amber
    return 'MISSING'
}

# ---- Rerun (rerun.io) staging + viewer (Phase 3) ---------------------------
# Make rerun-sdk importable on the robot before a --rerun follow. OFFLINE-first, mirrors
# Ensure-ReidModel's verify-by-effect discipline (success == the import works, NOT file presence):
#   1) already importable (python3 -c import rerun) -> done;
#   2) a local wheels\ dir next to the app -> scp the aarch64/cp310 closure to /home/booster/wheels
#      + pip install --no-index --find-links, then RE-VERIFY the import.
# Never throws. On $false the node still launches with Rerun DISABLED (no .rrd, follow byte-identical).
# NOTE: installs into whatever `python3` resolves to in the ssh shell -- the SAME interpreter
# run_follow.sh launches the node with, so an import here means an import at node start.
function Test-RerunImport([string]$ip){
    # RETRY x3 (fix 2026-07-05): a SINGLE flaky-ssh moment used to false-negative and pop the scary
    # "rerun not on the robot" dialog even though rerun IS installed (verified 0.23.1 + working sink).
    # rerun being absent is stable, so one clean import in a few tries is the truth; only declare it
    # missing after ALL attempts fail. Unique temp per try + kill-on-timeout.
    for($try=1; $try -le 3; $try++){
        $tmp = Join-Path $env:TEMP ('k1_rerun_imp_{0}.txt' -f ([IO.Path]::GetRandomFileName() -replace '\.',''))
        try{
            $q = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), "python3 -c 'import rerun,sys; sys.stdout.write(rerun.__version__)'")) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
            if(-not $q.WaitForExit(15000)){ try{ $q.Kill() }catch{}; $null=$q.WaitForExit(2000) }
            $v = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
            if($v -and ($v.Trim() -match '^\d+\.\d+')){ return $v.Trim() }
        }catch{}
        finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
        if($try -lt 3){ Start-Sleep -Milliseconds 800 }
    }
    return $null
}
function Ensure-RerunWheels([string]$ip){
    $v = Test-RerunImport $ip
    if($v){ Add-LogTrack ('rerun-sdk present on robot (v{0}).' -f $v) $green; return $true }
    $wdir = Join-Path $MODELS_DIR 'wheels'
    $whls = @(); if(Test-Path $wdir){ $whls = @(Get-ChildItem -Path $wdir -Filter '*.whl' -ErrorAction SilentlyContinue) }
    if($whls.Count -eq 0){
        Add-LogTrack ('rerun-sdk not importable + no local wheels\ dir ({0}) -> Rerun disabled (follow proceeds). Stage the aarch64 cp310 rerun-sdk closure into wheels\ to enable.' -f $wdir) $amber
        return $false
    }
    Add-LogTrack ('Staging {0} Rerun wheel(s) to robot...' -f $whls.Count) $accent
    $rwdir = $script:RerunWheelDir
    try{
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("mkdir -p '{0}'" -f $rwdir))) -NoNewWindow -PassThru; $null=$p.WaitForExit(10000)
        foreach($w in $whls){
            $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($w.FullName, ("{0}@{1}:{2}/{3}" -f $script:SshUser,$ip,$rwdir,$w.Name))) -NoNewWindow -PassThru
            if(-not $p.WaitForExit(120000)){ try{$p.Kill()}catch{}; Add-LogTrack ('scp of {0} timed out -> Rerun staging FAILED.' -f $w.Name) $red; return $false }
        }
    }catch{ Add-LogTrack ('wheel scp failed: {0}' -f $_) $red; return $false }
    Add-LogTrack 'Installing rerun-sdk (pip --user --no-index)...' $accent
    try{
        # PIN 0.23.1: the newest rerun that allows numpy 1.x. rerun >=0.23.2 requires numpy>=2, which
        # would break the robot's numpy-1.26-ABI follow stack (onnxruntime/cv2/rclpy). --user installs
        # to ~/.local (system dist-packages is not writable and MUST NOT be touched).
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("python3 -m pip install --user --no-index --find-links '{0}' 'rerun-sdk==0.23.1'" -f $rwdir))) -NoNewWindow -PassThru
        $null = $p.WaitForExit(180000)
    }catch{ Add-LogTrack ('pip install failed: {0}' -f $_) $red; return $false }
    $v = Test-RerunImport $ip
    if($v){ Add-LogTrack ('rerun-sdk installed (v{0}).' -f $v) $green; return $true }
    Add-LogTrack 'rerun-sdk still not importable after install -> Rerun disabled (follow proceeds).' $amber
    return $false
}
# Locate the laptop-side Rerun viewer exe (pinned for 'Open .rrd'); fall back to `python -m rerun`.
function Find-RerunViewer {
    if($script:RerunViewerExe -and (Test-Path $script:RerunViewerExe)){ return $script:RerunViewerExe }
    $cands = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\rerun\rerun.exe'),
        (Join-Path $env:USERPROFILE 'anaconda3\Scripts\rerun.exe'),
        (Join-Path $env:USERPROFILE 'AppData\Roaming\Python\Python313\Scripts\rerun.exe')
    )
    foreach($c in $cands){ if(Test-Path $c){ $script:RerunViewerExe=$c; return $c } }
    try{ $g=(Get-Command rerun.exe -ErrorAction SilentlyContinue).Source; if($g){ $script:RerunViewerExe=$g; return $g } }catch{}
    return $null
}
# Pull the NEWEST /home/booster/rerun/*.rrd to the local work dir; return the local path (or $null).
function Pull-RerunRecording([string]$ip){
    if(-not $ip){ return $null }
    $tmp = Join-Path $env:TEMP 'k1_rrd_name.txt'
    try{
        Remove-Item $tmp -ErrorAction SilentlyContinue
        # Newest .rrd across the loose rerun/ dir AND the offloaded bundles (runs/<id>/): the auto-offload
        # MOVES a finished run's .rrd out of rerun/ into its bundle, so looking only in rerun/ finds an
        # OLD leftover. Include runs/*/*.rrd so 'Open .rrd' shows the RUN YOU JUST DID, not a stale one.
        $q = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), 'ls -1t /home/booster/rerun/*.rrd /home/booster/runs/*/*.rrd 2>/dev/null | head -1')) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
        $null = $q.WaitForExit(10000)
    }catch{ Add-LogTrack ('rrd list failed: {0}' -f $_) $red; return $null }
    $remote = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue); if($remote){ $remote=$remote.Trim() }
    if(-not $remote){ Add-LogTrack 'No .rrd on the robot (/home/booster/rerun). Run a follow with Rerun ticked first.' $amber; return $null }
    $leaf = Split-Path $remote -Leaf; $dst = Join-Path $WORK $leaf
    Add-LogTrack ('Pulling {0} ...' -f $leaf) $accent
    try{
        $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}:{2}" -f $script:SshUser,$ip,$remote), $dst)) -NoNewWindow -PassThru
        if(-not $p.WaitForExit(120000)){ try{$p.Kill()}catch{}; Add-LogTrack 'scp of .rrd timed out.' $red; return $null }
    }catch{ Add-LogTrack ('scp failed: {0}' -f $_) $red; return $null }
    if((Test-Path $dst) -and (Get-Item $dst).Length -gt 0){ Add-LogTrack ('Pulled {0} ({1:N0} bytes).' -f $leaf,(Get-Item $dst).Length) $green; return $dst }
    Add-LogTrack '.rrd did not transfer.' $red; return $null
}
function Open-RerunRecording([string]$path){
    if(-not ($path -and (Test-Path $path))){ Add-LogTrack ('No .rrd to open.') $amber; return }
    $exe = Find-RerunViewer
    if($exe){ try{ Start-Process $exe -ArgumentList ('"{0}"' -f $path); Add-LogTrack ('Opened in Rerun viewer: {0}' -f (Split-Path $path -Leaf)) $accent }catch{ Add-LogTrack ('viewer launch failed: {0}' -f $_) $red } ; return }
    # No native viewer. Try `python -m rerun` ONLY if a REAL python (not the WindowsApps Store stub,
    # which would pop the Store) has rerun-sdk importable. Pre-verify the import -- Start-Process
    # 'python' succeeds even when rerun-sdk is absent (console flashes 'No module named rerun').
    $py=$null
    try{ $g=(Get-Command python.exe -ErrorAction SilentlyContinue); if($g -and ($g.Source -notmatch 'WindowsApps')){ $py=$g.Source } }catch{}
    if($py){
        $imp=$false
        try{ $q=Start-Process $py -ArgumentList '-c "import rerun"' -WindowStyle Hidden -PassThru; try{$null=$q.Handle}catch{}; if($q.WaitForExit(15000) -and $q.ExitCode -eq 0){ $imp=$true } }catch{}
        if($imp){
            try{ Start-Process $py -ArgumentList ('-m rerun "{0}"' -f $path); Add-LogTrack 'Opened via python -m rerun.' $accent; return }
            catch{ Add-LogTrack ('viewer launch failed: {0}' -f $_) $red }
        }
    }
    # FINAL fallback (no native viewer, no python+rerun -- this laptop): the version-matched WEB viewer.
    # No install => nothing for SmartScreen/Defender to block; the .rrd loads LOCALLY in the browser.
    # The web viewer can't take a local file path via ?url= (that needs an http-served file), so we open
    # the viewer AND reveal the pulled .rrd in Explorer for a one-drag load.
    try{
        Start-Process $script:RerunWebViewer
        try{ Start-Process explorer.exe -ArgumentList ('/select,"{0}"' -f $path) }catch{}
        Add-LogTrack ('No local viewer -> opened the Rerun {0} WEB viewer. DRAG the highlighted {1} from Explorer onto the browser tab (loads locally; no install/upload).' -f $script:RerunWebViewerVer,(Split-Path $path -Leaf)) $accent
    }catch{ Add-LogTrack ('web viewer launch failed: {0} -- open {1} in a browser and drag in {2}.' -f $_,$script:RerunWebViewer,$path) $red }
}
# ---- Reachability helper ----------------------------------------------------
function Test-K1Reachable {
    param([string]$ip)
    $result = [ordered]@{ Ping = $false; SSH = $false; Banner = ''; Hostname = '' }
    try { $result.Ping = (Test-Connection -ComputerName $ip -Count 1 -Quiet -ErrorAction SilentlyContinue) } catch { }
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $ar = $c.BeginConnect($ip, 22, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(1200)) {
            $c.EndConnect($ar)
            if ($c.Connected) {
                $result.SSH = $true
                try {
                    $stream = $c.GetStream(); $stream.ReadTimeout = 800
                    Start-Sleep -Milliseconds 150
                    $buf = New-Object byte[] 256
                    $n = $stream.Read($buf, 0, 256)
                    if ($n -gt 0) { $result.Banner = ([System.Text.Encoding]::ASCII.GetString($buf,0,$n)).Trim() }
                } catch { }
            }
        }
        $c.Close()
    } catch { }
    try { $result.Hostname = [System.Net.Dns]::GetHostEntry($ip).HostName } catch { }
    $result
}

# ---- Discover scan state + worker ------------------------------------------
$sync = [hashtable]::Synchronized(@{
    Running=$false; Cancel=$false; Progress=0; Total=0
    Results=(New-Object System.Collections.ArrayList); Log=(New-Object System.Collections.Queue)
    Subnets=''; Done=$false; PS=$null; Handle=$null
})
$scanScript = {
    function Get-LocalSubnets {
        $list = @()
        try {
            $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
                $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' }
            foreach ($i in $ips) {
                $oct = $i.IPAddress.Split('.')
                if ($oct.Count -eq 4) {
                    $list += [pscustomobject]@{ Base="$($oct[0]).$($oct[1]).$($oct[2])."; Self=$i.IPAddress; Iface=$i.InterfaceAlias }
                }
            }
        } catch { }
        $list
    }
    function Score-Candidate($ip,$banner,$hostname) {
        $score=0;$reasons=@()
        if ($ip -eq '192.168.10.102'){$score+=50;$reasons+='Default K1 wired IP (.102)'}
        if ($ip -like '192.168.10.*'){$score+=15;$reasons+='On K1 default subnet 192.168.10.x'}
        if ($banner -match 'SSH'){$score+=10;$reasons+='SSH (port 22) open'}
        if ($banner -match 'Ubuntu|Debian'){$score+=15;$reasons+='Ubuntu/Debian host (K1 runs Ubuntu)'}
        if ($hostname -match 'booster|k1|robot'){$score+=45;$reasons+='Hostname matches Booster/K1/robot'}
        $label='Low'; if($score -ge 50){$label='High'}elseif($score -ge 25){$label='Medium'}
        [pscustomobject]@{Score=$score;Label=$label;Why=($reasons -join '; ')}
    }
    try {
        $sync.Log.Enqueue('Detecting local network interfaces...')
        $subnets = Get-LocalSubnets
        $bases=@{}; $sumTxt=@()
        foreach($s in $subnets){ $bases[$s.Base]=$true; $sumTxt += ("{0}0/24 (this PC: {1}, {2})" -f $s.Base,$s.Self,$s.Iface) }
        $sync.Subnets = ($sumTxt -join '   |   ')
        if ($bases.Count -eq 0){ $sync.Log.Enqueue('No usable IPv4 subnet found.'); $sync.Done=$true; return }
        $sync.Log.Enqueue(("Found {0} subnet(s)." -f $bases.Count))
        $targets = New-Object System.Collections.Generic.List[string]
        foreach($b in $bases.Keys){ for($h=1;$h -le 254;$h++){ $targets.Add($b+$h) } }
        $sync.Total=$targets.Count
        $sync.Log.Enqueue(("Scanning {0} addresses for SSH/port 22 ..." -f $targets.Count))
        $batchSize=80; $timeoutMs=500
        for($start=0;$start -lt $targets.Count;$start+=$batchSize){
            if($sync.Cancel){ $sync.Log.Enqueue('Scan cancelled.'); break }
            $end=[Math]::Min($start+$batchSize,$targets.Count)-1; $batch=$targets[$start..$end]; $pending=@{}
            foreach($ip in $batch){ try{ $c=New-Object System.Net.Sockets.TcpClient; $ar=$c.BeginConnect($ip,22,$null,$null); $pending[$ip]=@{Client=$c;Async=$ar} }catch{} }
            Start-Sleep -Milliseconds $timeoutMs
            foreach($ip in $pending.Keys){
                $c=$pending[$ip].Client; $ar=$pending[$ip].Async; $open=$false
                try{ if($ar.AsyncWaitHandle.WaitOne(0)){ $c.EndConnect($ar); if($c.Connected){$open=$true} } }catch{}
                if($open){
                    $banner=''
                    try{ $st=$c.GetStream(); $st.ReadTimeout=600; Start-Sleep -Milliseconds 120; $buf=New-Object byte[] 256; $n=$st.Read($buf,0,256); if($n -gt 0){$banner=([System.Text.Encoding]::ASCII.GetString($buf,0,$n)).Trim()} }catch{}
                    $hn=''; try{$hn=[System.Net.Dns]::GetHostEntry($ip).HostName}catch{}
                    $sc=Score-Candidate $ip $banner $hn
                    [void]$sync.Results.Add([pscustomobject]@{Score=$sc.Score;Label=$sc.Label;IP=$ip;Hostname=$hn;Banner=$banner;Why=$sc.Why})
                    $sync.Log.Enqueue(("  [{0}] {1}  {2}  {3}" -f $sc.Label,$ip,$hn,$banner))
                }
                try{$c.Close()}catch{}
            }
            $sync.Progress=$end+1
        }
        if(-not $sync.Cancel){ $sync.Log.Enqueue(("Scan complete. {0} host(s) found." -f $sync.Results.Count)) }
    } catch { $sync.Log.Enqueue('Scan error: '+$_.Exception.Message) }
    finally { $sync.Done=$true }
}

# ---- Live + Control shared state -------------------------------------------
$liveSync = [hashtable]::Synchronized(@{ Jpeg=$null; Seq=0; Frames=0; Stop=$false; Done=$false; Err=''; Lock=0 })
# ---- Tracker (marker-seeded markerless PERSON-follow): annotated stream + status byte ----
$trackSync = [hashtable]::Synchronized(@{ Jpeg=$null; Seq=0; Frames=0; Stop=$false; Done=$false; Err=''; Lock=0 })
$script:TrackProc=$null; $script:TrackPS=$null; $script:TrackRS=$null; $script:TrackOn=$false; $script:TrackDrive=$false; $script:TrackErrSub=$null
$script:HbProc=$null   # Deadman-HB relay ssh process (P2 #12); alive only while the follow runs with 'Deadman HB' checked
$script:trackMs=$null; $script:trackLastSeq=-1; $script:trackFpsFrames=0; $script:trackLastLock=-1
$script:TrackStart=[datetime]::MinValue
$script:TrackRerunOn=$false      # P6.2b: did the current/last Tracker session record a .rrd? -> post-run offload
$script:K1SessionCapSec=300      # FIELD NIGHT 2026-09-10 (plan C25, operator-approved): every app-launched follow ends at 300 s. run_follow.sh reads it as K1_MAX_SEC (default 2000). Revert to 2000 after the field block.
# UI-side hard session watchdog BACKSTOP: must stay ABOVE K1SessionCapSec so the node can settle
# first. Council 2026-09-11 #8: dialogs used to advertise TrackMaxSec/FollowMaxSec (2060/1260) while
# the real cap was 300 — that lied to the operator. Watchdogs stay a short margin above the cap.
$script:TrackMaxSec=($script:K1SessionCapSec + 30)
$script:FollowMaxSec=($script:K1SessionCapSec + 30)
$script:TrackToggleGuard=$false  # prevents the toggle's CheckedChanged from re-entering during programmatic resets
$ctrlSync = [hashtable]::Synchronized(@{ Log=(New-Object System.Collections.Queue); Stop=$false })
$script:LiveProc=$null; $script:LivePS=$null; $script:LiveRS=$null; $script:LiveOn=$false
$script:liveMs=$null; $script:lastSeq=-1; $script:liveFpsFrames=0; $script:liveFpsTick=0; $script:lastLock=-1
$script:CtrlProc=$null; $script:CtrlPS=$null; $script:CtrlRS=$null; $script:CtrlOn=$false
$script:CtrlPrime=0   # countdown (seconds) to auto-send a priming 'gft' after connect
# command codes the robot's PTY echoes back on stdout; we filter these from the log so only responses show
$script:CtrlCmdSet=@{}; foreach($c in 'mp','md','mw','mc','w','a','s','d','q','e','l','hu','hd','hl','hr','ho','wh','ch','gu','ld','gft','rock','paper','scissor','ok','grasp'){ $script:CtrlCmdSet[$c]=$true }
$script:MotionButtons=@()
# ---- Follow-marker (QR) state: background reader writes $followSync.Log; UI drains in $mediaTimer ----
$followSync = [hashtable]::Synchronized(@{ Log=(New-Object System.Collections.Queue); Stop=$false })
$script:FollowProc=$null; $script:FollowPS=$null; $script:FollowRS=$null; $script:FollowOn=$false; $script:FollowDrive=$false
$script:FollowStart=[datetime]::MinValue
$script:FollowToggleGuard=$false  # prevents the toggle's CheckedChanged from re-entering during programmatic resets

# ============================================================================
#  Visual system — Tesla × SpaceX × Apple (near-black / cyan accent / mission clarity)
#  Brand-first header. Tracker hierarchy: Primary > Avoidance > Advanced > Cmd.
# ============================================================================
function New-K1Font([string]$Family, [float]$Size, [System.Drawing.FontStyle]$Style = 'Regular') {
    foreach ($name in @($Family, 'Bahnschrift', 'Segoe UI Variable Display', 'Segoe UI Variable Text', 'Segoe UI')) {
        try { return New-Object System.Drawing.Font($name, $Size, $Style) } catch {}
    }
    return New-Object System.Drawing.Font('Microsoft Sans Serif', $Size, $Style)
}

$font      = New-K1Font 'Bahnschrift' 9.5
$fontBold  = New-K1Font 'Bahnschrift' 10.5 ([System.Drawing.FontStyle]::Bold)
$fontBrand = New-K1Font 'Bahnschrift' 22 ([System.Drawing.FontStyle]::Bold)
$fontSub   = New-K1Font 'Bahnschrift' 9
$fontHero  = New-K1Font 'Bahnschrift' 14 ([System.Drawing.FontStyle]::Bold)
$fontStatus= New-K1Font 'Bahnschrift' 16 ([System.Drawing.FontStyle]::Bold)
$mono      = New-K1Font 'Cascadia Mono' 9.25
if (-not $mono) { $mono = New-Object System.Drawing.Font('Consolas', 9) }

# Palette — pure black chassis, one electric cyan accent, Tesla red danger
$bg        = [System.Drawing.Color]::FromArgb(0, 0, 0)          # #000000
$surface   = [System.Drawing.Color]::FromArgb(10, 10, 10)      # #0A0A0A
$panelBg   = [System.Drawing.Color]::FromArgb(20, 20, 20)      # #141414
$surface2  = [System.Drawing.Color]::FromArgb(28, 28, 30)      # #1C1C1E raised
$stroke    = [System.Drawing.Color]::FromArgb(44, 44, 46)      # hairline
$text      = [System.Drawing.Color]::FromArgb(245, 245, 247)   # #F5F5F7
$muted     = [System.Drawing.Color]::FromArgb(142, 142, 147)   # #8E8E93
$accent    = [System.Drawing.Color]::FromArgb(50, 212, 255)    # #32D4FF — ONE accent
$green     = [System.Drawing.Color]::FromArgb(180, 230, 200)   # soft white-green (sparingly)
$red       = [System.Drawing.Color]::FromArgb(227, 25, 55)     # #E31937 Tesla red
$amber     = [System.Drawing.Color]::FromArgb(200, 170, 90)    # restrained caution
$dark      = $bg                                                 # logs / video wells (compat alias)
$chipBg    = [System.Drawing.Color]::FromArgb(28, 28, 30)

function Set-K1PrimaryButton([System.Windows.Forms.Button]$b) {
    $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderSize = 0
    $b.BackColor = $accent; $b.ForeColor = [System.Drawing.Color]::Black; $b.Font = $fontBold
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
}
function Set-K1DangerButton([System.Windows.Forms.Button]$b) {
    $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderSize = 0
    $b.BackColor = $red; $b.ForeColor = [System.Drawing.Color]::White; $b.Font = $fontBold
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
}
function Set-K1GhostButton([System.Windows.Forms.Button]$b) {
    $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderColor = $stroke; $b.FlatAppearance.BorderSize = 1
    $b.BackColor = $surface2; $b.ForeColor = $text; $b.Font = $font
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
}
function Set-K1OkButton([System.Windows.Forms.Button]$b) {
    $b.FlatStyle = 'Flat'; $b.FlatAppearance.BorderSize = 0
    $b.BackColor = $green; $b.ForeColor = [System.Drawing.Color]::Black; $b.Font = $fontBold
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
}
function Set-K1Field([System.Windows.Forms.TextBox]$tb) {
    $tb.BackColor = $surface2; $tb.ForeColor = $text; $tb.BorderStyle = 'FixedSingle'
}
function Set-K1Group([System.Windows.Forms.GroupBox]$g) {
    $g.ForeColor = $muted; $g.BackColor = $panelBg; $g.Font = $fontBold
}
function Set-K1Panel([System.Windows.Forms.Panel]$p) {
    $p.BackColor = $panelBg
}
function Set-K1Check([System.Windows.Forms.CheckBox]$c, [string]$Tone = 'normal') {
    $c.BackColor = $panelBg; $c.FlatStyle = 'Flat'
    switch ($Tone) {
        'danger'  { $c.ForeColor = $red; $c.Font = $fontBold }
        'accent'  { $c.ForeColor = $accent; $c.Font = $fontBold }
        'caution' { $c.ForeColor = $amber; $c.Font = $fontBold }
        default   { $c.ForeColor = $text; $c.Font = $font }
    }
}
function Set-K1Combo([System.Windows.Forms.ComboBox]$cb) {
    $cb.FlatStyle = 'Flat'; $cb.BackColor = $surface2; $cb.ForeColor = $text
}
function Set-K1Label([System.Windows.Forms.Label]$l, [string]$Tone = 'normal') {
    $l.BackColor = $panelBg
    switch ($Tone) {
        'muted'  { $l.ForeColor = $muted; $l.Font = $font }
        'accent' { $l.ForeColor = $accent; $l.Font = $fontBold }
        'section'{ $l.ForeColor = $muted; $l.Font = $fontBold }
        default  { $l.ForeColor = $text; $l.Font = $font }
    }
}
function Set-K1List([System.Windows.Forms.ListView]$lv) {
    $lv.BackColor = $surface; $lv.ForeColor = $text; $lv.BorderStyle = 'None'
}
function Set-K1Log([System.Windows.Forms.RichTextBox]$rtb) {
    $rtb.BackColor = $bg; $rtb.ForeColor = $accent; $rtb.BorderStyle = 'None'
}

# ============================================================================
#  Form + header + tabs + status
# ============================================================================
$form=New-Object System.Windows.Forms.Form
$form.Text='K1 Finder'
$form.Size=New-Object System.Drawing.Size(1180,860)
$form.MinimumSize=New-Object System.Drawing.Size(1000,740)
$form.StartPosition='CenterScreen'; $form.Font=$font; $form.BackColor=$bg
$form.ForeColor=$text

$header=New-Object System.Windows.Forms.Panel
$header.Dock='Top'; $header.Height=72; $header.BackColor=$bg
$header.Padding='0,0,0,0'
# Subtle top accent line (brand presence without chrome clutter)
$headerAccent=New-Object System.Windows.Forms.Panel
$headerAccent.Dock='Top'; $headerAccent.Height=3; $headerAccent.BackColor=$accent
$header.Controls.Add($headerAccent)
$title=New-Object System.Windows.Forms.Label
$title.Text='K1 Finder'
$title.ForeColor=$text
$title.Font=$fontBrand
$title.AutoSize=$true; $title.Location=New-Object System.Drawing.Point(20,14)
$header.Controls.Add($title)
$subtitle=New-Object System.Windows.Forms.Label
$subtitle.Text='mission control  ·  discover  ·  drive  ·  observe'
$subtitle.ForeColor=$muted
$subtitle.Font=$fontSub
$subtitle.AutoSize=$true; $subtitle.Location=New-Object System.Drawing.Point(22,46)
$header.Controls.Add($subtitle)

$status=New-Object System.Windows.Forms.StatusStrip
$status.BackColor=$surface; $status.ForeColor=$muted
$statusLbl=New-Object System.Windows.Forms.ToolStripStatusLabel
$statusLbl.Text='Ready.'
$statusLbl.ForeColor=$muted
[void]$status.Items.Add($statusLbl)

$tabs=New-Object System.Windows.Forms.TabControl
$tabs.Dock='Fill'; $tabs.Font=$fontBold
$tabs.SizeMode='Fixed'; $tabs.ItemSize=New-Object System.Drawing.Size(124,32)
$tabs.Padding=New-Object System.Drawing.Point(12,6)
$tabs.DrawMode='OwnerDrawFixed'
$tabs.Add_DrawItem({
    param($sender, $e)
    $g = $e.Graphics
    $r = $e.Bounds
    $selected = ($e.Index -eq $sender.SelectedIndex)
    $fill = if ($selected) { $surface2 } else { $bg }
    $g.FillRectangle((New-Object System.Drawing.SolidBrush $fill), $r)
    if ($selected) {
        $g.FillRectangle((New-Object System.Drawing.SolidBrush $accent),
            (New-Object System.Drawing.Rectangle $r.X, ($r.Bottom - 3), $r.Width, 3))
    }
    $txt = $sender.TabPages[$e.Index].Text.Trim()
    $brush = New-Object System.Drawing.SolidBrush $(if ($selected) { $text } else { $muted })
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
    $g.DrawString($txt, $fontBold, $brush, $r, $sf)
    $brush.Dispose(); $sf.Dispose()
})
$tabDiscover=New-Object System.Windows.Forms.TabPage; $tabDiscover.Text='Discover'; $tabDiscover.BackColor=$bg; $tabDiscover.ForeColor=$text
$tabSsh=New-Object System.Windows.Forms.TabPage; $tabSsh.Text='SSH'; $tabSsh.BackColor=$bg; $tabSsh.ForeColor=$text
$tabLive=New-Object System.Windows.Forms.TabPage; $tabLive.Text='Live'; $tabLive.BackColor=$bg; $tabLive.ForeColor=$text
$tabCtrl=New-Object System.Windows.Forms.TabPage; $tabCtrl.Text='Control'; $tabCtrl.BackColor=$bg; $tabCtrl.ForeColor=$text
$tabFiles=New-Object System.Windows.Forms.TabPage; $tabFiles.Text='Files'; $tabFiles.BackColor=$bg; $tabFiles.ForeColor=$text
$tabTrack=New-Object System.Windows.Forms.TabPage; $tabTrack.Text='Tracker'; $tabTrack.BackColor=$bg; $tabTrack.ForeColor=$text
$tabMap=New-Object System.Windows.Forms.TabPage; $tabMap.Text='Local Map'; $tabMap.BackColor=$bg; $tabMap.ForeColor=$text
[void]$tabs.TabPages.AddRange(@($tabDiscover,$tabSsh,$tabLive,$tabCtrl,$tabFiles,$tabTrack,$tabMap))

$form.Controls.Add($header); $form.Controls.Add($status); $form.Controls.Add($tabs); $tabs.BringToFront()

# ============================================================================
#  TAB 1 - DISCOVER
# ============================================================================
$ctrlPanel=New-Object System.Windows.Forms.Panel; $ctrlPanel.Dock='Top'; $ctrlPanel.Height=108; $ctrlPanel.Padding='16,12,16,12'; $ctrlPanel.BackColor=$surface
$subnetLbl=New-Object System.Windows.Forms.Label; $subnetLbl.Text='Subnets: (detected at scan time)'; $subnetLbl.AutoSize=$true; $subnetLbl.ForeColor=$muted; $subnetLbl.Location=New-Object System.Drawing.Point(16,10); $ctrlPanel.Controls.Add($subnetLbl)
$scanBtn=New-Object System.Windows.Forms.Button; $scanBtn.Text='Scan for K1'; $scanBtn.Size='132,36'; $scanBtn.Location='16,40'; Set-K1PrimaryButton $scanBtn; $ctrlPanel.Controls.Add($scanBtn)
$stopBtn=New-Object System.Windows.Forms.Button; $stopBtn.Text='Stop'; $stopBtn.Size='78,36'; $stopBtn.Location='158,40'; Set-K1GhostButton $stopBtn; $stopBtn.Enabled=$false; $ctrlPanel.Controls.Add($stopBtn)
$manualLbl=New-Object System.Windows.Forms.Label; $manualLbl.Text='Or enter K1 IP'; $manualLbl.AutoSize=$true; $manualLbl.ForeColor=$muted; $manualLbl.Location='268,20'; $ctrlPanel.Controls.Add($manualLbl)
$ipBox=New-Object System.Windows.Forms.TextBox; $ipBox.Size='148,28'; $ipBox.Location='268,44'; $ipBox.Font=$mono; $ipBox.Text=$K1_DEFAULT_IP; Set-K1Field $ipBox; $ctrlPanel.Controls.Add($ipBox)
$verifyBtn=New-Object System.Windows.Forms.Button; $verifyBtn.Text='Verify'; $verifyBtn.Size='88,36'; $verifyBtn.Location='428,40'; Set-K1GhostButton $verifyBtn; $ctrlPanel.Controls.Add($verifyBtn)
$progress=New-Object System.Windows.Forms.ProgressBar; $progress.Size='220,10'; $progress.Location='536,54'; $progress.Style='Continuous'; $progress.ForeColor=$accent; $ctrlPanel.Controls.Add($progress)

$list=New-Object System.Windows.Forms.ListView; $list.View='Details'; $list.FullRowSelect=$true; $list.GridLines=$false; $list.MultiSelect=$false; $list.HideSelection=$false; $list.Dock='Fill'; $list.Font=$font
$list.BackColor=$surface; $list.ForeColor=$text; $list.BorderStyle='None'
[void]$list.Columns.Add('Confidence',100);[void]$list.Columns.Add('IP Address',140);[void]$list.Columns.Add('Hostname',160);[void]$list.Columns.Add('SSH Banner',220);[void]$list.Columns.Add('Why',280)
$listPanel=New-Object System.Windows.Forms.Panel; $listPanel.Dock='Fill'; $listPanel.Padding='16,12,16,8'; $listPanel.BackColor=$bg; $listPanel.Controls.Add($list)

$bottom=New-Object System.Windows.Forms.Panel; $bottom.Dock='Bottom'; $bottom.Height=200; $bottom.Padding='16,8,16,12'; $bottom.BackColor=$bg
$connectBtn=New-Object System.Windows.Forms.Button; $connectBtn.Text='Verify Selected'; $connectBtn.Size='140,34'; $connectBtn.Location='16,4'; Set-K1OkButton $connectBtn; $bottom.Controls.Add($connectBtn)
$useBtn=New-Object System.Windows.Forms.Button; $useBtn.Text='Use this IP everywhere'; $useBtn.Size='180,34'; $useBtn.Location='168,4'; Set-K1GhostButton $useBtn; $bottom.Controls.Add($useBtn)
$logBox=New-Object System.Windows.Forms.RichTextBox; $logBox.ReadOnly=$true; $logBox.Dock='Bottom'; $logBox.Height=148; $logBox.BackColor=$surface; $logBox.ForeColor=$accent; $logBox.Font=$mono; $logBox.BorderStyle='None'; $bottom.Controls.Add($logBox)
$tabDiscover.Controls.Add($ctrlPanel); $tabDiscover.Controls.Add($bottom); $tabDiscover.Controls.Add($listPanel); $listPanel.BringToFront()

# ============================================================================
#  TAB 2 - SSH & FILES
# ============================================================================
$sshLayout=New-Object System.Windows.Forms.TableLayoutPanel; $sshLayout.Dock='Fill'; $sshLayout.ColumnCount=1; $sshLayout.RowCount=4; $sshLayout.Padding='12,8,12,8'
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,74)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,92)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,188)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
$grpRobot=New-Object System.Windows.Forms.GroupBox; $grpRobot.Text='Robot'; $grpRobot.Dock='Fill'; Set-K1Group $grpRobot
$lblIp2=New-Object System.Windows.Forms.Label; $lblIp2.Text='Robot IP:'; $lblIp2.AutoSize=$true; $lblIp2.Location='12,28'; Set-K1Label $lblIp2 'muted'; $grpRobot.Controls.Add($lblIp2)
$ip2Box=New-Object System.Windows.Forms.TextBox; $ip2Box.Size='150,26'; $ip2Box.Location='78,25'; $ip2Box.Font=$mono; $ip2Box.Text=$K1_DEFAULT_IP; Set-K1Field $ip2Box; $grpRobot.Controls.Add($ip2Box)
$pullBtn=New-Object System.Windows.Forms.Button; $pullBtn.Text='Pull from Discover'; $pullBtn.Size='140,26'; $pullBtn.Location='240,25'; Set-K1GhostButton $pullBtn; $grpRobot.Controls.Add($pullBtn)
$testBtn=New-Object System.Windows.Forms.Button; $testBtn.Text='Test SSH'; $testBtn.Size='100,26'; $testBtn.Location='392,25'; Set-K1GhostButton $testBtn; $grpRobot.Controls.Add($testBtn)
$userLbl=New-Object System.Windows.Forms.Label; $userLbl.Text=("login: {0} (SSH key auth)" -f $K1_SSH_USER); $userLbl.AutoSize=$true; $userLbl.Location='512,30'; Set-K1Label $userLbl 'muted'; $grpRobot.Controls.Add($userLbl)
$grpSsh=New-Object System.Windows.Forms.GroupBox; $grpSsh.Text='SSH'; $grpSsh.Dock='Fill'; Set-K1Group $grpSsh
$sshDesc=New-Object System.Windows.Forms.Label; $sshDesc.Text='Open a shell on the robot, or install an SSH key so uploads need no password.'; $sshDesc.AutoSize=$true; $sshDesc.Location='12,22'; Set-K1Label $sshDesc 'muted'; $grpSsh.Controls.Add($sshDesc)
$sshTermBtn=New-Object System.Windows.Forms.Button; $sshTermBtn.Text='Open SSH Terminal'; $sshTermBtn.Size='160,30'; $sshTermBtn.Location='12,46'; Set-K1PrimaryButton $sshTermBtn; $grpSsh.Controls.Add($sshTermBtn)
$keyBtn=New-Object System.Windows.Forms.Button; $keyBtn.Text='Enable passwordless login (install SSH key)'; $keyBtn.Size='300,30'; $keyBtn.Location='184,46'; Set-K1GhostButton $keyBtn; $grpSsh.Controls.Add($keyBtn)
$grpUp=New-Object System.Windows.Forms.GroupBox; $grpUp.Text='Upload files / folders to the K1'; $grpUp.Dock='Fill'; Set-K1Group $grpUp
$addFilesBtn=New-Object System.Windows.Forms.Button; $addFilesBtn.Text='Add files...'; $addFilesBtn.Size='100,28'; $addFilesBtn.Location='12,24'; Set-K1GhostButton $addFilesBtn; $grpUp.Controls.Add($addFilesBtn)
$addFolderBtn=New-Object System.Windows.Forms.Button; $addFolderBtn.Text='Add folder...'; $addFolderBtn.Size='100,28'; $addFolderBtn.Location='118,24'; Set-K1GhostButton $addFolderBtn; $grpUp.Controls.Add($addFolderBtn)
$clearFilesBtn=New-Object System.Windows.Forms.Button; $clearFilesBtn.Text='Clear'; $clearFilesBtn.Size='70,28'; $clearFilesBtn.Location='224,24'; Set-K1GhostButton $clearFilesBtn; $grpUp.Controls.Add($clearFilesBtn)
$fileList=New-Object System.Windows.Forms.ListBox; $fileList.Size='400,92'; $fileList.Location='12,58'; $fileList.Font=$mono; $fileList.HorizontalScrollbar=$true; $fileList.BackColor=$surface2; $fileList.ForeColor=$text; $fileList.BorderStyle='FixedSingle'; $grpUp.Controls.Add($fileList)
$remoteLbl=New-Object System.Windows.Forms.Label; $remoteLbl.Text='Remote path:'; $remoteLbl.AutoSize=$true; $remoteLbl.Location='428,60'; Set-K1Label $remoteLbl 'muted'; $grpUp.Controls.Add($remoteLbl)
$remoteBox=New-Object System.Windows.Forms.TextBox; $remoteBox.Size='280,26'; $remoteBox.Location='428,80'; $remoteBox.Font=$mono; $remoteBox.Text='/home/booster/'; Set-K1Field $remoteBox; $grpUp.Controls.Add($remoteBox)
$pwlessChk=New-Object System.Windows.Forms.CheckBox; $pwlessChk.Text='Passwordless (SSH key installed) - show result in app'; $pwlessChk.AutoSize=$true; $pwlessChk.Location='428,112'; Set-K1Check $pwlessChk; $grpUp.Controls.Add($pwlessChk)
$uploadBtn=New-Object System.Windows.Forms.Button; $uploadBtn.Text='Upload to K1'; $uploadBtn.Size='160,34'; $uploadBtn.Location='548,138'; Set-K1OkButton $uploadBtn; $grpUp.Controls.Add($uploadBtn)
$logBox2=New-Object System.Windows.Forms.RichTextBox; $logBox2.ReadOnly=$true; $logBox2.Dock='Fill'; $logBox2.Font=$mono; Set-K1Log $logBox2
$sshLayout.BackColor=$bg; $sshLayout.Controls.Add($grpRobot,0,0); $sshLayout.Controls.Add($grpSsh,0,1); $sshLayout.Controls.Add($grpUp,0,2); $sshLayout.Controls.Add($logBox2,0,3)
$tabSsh.Controls.Add($sshLayout)

# ============================================================================
#  TAB 3 - LIVE VIEW
# ============================================================================
$liveLayout=New-Object System.Windows.Forms.TableLayoutPanel; $liveLayout.Dock='Fill'; $liveLayout.ColumnCount=1; $liveLayout.RowCount=3; $liveLayout.Padding='10,8,10,8'
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,72)))
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,96)))
$grpLiveCtl=New-Object System.Windows.Forms.GroupBox; $grpLiveCtl.Text='Camera'; $grpLiveCtl.Dock='Fill'; Set-K1Group $grpLiveCtl
$lblIpL=New-Object System.Windows.Forms.Label; $lblIpL.Text='IP:'; $lblIpL.AutoSize=$true; $lblIpL.Location='10,26'; Set-K1Label $lblIpL 'muted'; $grpLiveCtl.Controls.Add($lblIpL)
$ipLive=New-Object System.Windows.Forms.TextBox; $ipLive.Size='120,24'; $ipLive.Location='34,23'; $ipLive.Font=$mono; $ipLive.Text=$K1_DEFAULT_IP; Set-K1Field $ipLive; $grpLiveCtl.Controls.Add($ipLive)
$lblTopic=New-Object System.Windows.Forms.Label; $lblTopic.Text='Topic:'; $lblTopic.AutoSize=$true; $lblTopic.Location='164,26'; Set-K1Label $lblTopic 'muted'; $grpLiveCtl.Controls.Add($lblTopic)
$topicCombo=New-Object System.Windows.Forms.ComboBox; $topicCombo.Size='220,24'; $topicCombo.Location='208,23'; $topicCombo.DropDownStyle='DropDownList'; Set-K1Combo $topicCombo
# raw/rgb is the topic that actually delivers frames (the processed head/rgb stays at 0Hz); default to it.
[void]$topicCombo.Items.AddRange(@('/boostercamera/head/raw/rgb','/boostercamera/head/rgb','/boostercamera/head/right/rgb','/boostercamera/head/raw/combine/rgb','/boostercamera/head/depth')); $topicCombo.SelectedIndex=0; $grpLiveCtl.Controls.Add($topicCombo)
$lblFps=New-Object System.Windows.Forms.Label; $lblFps.Text='FPS:'; $lblFps.AutoSize=$true; $lblFps.Location='440,26'; Set-K1Label $lblFps 'muted'; $grpLiveCtl.Controls.Add($lblFps)
$fpsCombo=New-Object System.Windows.Forms.ComboBox; $fpsCombo.Size='55,24'; $fpsCombo.Location='474,23'; $fpsCombo.DropDownStyle='DropDownList'; Set-K1Combo $fpsCombo; [void]$fpsCombo.Items.AddRange(@('5','10','12','15','20')); $fpsCombo.SelectedIndex=2; $grpLiveCtl.Controls.Add($fpsCombo)
$lblQ=New-Object System.Windows.Forms.Label; $lblQ.Text='Q:'; $lblQ.AutoSize=$true; $lblQ.Location='536,26'; Set-K1Label $lblQ 'muted'; $grpLiveCtl.Controls.Add($lblQ)
$qCombo=New-Object System.Windows.Forms.ComboBox; $qCombo.Size='55,24'; $qCombo.Location='556,23'; $qCombo.DropDownStyle='DropDownList'; Set-K1Combo $qCombo; [void]$qCombo.Items.AddRange(@('40','55','70','85')); $qCombo.SelectedIndex=2; $grpLiveCtl.Controls.Add($qCombo)
$startLiveBtn=New-Object System.Windows.Forms.Button; $startLiveBtn.Text='Start'; $startLiveBtn.Size='70,30'; $startLiveBtn.Location='624,21'; Set-K1OkButton $startLiveBtn; $grpLiveCtl.Controls.Add($startLiveBtn)
$stopLiveBtn=New-Object System.Windows.Forms.Button; $stopLiveBtn.Text='Stop'; $stopLiveBtn.Size='60,30'; $stopLiveBtn.Location='698,21'; Set-K1GhostButton $stopLiveBtn; $stopLiveBtn.Enabled=$false; $grpLiveCtl.Controls.Add($stopLiveBtn)
$enableCamBtn=New-Object System.Windows.Forms.Button; $enableCamBtn.Text='Enable cam (beta)'; $enableCamBtn.Size='130,30'; $enableCamBtn.Location='762,21'; Set-K1GhostButton $enableCamBtn; $grpLiveCtl.Controls.Add($enableCamBtn)
$liveStatus=New-Object System.Windows.Forms.Label; $liveStatus.Text='Idle.'; $liveStatus.AutoSize=$true; $liveStatus.Location='12,50'; Set-K1Label $liveStatus 'muted'; $grpLiveCtl.Controls.Add($liveStatus)
# marker lock-on badge (driven by the per-frame status byte from the streamer)
$lockBadge=New-Object System.Windows.Forms.Label; $lockBadge.Text='  MARKER: --  '; $lockBadge.AutoSize=$false; $lockBadge.Size='250,26'; $lockBadge.TextAlign='MiddleCenter'; $lockBadge.Font=$fontBold; $lockBadge.ForeColor='White'; $lockBadge.BackColor=$chipBg; $lockBadge.Location='430,48'; $grpLiveCtl.Controls.Add($lockBadge)
$muteChk=New-Object System.Windows.Forms.CheckBox; $muteChk.Text='mute lock sound'; $muteChk.AutoSize=$true; $muteChk.Location='690,50'; Set-K1Check $muteChk; $muteChk.ForeColor=$muted; $grpLiveCtl.Controls.Add($muteChk)
$livePic=New-Object System.Windows.Forms.PictureBox; $livePic.Dock='Fill'; $livePic.BackColor=$bg; $livePic.SizeMode='Zoom'
$liveLog=New-Object System.Windows.Forms.RichTextBox; $liveLog.ReadOnly=$true; $liveLog.Dock='Fill'; $liveLog.Font=$mono; Set-K1Log $liveLog
$liveLayout.BackColor=$bg; $liveLayout.Controls.Add($grpLiveCtl,0,0); $liveLayout.Controls.Add($livePic,0,1); $liveLayout.Controls.Add($liveLog,0,2)
$tabLive.Controls.Add($liveLayout)

# ============================================================================
#  TAB 4 - CONTROL
# ============================================================================
$ctrlLayout=New-Object System.Windows.Forms.TableLayoutPanel; $ctrlLayout.Dock='Fill'; $ctrlLayout.ColumnCount=1; $ctrlLayout.RowCount=4; $ctrlLayout.Padding='10,8,10,8'
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,76)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,250)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,86)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))

$grpConn=New-Object System.Windows.Forms.GroupBox; $grpConn.Text='Controller connection'; $grpConn.Dock='Fill'; Set-K1Group $grpConn
$lblIpC=New-Object System.Windows.Forms.Label; $lblIpC.Text='IP:'; $lblIpC.AutoSize=$true; $lblIpC.Location='10,28'; Set-K1Label $lblIpC 'muted'; $grpConn.Controls.Add($lblIpC)
$ipCtrl=New-Object System.Windows.Forms.TextBox; $ipCtrl.Size='120,24'; $ipCtrl.Location='34,25'; $ipCtrl.Font=$mono; $ipCtrl.Text=$K1_DEFAULT_IP; Set-K1Field $ipCtrl; $grpConn.Controls.Add($ipCtrl)
$lblIf=New-Object System.Windows.Forms.Label; $lblIf.Text='SDK iface:'; $lblIf.AutoSize=$true; $lblIf.Location='164,28'; Set-K1Label $lblIf 'muted'; $grpConn.Controls.Add($lblIf)
$ifaceBox=New-Object System.Windows.Forms.TextBox; $ifaceBox.Size='100,24'; $ifaceBox.Location='234,25'; $ifaceBox.Font=$mono; $ifaceBox.Text=$K1_LOCO_IFACE; Set-K1Field $ifaceBox; $grpConn.Controls.Add($ifaceBox)
$connCtrlBtn=New-Object System.Windows.Forms.Button; $connCtrlBtn.Text='Connect'; $connCtrlBtn.Size='90,30'; $connCtrlBtn.Location='344,22'; Set-K1PrimaryButton $connCtrlBtn; $grpConn.Controls.Add($connCtrlBtn)
$discCtrlBtn=New-Object System.Windows.Forms.Button; $discCtrlBtn.Text='Disconnect'; $discCtrlBtn.Size='90,30'; $discCtrlBtn.Location='438,22'; Set-K1GhostButton $discCtrlBtn; $discCtrlBtn.Enabled=$false; $grpConn.Controls.Add($discCtrlBtn)
$gftBtn=New-Object System.Windows.Forms.Button; $gftBtn.Text='Test link (gft)'; $gftBtn.Size='110,30'; $gftBtn.Location='532,22'; Set-K1GhostButton $gftBtn; $gftBtn.Enabled=$false; $grpConn.Controls.Add($gftBtn)
$armChk=New-Object System.Windows.Forms.CheckBox; $armChk.Text='ARM MOTION'; $armChk.AutoSize=$true; $armChk.Location='656,28'; Set-K1Check $armChk 'danger'; $grpConn.Controls.Add($armChk)
$connStatus=New-Object System.Windows.Forms.Label; $connStatus.Text='Not connected. Connect, then ARM to enable motion. STOP/Damping stay live.'; $connStatus.AutoSize=$true; $connStatus.Location='12,52'; Set-K1Label $connStatus 'muted'; $grpConn.Controls.Add($connStatus)

$grpCmd=New-Object System.Windows.Forms.GroupBox; $grpCmd.Text='Commands'; $grpCmd.Dock='Fill'; Set-K1Group $grpCmd

# helper to add a command button
function Add-Cmd {
    param($parent,$text,$code,$x,$y,$w,[bool]$motion=$true,$color=$null)
    $b=New-Object System.Windows.Forms.Button
    $b.Text=$text; $b.Location=New-Object System.Drawing.Point($x,$y); $b.Size=New-Object System.Drawing.Size($w,34); $b.Tag=$code; $b.Enabled=$false
    if($color -eq $red){ Set-K1DangerButton $b }
    elseif($color -eq $amber){ $b.FlatStyle='Flat'; $b.FlatAppearance.BorderSize=0; $b.BackColor=$amber; $b.ForeColor=$bg; $b.Font=$fontBold; $b.Cursor=[System.Windows.Forms.Cursors]::Hand }
    elseif($color -eq $green){ Set-K1OkButton $b }
    else { Set-K1GhostButton $b }
    $b.Add_Click({ Send-Loco ($this.Tag) })
    [void]$parent.Controls.Add($b)
    if($motion){ $script:MotionButtons += $b }
    return $b
}
# Section labels
function Add-SecLabel($parent,$text,$x,$y){ $l=New-Object System.Windows.Forms.Label; $l.Text=$text; $l.AutoSize=$true; $l.Location=New-Object System.Drawing.Point($x,$y); Set-K1Label $l 'accent'; $parent.Controls.Add($l) }

# Modes
Add-SecLabel $grpCmd 'Modes' 14 22
$btnPrep=Add-Cmd $grpCmd 'Prepare (mp)' 'mp' 14 42 110 $true
$btnWalk=Add-Cmd $grpCmd 'Walking (mw)' 'mw' 128 42 110 $true
$btnCustom=Add-Cmd $grpCmd 'Custom (mc)' 'mc' 242 42 110 $true
$btnDamp=Add-Cmd $grpCmd 'DAMPING (md)' 'md' 356 42 120 $false $amber
# Move
Add-SecLabel $grpCmd 'Move' 14 86
$btnFwd=Add-Cmd $grpCmd 'Fwd (w)' 'w' 14 106 80 $true
$btnBack=Add-Cmd $grpCmd 'Back (s)' 's' 98 106 80 $true
$btnLeft=Add-Cmd $grpCmd 'Left (a)' 'a' 182 106 80 $true
$btnRight=Add-Cmd $grpCmd 'Right (d)' 'd' 266 106 80 $true
$btnTL=Add-Cmd $grpCmd 'Turn L (q)' 'q' 350 106 90 $true
$btnTR=Add-Cmd $grpCmd 'Turn R (e)' 'e' 444 106 90 $true
$btnStop=Add-Cmd $grpCmd 'STOP (l)' 'l' 542 106 100 $false $red
# Head
Add-SecLabel $grpCmd 'Head' 14 150
$btnHU=Add-Cmd $grpCmd 'Up (hu)' 'hu' 14 170 80 $true
$btnHD=Add-Cmd $grpCmd 'Down (hd)' 'hd' 98 170 80 $true
$btnHL=Add-Cmd $grpCmd 'Left (hl)' 'hl' 182 170 80 $true
$btnHR=Add-Cmd $grpCmd 'Right (hr)' 'hr' 266 170 80 $true
$btnHO=Add-Cmd $grpCmd 'Center (ho)' 'ho' 350 170 90 $true
# Gestures / posture
Add-SecLabel $grpCmd 'Gestures / posture' 460 150
$btnWave=Add-Cmd $grpCmd 'WAVE (wh)' 'wh' 460 170 100 $true $green
$btnWaveC=Add-Cmd $grpCmd 'Wave-close (ch)' 'ch' 564 170 110 $true
$btnGU=Add-Cmd $grpCmd 'Get up (gu)' 'gu' 14 210 90 $true
$btnLD=Add-Cmd $grpCmd 'Lie down (ld)' 'ld' 108 210 100 $true
$btnRock=Add-Cmd $grpCmd 'Rock' 'rock' 212 210 70 $true
$btnPaper=Add-Cmd $grpCmd 'Paper' 'paper' 286 210 70 $true
$btnScis=Add-Cmd $grpCmd 'Scissor' 'scissor' 360 210 75 $true
$btnOk=Add-Cmd $grpCmd 'OK' 'ok' 439 210 55 $true
$btnGrasp=Add-Cmd $grpCmd 'Grasp' 'grasp' 498 210 70 $true

# --- Follow marker (QR) group: a single ON/OFF toggle + a DRIVE mode checkbox ---
$grpFollow=New-Object System.Windows.Forms.GroupBox; $grpFollow.Text='Follow marker (QR / ArUco)'; $grpFollow.Dock='Fill'; Set-K1Group $grpFollow
$followToggle=New-Object System.Windows.Forms.CheckBox; $followToggle.Appearance='Button'; $followToggle.Text='Follow Marker (QR): OFF'; $followToggle.TextAlign='MiddleCenter'; $followToggle.Size='220,34'; $followToggle.Location='14,22'; $followToggle.FlatStyle='Flat'; $followToggle.Font=$fontBold; $followToggle.BackColor=$surface2; $followToggle.ForeColor=$text; $grpFollow.Controls.Add($followToggle)
$followDriveChk=New-Object System.Windows.Forms.CheckBox; $followDriveChk.Text='DRIVE (walk the robot)'; $followDriveChk.AutoSize=$true; $followDriveChk.Location='246,30'; Set-K1Check $followDriveChk 'danger'; $grpFollow.Controls.Add($followDriveChk)
$followStatus=New-Object System.Windows.Forms.Label; $followStatus.Text='marker = one-time lock onto the person, then follows that person (re-show marker to re-seed). Toggle ON for PREVIEW (no motion); tick DRIVE + ARM to walk.'; $followStatus.AutoSize=$false; $followStatus.Size='660,28'; $followStatus.Location='14,60'; Set-K1Label $followStatus 'muted'; $grpFollow.Controls.Add($followStatus)

$ctrlLog=New-Object System.Windows.Forms.RichTextBox; $ctrlLog.ReadOnly=$true; $ctrlLog.Dock='Fill'; $ctrlLog.Font=$mono; Set-K1Log $ctrlLog
$ctrlLayout.BackColor=$bg; $ctrlLayout.Controls.Add($grpConn,0,0); $ctrlLayout.Controls.Add($grpCmd,0,1); $ctrlLayout.Controls.Add($grpFollow,0,2); $ctrlLayout.Controls.Add($ctrlLog,0,3)
$tabCtrl.Controls.Add($ctrlLayout)

# ============================================================================
#  TAB 5 - ROBOT FILES (SDK browser, manifest-driven)
# ============================================================================
# Synchronized state for the two background jobs (refresh + single-file view).
# Background runspaces ONLY write these; the UI thread drains them in $mediaTimer.
$filesSync = [hashtable]::Synchronized(@{ Running=$false; Done=$false; Ok=$false; Log=(New-Object System.Collections.Queue); Stage=''; PS=$null; RS=$null; Handle=$null })
$viewSync  = [hashtable]::Synchronized(@{ Running=$false; Done=$false; Ok=$false; Path=''; Local=''; Size=0; Msg=''; PS=$null; RS=$null; Handle=$null })

# Cache of the parsed manifest so other tabs can self-correct hard-coded paths.
$script:RobotPaths = @{}

$filesLayout=New-Object System.Windows.Forms.TableLayoutPanel; $filesLayout.Dock='Fill'; $filesLayout.ColumnCount=1; $filesLayout.RowCount=2; $filesLayout.Padding='10,8,10,8'
[void]$filesLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,64)))
[void]$filesLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))

# --- top bar: IP + Refresh + status ---------------------------------------
$grpFilesTop=New-Object System.Windows.Forms.GroupBox; $grpFilesTop.Text='Robot file manifest'; $grpFilesTop.Dock='Fill'; Set-K1Group $grpFilesTop
$lblIpF=New-Object System.Windows.Forms.Label; $lblIpF.Text='IP:'; $lblIpF.AutoSize=$true; $lblIpF.Location='10,26'; Set-K1Label $lblIpF 'muted'; $grpFilesTop.Controls.Add($lblIpF)
$ipFiles=New-Object System.Windows.Forms.TextBox; $ipFiles.Size='120,24'; $ipFiles.Location='34,23'; $ipFiles.Font=$mono; $ipFiles.Text=$(if($script:RobotIP){$script:RobotIP}else{$K1_DEFAULT_IP}); Set-K1Field $ipFiles; $grpFilesTop.Controls.Add($ipFiles)
$refreshFilesBtn=New-Object System.Windows.Forms.Button; $refreshFilesBtn.Text='Refresh from robot'; $refreshFilesBtn.Size='150,30'; $refreshFilesBtn.Location='164,20'; Set-K1PrimaryButton $refreshFilesBtn; $grpFilesTop.Controls.Add($refreshFilesBtn)
$filesStatus=New-Object System.Windows.Forms.Label; $filesStatus.Text='Click "Refresh from robot" to deploy + run tree_manifest.py and load the SDK layout.'; $filesStatus.AutoSize=$true; $filesStatus.MaximumSize='520,40'; $filesStatus.Location='326,24'; Set-K1Label $filesStatus 'muted'; $grpFilesTop.Controls.Add($filesStatus)

# --- body: left TreeView | right (key-paths list over file preview) --------
$filesSplit=New-Object System.Windows.Forms.SplitContainer; $filesSplit.Dock='Fill'; $filesSplit.Orientation='Vertical'; $filesSplit.SplitterWidth=6; $filesSplit.Panel1MinSize=180; $filesSplit.Panel2MinSize=220; $filesSplit.BackColor=$bg; $filesSplit.Panel1.BackColor=$bg; $filesSplit.Panel2.BackColor=$bg

# left: SDK + /home/booster tree
$grpTree=New-Object System.Windows.Forms.GroupBox; $grpTree.Text='SDK + /home/booster'; $grpTree.Dock='Fill'; Set-K1Group $grpTree
$filesTree=New-Object System.Windows.Forms.TreeView; $filesTree.Dock='Fill'; $filesTree.Font=$mono; $filesTree.HideSelection=$false; $filesTree.ShowLines=$true; $filesTree.PathSeparator='/'; $filesTree.BackColor=$surface; $filesTree.ForeColor=$text; $filesTree.BorderStyle='None'
$grpTree.Controls.Add($filesTree)
$filesSplit.Panel1.Controls.Add($grpTree)

# right: key-paths list (top) over file preview (bottom)
$rightLayout=New-Object System.Windows.Forms.TableLayoutPanel; $rightLayout.Dock='Fill'; $rightLayout.ColumnCount=1; $rightLayout.RowCount=2; $rightLayout.BackColor=$bg
[void]$rightLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,46)))
[void]$rightLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,54)))

$grpKeys=New-Object System.Windows.Forms.GroupBox; $grpKeys.Text='Key paths (from k1_paths.json)'; $grpKeys.Dock='Fill'; Set-K1Group $grpKeys
$keysList=New-Object System.Windows.Forms.ListView; $keysList.View='Details'; $keysList.FullRowSelect=$true; $keysList.GridLines=$false; $keysList.MultiSelect=$false; $keysList.HideSelection=$false; $keysList.Dock='Fill'; $keysList.Font=$font; Set-K1List $keysList
[void]$keysList.Columns.Add('Key',150); [void]$keysList.Columns.Add('Path',360); [void]$keysList.Columns.Add('Exists',70)
$grpKeys.Controls.Add($keysList)

$grpPreview=New-Object System.Windows.Forms.GroupBox; $grpPreview.Text='File preview (double-click a FILE in the tree)'; $grpPreview.Dock='Fill'; Set-K1Group $grpPreview
$previewBox=New-Object System.Windows.Forms.RichTextBox; $previewBox.ReadOnly=$true; $previewBox.Dock='Fill'; $previewBox.Font=$mono; $previewBox.WordWrap=$false; $previewBox.DetectUrls=$false; Set-K1Log $previewBox
$grpPreview.Controls.Add($previewBox)

$rightLayout.Controls.Add($grpKeys,0,0); $rightLayout.Controls.Add($grpPreview,0,1)
$filesSplit.Panel2.Controls.Add($rightLayout)

$filesLayout.BackColor=$bg; $filesLayout.Controls.Add($grpFilesTop,0,0); $filesLayout.Controls.Add($filesSplit,0,1)
$tabFiles.Controls.Add($filesLayout)

# ============================================================================
#  TAB 6 - TRACKER  (cockpit for the marker-seeded markerless PERSON-follow)
#  Hierarchy: Primary (Follow/DRIVE/ARM/STOP + IP/Distance/Speed)
#             Avoidance (gap/scan/hit/escape + brakes/maps)
#             Advanced (perception/gesture/voice/REID/rerun/controller)
#             Cmd row (WAIT/RESUME/PARK/STATUS/FOLLOW + Open .rrd)
# ============================================================================
$trackLayout=New-Object System.Windows.Forms.TableLayoutPanel; $trackLayout.Dock='Fill'; $trackLayout.ColumnCount=1; $trackLayout.RowCount=4; $trackLayout.Padding='10,8,10,8'; $trackLayout.BackColor=$bg
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,292)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,54)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,110)))

# --- control panel (taller; no overlapping rows) ---------------------------
$grpTrackCtl=New-Object System.Windows.Forms.GroupBox; $grpTrackCtl.Text='Tracker  ·  Primary / Avoidance / Advanced / Cmd'; $grpTrackCtl.Dock='Fill'; Set-K1Group $grpTrackCtl

# === PRIMARY: Follow / DRIVE / ARM / STOP + IP + Distance + Speed ==========
$lblPrimary=New-Object System.Windows.Forms.Label; $lblPrimary.Text='PRIMARY'; $lblPrimary.AutoSize=$true; $lblPrimary.Location='14,4'; Set-K1Label $lblPrimary 'section'; $grpTrackCtl.Controls.Add($lblPrimary)
$trackToggle=New-Object System.Windows.Forms.CheckBox; $trackToggle.Appearance='Button'; $trackToggle.Text='Follow: OFF'; $trackToggle.TextAlign='MiddleCenter'; $trackToggle.Size='150,34'; $trackToggle.Location='14,22'; $trackToggle.FlatStyle='Flat'; $trackToggle.Font=$fontBold; $trackToggle.BackColor=$surface2; $trackToggle.ForeColor=$text; $grpTrackCtl.Controls.Add($trackToggle)
$trackDriveChk=New-Object System.Windows.Forms.CheckBox; $trackDriveChk.Text='DRIVE (walk)'; $trackDriveChk.AutoSize=$true; $trackDriveChk.Location='178,30'; Set-K1Check $trackDriveChk 'danger'; $grpTrackCtl.Controls.Add($trackDriveChk)
$trackArmChk=New-Object System.Windows.Forms.CheckBox; $trackArmChk.Text='ARM MOTION'; $trackArmChk.AutoSize=$true; $trackArmChk.Location='302,30'; Set-K1Check $trackArmChk 'danger'; $grpTrackCtl.Controls.Add($trackArmChk)
$trackStopBtn=New-Object System.Windows.Forms.Button; $trackStopBtn.Text='STOP'; $trackStopBtn.Size='100,34'; $trackStopBtn.Location='434,22'; Set-K1DangerButton $trackStopBtn; $grpTrackCtl.Controls.Add($trackStopBtn)
$trackMuteChk=New-Object System.Windows.Forms.CheckBox; $trackMuteChk.Text='mute lock sound'; $trackMuteChk.AutoSize=$true; $trackMuteChk.Location='554,30'; Set-K1Check $trackMuteChk; $trackMuteChk.ForeColor=$muted; $grpTrackCtl.Controls.Add($trackMuteChk)

$lblIpT=New-Object System.Windows.Forms.Label; $lblIpT.Text='IP:'; $lblIpT.AutoSize=$true; $lblIpT.Location='14,68'; Set-K1Label $lblIpT 'muted'; $grpTrackCtl.Controls.Add($lblIpT)
$ipTrack=New-Object System.Windows.Forms.TextBox; $ipTrack.Size='120,24'; $ipTrack.Location='40,64'; $ipTrack.Font=$mono; $ipTrack.Text=$(if($script:RobotIP){$script:RobotIP}else{$K1_DEFAULT_IP}); Set-K1Field $ipTrack; $grpTrackCtl.Controls.Add($ipTrack)

# Distance / standoff slider (TrackBar ticks in 0.1 m: 6..30 -> 0.6..3.0 m, default 12 -> 1.2 m)
$lblDistCap=New-Object System.Windows.Forms.Label; $lblDistCap.Text='Distance:'; $lblDistCap.AutoSize=$true; $lblDistCap.Location='178,68'; Set-K1Label $lblDistCap 'muted'; $grpTrackCtl.Controls.Add($lblDistCap)
$distTrack=New-Object System.Windows.Forms.TrackBar; $distTrack.Minimum=6; $distTrack.Maximum=30; $distTrack.TickFrequency=2; $distTrack.SmallChange=1; $distTrack.LargeChange=2; $distTrack.Value=12; $distTrack.Size='150,40'; $distTrack.Location='244,56'; $distTrack.BackColor=$panelBg; $grpTrackCtl.Controls.Add($distTrack)
$lblDistVal=New-Object System.Windows.Forms.Label; $lblDistVal.Text='1.2 m'; $lblDistVal.AutoSize=$false; $lblDistVal.Size='52,20'; $lblDistVal.TextAlign='MiddleLeft'; $lblDistVal.Font=$fontBold; $lblDistVal.Location='398,68'; $lblDistVal.ForeColor=$text; $lblDistVal.BackColor=$panelBg; $grpTrackCtl.Controls.Add($lblDistVal)

# Speed / vx-max slider (TrackBar ticks in 0.01 m/s: 5..30 -> 0.05..0.30 m/s, default 18 -> 0.18 m/s)
$lblSpdCap=New-Object System.Windows.Forms.Label; $lblSpdCap.Text='Speed:'; $lblSpdCap.AutoSize=$true; $lblSpdCap.Location='460,68'; Set-K1Label $lblSpdCap 'muted'; $grpTrackCtl.Controls.Add($lblSpdCap)
$spdTrack=New-Object System.Windows.Forms.TrackBar; $spdTrack.Minimum=5; $spdTrack.Maximum=30; $spdTrack.TickFrequency=5; $spdTrack.SmallChange=1; $spdTrack.LargeChange=5; $spdTrack.Value=18; $spdTrack.Size='150,40'; $spdTrack.Location='510,56'; $spdTrack.BackColor=$panelBg; $grpTrackCtl.Controls.Add($spdTrack)
$lblSpdVal=New-Object System.Windows.Forms.Label; $lblSpdVal.Text='0.18 m/s'; $lblSpdVal.AutoSize=$false; $lblSpdVal.Size='66,20'; $lblSpdVal.TextAlign='MiddleLeft'; $lblSpdVal.Font=$fontBold; $lblSpdVal.Location='664,68'; $lblSpdVal.ForeColor=$text; $lblSpdVal.BackColor=$panelBg; $grpTrackCtl.Controls.Add($lblSpdVal)

# === AVOIDANCE =============================================================
$lblAvoid=New-Object System.Windows.Forms.Label; $lblAvoid.Text='AVOIDANCE'; $lblAvoid.AutoSize=$true; $lblAvoid.Location='14,104'; Set-K1Label $lblAvoid 'section'; $grpTrackCtl.Controls.Add($lblAvoid)

# GAP STEER (stage 5): route AROUND a blocked corridor instead of only stopping for it.
#   off   - shipped behaviour, brake only (default)
#   audit - logs SECTOR L/C/R + the bias it WOULD apply, commands nothing. Run this FIRST.
#   on    - actually steers: yaw bias toward a measured-clear side. Forward speed still sits
#           entirely under the obstacle brake, so it turns toward the gap with vx capped and
#           forward resumes by itself once the rotation puts the gap in the centre corridor.
$lblGap=New-Object System.Windows.Forms.Label; $lblGap.Text='Gap steer'; $lblGap.AutoSize=$true; $lblGap.Location='14,128'; Set-K1Label $lblGap 'muted'; $grpTrackCtl.Controls.Add($lblGap)
$trackGap=New-Object System.Windows.Forms.ComboBox; $trackGap.DropDownStyle='DropDownList'; $trackGap.Size='74,24'; $trackGap.Location='86,124'; Set-K1Combo $trackGap; [void]$trackGap.Items.AddRange(@('off','audit','on')); $trackGap.SelectedIndex=0; $grpTrackCtl.Controls.Add($trackGap)
# HEAD SCAN (--head-scan). The freeze fix: when the brake has fully stopped forward motion but the
# operator is still beyond the standoff, the robot sweeps its head across the room, samples the
# depth corridor at each position, re-centres, and picks the freest heading. An obstacle at
# 0.4-0.6 m fills the 105.8 deg field of view, so from a fixed camera NEITHER side can be judged
# and gap steer has nothing to commit to -- the sweep sees ~170 deg instead.
# YAW ONLY: forward speed stays under the brake, so a wrong result turns the robot on the spot.
# Start on 'audit' -- it performs the sweep and logs the HEAD-SCAN map without steering, which is
# also how the head yaw -> left/right convention gets confirmed from the cx-evidence field.
$lblScan=New-Object System.Windows.Forms.Label; $lblScan.Text='Head scan'; $lblScan.AutoSize=$true; $lblScan.Location='172,128'; Set-K1Label $lblScan 'muted'; $grpTrackCtl.Controls.Add($lblScan)
# 'patient' == on, but the way ahead must stay blocked 2 s before it sweeps. The scan otherwise
# starts on the FIRST blocked frame, and stops flicker (CLEARANCE, then blob=0px clear, then
# CLEARANCE), so it swept constantly and mostly aborted mid-sweep -- 16 starts in one 300 s run,
# most ending "ABORT orphaned". A real obstacle holds the block; flicker does not.
$trackScan=New-Object System.Windows.Forms.ComboBox; $trackScan.DropDownStyle='DropDownList'; $trackScan.Size='74,24'; $trackScan.Location='244,124'; Set-K1Combo $trackScan; [void]$trackScan.Items.AddRange(@('off','audit','on','patient')); $trackScan.SelectedIndex=0; $grpTrackCtl.Controls.Add($trackScan)

# HIT BOX (--corridor-mode footprint). Selects depth returns by the robot's OWN physical extent
# instead of a fixed image fraction. Fixes three measured faults of the fraction corridor:
#   * it goes BLIND at hand height (0.67 m) inside 0.4 m, which is the clipped-hands report --
#     an obstacle is tracked from 1.0 m down to 0.5 m then lost over the final 20 cm;
#   * it MISSES an in-path object 0.30 m off centre at 0.5 m (reports it clear);
#   * it is absurdly over-wide at range (5.09 m at 3.5 m), braking for furniture off the shoulder.
# Also excludes the floor by GEOMETRY, so the band cap that caused the blindness is not needed.
# Dimensions come from the robot's own URDF: 0.457 m lateral, 0.192 m deep, hands at 0.67 m.
$lblHit=New-Object System.Windows.Forms.Label; $lblHit.Text='Hit box'; $lblHit.AutoSize=$true; $lblHit.Location='332,128'; Set-K1Label $lblHit 'muted'; $grpTrackCtl.Controls.Add($lblHit)
$trackHitBox=New-Object System.Windows.Forms.ComboBox; $trackHitBox.DropDownStyle='DropDownList'; $trackHitBox.Size='90,24'; $trackHitBox.Location='388,124'; Set-K1Combo $trackHitBox; [void]$trackHitBox.Items.AddRange(@('frac','footprint')); $trackHitBox.SelectedIndex=1; $grpTrackCtl.Controls.Add($trackHitBox)   # DEFAULT footprint: required for Local map + Map assist to engage (map-assist is footprint-gated)

# ESCAPE (--body-scan / --reverse-when-stuck). What to do when the brake has stopped the robot and
# NEITHER gap steer nor the head scan can find a way past. That is not stubbornness: from 0.35 m off
# a wide obstacle the gap sits ~68 deg off-centre, outside the head sweep (+/-23) AND the camera
# half-field (52.9), so it is unobservable from there. Measured as every head-scan direction
# returning 0.33-0.41 m while the robot shuffled and never committed.
#   spin    = rotate a full turn sampling free space per heading, then face the middle of the widest
#             gap. Rotating is NOT blind -- the robot sees everything it turns past.
#   spin+back = also allow a short bounded REVERSE as a last resort when a turn finds nothing. That
#             one IS blind (no rear sensor), so it only ever retraces ground just walked forward.
$lblEsc=New-Object System.Windows.Forms.Label; $lblEsc.Text='Escape'; $lblEsc.AutoSize=$true; $lblEsc.Location='492,128'; Set-K1Label $lblEsc 'muted'; $grpTrackCtl.Controls.Add($lblEsc)
$trackEscape=New-Object System.Windows.Forms.ComboBox; $trackEscape.DropDownStyle='DropDownList'; $trackEscape.Size='90,24'; $trackEscape.Location='542,124'; Set-K1Combo $trackEscape; [void]$trackEscape.Items.AddRange(@('off','spin','spin+back')); $trackEscape.SelectedIndex=0; $grpTrackCtl.Controls.Add($trackEscape)

# OBSTACLE BRAKE (--obstacle-brake, OBSTACLE_LABELING_PLAN.md Phase 3). Depth forward-clearance
# reflex: grades forward vx down from --obstacle-brake-start (1.5 m) to ZERO at --obstacle-brake-stop
# (0.7 m). Percentile + aged-median (never a raw min -> a single depth glitch cannot false-brake),
# IGNORES the followed operator (--obstacle-target-margin), yaw untouched, fail-to-stop with no depth.
# Only ever REDUCES vx, so it cannot make the forward path less safe. DEFAULT ON: it was built
# 2026-07-04 but never wired here, so every session before 2026-09-03 drove with NO obstacle braking.
$trackObstacle=New-Object System.Windows.Forms.CheckBox; $trackObstacle.Text='Obstacle brake'; $trackObstacle.AutoSize=$true; $trackObstacle.Location='652,126'; Set-K1Check $trackObstacle 'accent'; $trackObstacle.Checked=$true; $grpTrackCtl.Controls.Add($trackObstacle)
# Plan A class-aware brake (--obstacle-class-brake). Geometry still triggers; COCO class may only
# TIGHTEN the vx cap (never loosen, never class-only without depth). DEFAULT OFF until Batch-Label
# tally on laptop runs looks sane (docs/PLAN_A_CLASS_AWARE_BRAKE.md). Requires Obstacle brake on.
$trackClassBrake=New-Object System.Windows.Forms.CheckBox; $trackClassBrake.Text='Class brake'; $trackClassBrake.AutoSize=$true; $trackClassBrake.Location='792,126'; Set-K1Check $trackClassBrake; $trackClassBrake.Checked=$false; $grpTrackCtl.Controls.Add($trackClassBrake)

# FLOOR REJECT (--ground-reject). The hit box computes a pixel's height assuming a LEVEL camera;
# the real pitch is ~10 deg, and the resulting z*sin(pitch) error scales with RANGE, so the floor
# plane tilts up into the height window and open floor reads as an obstacle at 0.43-0.80 m with vx
# capped to zero -- 97 of 219 clearance readings in one 300 s run, AFTER the robot's own arm had
# already been excluded. The pitch itself could not be measured (only 4 of ~1000 archived frames
# give a confident ground fit, and no recording carries the head pose), so this identifies the floor
# by SHAPE instead: a plane seen obliquely has height strongly correlated with depth (-0.76..-0.94
# measured) where a compact object does not (+0.45..+0.96).
# DEFAULT OFF, and deliberately a choice rather than a default: a tabletop is also a horizontal
# plane, so this is the only control on this page that can HIDE a real obstacle. It is gated on a
# large blob so ambiguous ones keep braking. Turn it on, then watch the log for GROUND-REJECT lines
# and check each one was really floor.
$trackFloor=New-Object System.Windows.Forms.CheckBox; $trackFloor.Text='Floor reject'; $trackFloor.AutoSize=$true; $trackFloor.Location='14,154'; Set-K1Check $trackFloor 'accent'; $grpTrackCtl.Controls.Add($trackFloor)
# LOCAL MAP (--localmap): a short-horizon occupancy memory from the robot's OWN depth + odometry, so
# it remembers an obstacle after turning away from it instead of forgetting it (obstacle_memory holds
# ONE, wiped past a 25 deg turn). Only ever REDUCES clearance -- it can brake for something the live
# view has lost, never release the brake for something it can see. Needs --odom-topic (added below).
# The three 2026-09-05 audit defects (blind-frame release, cell-key sign, operator-wake writes) are
# fixed and gated. Default OFF; footprint hit box only (its height model is what places cells).
$trackLocalMap=New-Object System.Windows.Forms.CheckBox; $trackLocalMap.Text='Local map'; $trackLocalMap.AutoSize=$true; $trackLocalMap.Location='128,154'; Set-K1Check $trackLocalMap 'accent'; $trackLocalMap.Checked=$true; $grpTrackCtl.Controls.Add($trackLocalMap)
# MAP ASSIST (--map-assist): the pre-built Aurora 3D map REINFORCES a live obstacle the local
# avoidance already sees at the same range; a map obstacle the live view does not confirm is
# discarded, and it never releases the brake. Pose comes from the robot's OWN odometry, not the
# Aurora. Assistive, not primary. Footprint hit box only; default OFF, byte-identical when off.
# (36bf74a had made it ON by default. With the Aurora retired nothing publishes /aurora_odom, so every
# DRIVE from a freshly started app waited 30 s for that pose and DRIVE-ABORTed -- 2026-09-10 readiness.)
$trackMapAssist=New-Object System.Windows.Forms.CheckBox; $trackMapAssist.Text='Map assist'; $trackMapAssist.AutoSize=$true; $trackMapAssist.Location='228,154'; Set-K1Check $trackMapAssist 'accent'; $trackMapAssist.Checked=$false; $grpTrackCtl.Controls.Add($trackMapAssist)
# HEAD PROBE (--head-probe). ONE-SHOT startup calibration for the head, NOT a follow feature.
# The firmware MODE-GATES RotateHead: it answers 400 (bad request) in kPrepare and is accepted only
# in kWalking -- so the head cannot be checked on a parked robot, and the probe has to ride along
# with a real session. It runs after kWalking is entered but BEFORE velocity is ungated, so the only
# thing that can move is the head. Costs ~5 s of startup. Leave it on until HEAD-PROBE OK appears in
# the log with the measured sign, then it can be switched off.
$trackHeadProbe=New-Object System.Windows.Forms.CheckBox; $trackHeadProbe.Text='Head probe'; $trackHeadProbe.AutoSize=$true; $trackHeadProbe.Location='336,154'; Set-K1Check $trackHeadProbe 'accent'; $trackHeadProbe.Checked=$true; $grpTrackCtl.Controls.Add($trackHeadProbe)

# === ADVANCED ==============================================================
$lblAdv=New-Object System.Windows.Forms.Label; $lblAdv.Text='ADVANCED'; $lblAdv.AutoSize=$true; $lblAdv.Location='14,186'; Set-K1Label $lblAdv 'section'; $grpTrackCtl.Controls.Add($lblAdv)

$lblPercep=New-Object System.Windows.Forms.Label; $lblPercep.Text='Perception:'; $lblPercep.AutoSize=$true; $lblPercep.Location='110,186'; Set-K1Label $lblPercep 'muted'; $grpTrackCtl.Controls.Add($lblPercep)
# 'striped' removed from the operator-facing list: it is the documented lock-loser (degraded then lost the lock).
# The node's --appearance striped path is left intact for a dev who passes it via the CLI (Get-TrackExtraArgs still
# emits '--appearance striped' when $app -eq 'striped'). Items are now global,osnet -> osnet is index 1.
$trackApp=New-Object System.Windows.Forms.ComboBox; $trackApp.DropDownStyle='DropDownList'; $trackApp.Size='92,24'; $trackApp.Location='188,182'; Set-K1Combo $trackApp; [void]$trackApp.Items.AddRange(@('global','osnet')); $trackApp.SelectedIndex=1; $grpTrackCtl.Controls.Add($trackApp)   # default OSNet (deep ReID; armed re-lock needs it)
$trackCoast=New-Object System.Windows.Forms.CheckBox; $trackCoast.Text='Coast occlusions'; $trackCoast.AutoSize=$true; $trackCoast.Location='292,184'; Set-K1Check $trackCoast; $grpTrackCtl.Controls.Add($trackCoast)
$trackReacq=New-Object System.Windows.Forms.CheckBox; $trackReacq.Text='Auto re-acq'; $trackReacq.AutoSize=$true; $trackReacq.Location='430,184'; Set-K1Check $trackReacq; $trackReacq.Checked=$true; $grpTrackCtl.Controls.Add($trackReacq)
$trackFence=New-Object System.Windows.Forms.CheckBox; $trackFence.Text='Range fence'; $trackFence.AutoSize=$true; $trackFence.Location='538,184'; Set-K1Check $trackFence; $grpTrackCtl.Controls.Add($trackFence)
# DANGER: armed markerless re-lock (--arm-reacquire). OSNet only -- the node refuses it on the weak
# backends. Default OFF; preview-verify it re-locks onto YOU before driving with it on.
$trackArmReloc=New-Object System.Windows.Forms.CheckBox; $trackArmReloc.Text='Arm re-lock'; $trackArmReloc.AutoSize=$true; $trackArmReloc.Location='646,184'; Set-K1Check $trackArmReloc 'danger'; $grpTrackCtl.Controls.Add($trackArmReloc)
# Deadman HB (P2 #12): passes --require-heartbeat to the node (which also env-arms the bridge's own
# K1_REQUIRE_HB watchdog) and starts the app-side heartbeat relay. The remote loop touches the hb
# file ONLY on bytes RECEIVED from this app, so mtime freshness == end-to-end connectivity: WiFi
# drop / app freeze / laptop death -> touches stop -> node zeroes (400ms) + bridge kPrepares (1.5s).
# Default ON (P1.2): the node now REFUSES --drive without --require-heartbeat (fail-closed gate),
# so an unchecked box = guaranteed launch abort, not the old silent-degrade. Safe flag is the
# default; untick only for a tethered bench run WITH --allow-untethered-unsafe intent.
# One pillar of the untethered gate (UNTETHERED_FOLLOW.md).
$trackHbChk=New-Object System.Windows.Forms.CheckBox; $trackHbChk.Text='Deadman HB'; $trackHbChk.AutoSize=$true; $trackHbChk.Location='758,184'; Set-K1Check $trackHbChk 'danger'; $trackHbChk.Checked=$true; $grpTrackCtl.Controls.Add($trackHbChk)

# --- Acquisition + command-surface toggles (LAUNCH-time for gesture/A-B; runtime for voice) ---
# Gesture lock = --lock-trigger gesture (raised hand seeds instead of the marker). A/B = --lock-trigger
# both (ArUco still drives, gesture audits -> GBIND data to retire ArUco). Voice = System.Speech keyword
# recognizer that maps spoken words to the SAME command enum the Cmd buttons send.
$chkGesture=New-Object System.Windows.Forms.CheckBox; $chkGesture.Text='Gesture lock'; $chkGesture.AutoSize=$true; $chkGesture.Location='14,216'; Set-K1Check $chkGesture 'accent'; $chkGesture.Checked=$true; $grpTrackCtl.Controls.Add($chkGesture)   # DEFAULT ON (2026-07-05, user request): gesture is the default lock trigger; UNTICK for the (more reliable) ArUco marker. Raise a hand DURING SEARCH to seed.
$chkAB=New-Object System.Windows.Forms.CheckBox; $chkAB.Text='A/B (compare)'; $chkAB.AutoSize=$true; $chkAB.Location='134,216'; Set-K1Check $chkAB; $grpTrackCtl.Controls.Add($chkAB)
$chkVoice=New-Object System.Windows.Forms.CheckBox; $chkVoice.Text='Voice cmds'; $chkVoice.AutoSize=$true; $chkVoice.Location='254,216'; Set-K1Check $chkVoice 'accent'; $chkVoice.Enabled=$false; $grpTrackCtl.Controls.Add($chkVoice)
$voiceStatus=New-Object System.Windows.Forms.Label; $voiceStatus.Text='Voice: off'; $voiceStatus.AutoSize=$true; $voiceStatus.Location='354,218'; Set-K1Label $voiceStatus 'muted'; $grpTrackCtl.Controls.Add($voiceStatus)
# --- persistent ReID-health badge (parsed from the node's REID-ENGINE ok/FAILED + REID-DEGRADED stderr) ---
# OSNet(TRT)/OSNet(CUDA)=green, CPU-EP=amber, HIST fallback / DEGRADED=red, '--'=idle. Set by Update-ReidBadge.
$reidBadge=New-Object System.Windows.Forms.Label; $reidBadge.Text='REID: --'; $reidBadge.AutoSize=$false; $reidBadge.Size='168,22'; $reidBadge.TextAlign='MiddleCenter'; $reidBadge.Location='448,214'; $reidBadge.ForeColor='White'; $reidBadge.BackColor=$chipBg; $reidBadge.Font=$fontBold; $reidBadge.BorderStyle='FixedSingle'; $grpTrackCtl.Controls.Add($reidBadge)
# Rerun (rerun.io) recording toggle -> --rerun (Phase 3). Default OFF and byte-identical to today
# when off. Records a scrubbable .rrd on the robot; pull+open it with the 'Open .rrd' button below.
$trackRerun=New-Object System.Windows.Forms.CheckBox; $trackRerun.Text='Rerun'; $trackRerun.AutoSize=$true; $trackRerun.Location='628,216'; Set-K1Check $trackRerun 'accent'; $grpTrackCtl.Controls.Add($trackRerun)
# CONTROLLER RUN (2026-09-10 operator request): collect a full data bundle while driving the robot
# MANUALLY on the Booster gamepad, with no autonomous following at all.
#
# HOW IT WORKS. Forces the node into --preview and adds --profile capture. Preview never opens the
# bridge, so the node CANNOT command velocity and the gamepad's own firmware path is untouched -- the
# operator drives, the node only watches. --profile capture turns on the recording bundle (rerun RGB +
# depth + scalars + P6.1 intrinsics + /odometer_state) that demo/field deliberately leave off for
# loop cost. We keep run_follow.sh (not run_follow_capture.sh) so --stream survives and the app still
# shows live video while you drive; run_follow_capture.sh omits --stream and the preview would go dark.
#
# WHY THIS BEATS A BLIND RECORDING. Detection, tracking and re-ID all still run in preview, so the
# .rrd carries per-frame person boxes, track ids and depth alongside the images -- labelled data for
# free, instead of raw video to annotate later.
#
# SAFETY: this OVERRIDES the drive button (see the $mode line at launch). Ticked, the follow cannot be
# commanded to drive under any circumstance, which is what makes it safe to walk the robot by hand.
$trackCtrlRun=New-Object System.Windows.Forms.CheckBox; $trackCtrlRun.Text='Controller run (no follow)'; $trackCtrlRun.AutoSize=$true; $trackCtrlRun.Location='710,216'; Set-K1Check $trackCtrlRun 'caution'; $grpTrackCtl.Controls.Add($trackCtrlRun)
$trackCtrlRun.Add_CheckedChanged({
    if($trackCtrlRun.Checked){
        $trackRerun.Checked = $true    # the recording bundle IS the point of this mode
        Add-LogTrack 'CONTROLLER RUN armed: forces --preview + --profile capture. The follow will NOT drive -- use the Booster gamepad. Recording RGB+depth+odom.' $amber
    } else {
        Add-LogTrack 'Controller run disarmed -- normal follow behaviour restored.' $accent
    }
})
$chkGesture.Add_CheckedChanged({ if($chkGesture.Checked -and $chkAB.Checked){ $chkAB.Checked=$false } })
$chkAB.Add_CheckedChanged({ if($chkAB.Checked -and $chkGesture.Checked){ $chkGesture.Checked=$false } })
$chkVoice.Add_CheckedChanged({ if($chkVoice.Checked){ Start-Voice } else { Stop-Voice } })

# === CMD ROW ===============================================================
# Tier-1 command row (watched-file channel /tmp/k1_cmd; enabled ONLY while a follow session runs).
# De-escalating WAIT/PARK/STATUS go immediately; RESUME/FOLLOW carry the per-command ARM credential
# under DRIVE (ARM MOTION + a typed confirm). The Ctrl-C STOP button stays the supreme deadman.
$cmdLbl=New-Object System.Windows.Forms.Label; $cmdLbl.Text='Cmd:'; $cmdLbl.AutoSize=$true; $cmdLbl.Location='14,254'; Set-K1Label $cmdLbl 'muted'; $grpTrackCtl.Controls.Add($cmdLbl)
$btnWait=New-Object System.Windows.Forms.Button; $btnWait.Text='WAIT'; $btnWait.Size='64,28'; $btnWait.Location='52,248'; Set-K1GhostButton $btnWait; $btnWait.Enabled=$false; $grpTrackCtl.Controls.Add($btnWait)
$btnResume=New-Object System.Windows.Forms.Button; $btnResume.Text='RESUME'; $btnResume.Size='76,28'; $btnResume.Location='122,248'; Set-K1GhostButton $btnResume; $btnResume.Enabled=$false; $grpTrackCtl.Controls.Add($btnResume)
$btnPark=New-Object System.Windows.Forms.Button; $btnPark.Text='PARK'; $btnPark.Size='64,28'; $btnPark.Location='204,248'; Set-K1GhostButton $btnPark; $btnPark.Enabled=$false; $grpTrackCtl.Controls.Add($btnPark)
$btnStatus=New-Object System.Windows.Forms.Button; $btnStatus.Text='STATUS'; $btnStatus.Size='72,28'; $btnStatus.Location='274,248'; Set-K1GhostButton $btnStatus; $btnStatus.Enabled=$false; $grpTrackCtl.Controls.Add($btnStatus)
$btnFollowCmd=New-Object System.Windows.Forms.Button; $btnFollowCmd.Text='FOLLOW'; $btnFollowCmd.Size='72,28'; $btnFollowCmd.Location='352,248'; Set-K1GhostButton $btnFollowCmd; $btnFollowCmd.Enabled=$false; $grpTrackCtl.Controls.Add($btnFollowCmd)
$btnWait.Add_Click({ Send-FollowCmd 'HOLD' })
$btnResume.Add_Click({ Send-FollowCmd 'RESUME' })
$btnPark.Add_Click({ Send-FollowCmd 'PARK' })
$btnStatus.Add_Click({ Send-FollowCmd 'STATUS' })
$btnFollowCmd.Add_Click({ Send-FollowCmd 'FOLLOW' })
# Rerun (Phase 3): pull the newest .rrd off the robot and open it in the laptop viewer. Always
# enabled (unlike the session-only Cmd buttons) -- you scrub AFTER a run. No-op-safe if none exists.
# Placed clear of Controller run / avoidance combos (was overlapping "Open .rrd" on the dense layout).
$btnRrd=New-Object System.Windows.Forms.Button; $btnRrd.Text='Open .rrd'; $btnRrd.Size='110,28'; $btnRrd.Location='440,248'; Set-K1GhostButton $btnRrd; $btnRrd.ForeColor=$accent; $grpTrackCtl.Controls.Add($btnRrd)
$btnRrd.Add_Click({
    $ip=$ipTrack.Text.Trim(); if(-not $ip){ Add-LogTrack 'Enter the robot IP first.' $amber; return }
    $script:RobotIP=$ip
    # Review fix: the newest .rrd during a live Rerun follow is the file being WRITTEN -- pulling it
    # yields a truncated snapshot missing the most recent (most diagnostic) seconds. Warn + confirm.
    if($script:TrackOn -and $trackRerun -and $trackRerun.Checked){
        $r=[System.Windows.Forms.MessageBox]::Show("A Rerun-recording follow session is still running. The newest .rrd is still being written (its tail is unflushed) -- pulling now yields a TRUNCATED snapshot. Stop the session first for a complete recording. Pull anyway?",'Recording in progress',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -ne [System.Windows.Forms.DialogResult]::OK){ return }
    }
    $p=Pull-RerunRecording $ip; if($p){ Open-RerunRecording $p }
})

# --- big annotated video --------------------------------------------------
$trackPic=New-Object System.Windows.Forms.PictureBox; $trackPic.Dock='Fill'; $trackPic.BackColor=$bg; $trackPic.SizeMode='Zoom'

# --- big state badge ------------------------------------------------------
$trackBadge=New-Object System.Windows.Forms.Label; $trackBadge.Text='IDLE'; $trackBadge.Dock='Fill'; $trackBadge.TextAlign='MiddleCenter'; $trackBadge.ForeColor='White'; $trackBadge.BackColor=$chipBg; $trackBadge.Font=$fontHero

# --- small status-transition log ------------------------------------------
$trackLog=New-Object System.Windows.Forms.RichTextBox; $trackLog.ReadOnly=$true; $trackLog.Dock='Fill'; $trackLog.Font=$mono; Set-K1Log $trackLog

$trackLayout.Controls.Add($grpTrackCtl,0,0); $trackLayout.Controls.Add($trackPic,0,1); $trackLayout.Controls.Add($trackBadge,0,2); $trackLayout.Controls.Add($trackLog,0,3)
$tabTrack.Controls.Add($trackLayout)

# ============================================================================
#  TAB 7 - LOCAL MAP  (interactive 3D short-horizon occupancy — robot-local)
#  Domains = separate environments; each follow/capture run merges into the active domain.
#  Primary UX: WebView2 (or WebBrowser fallback) hosting desktop/localmap-viewer/
#  Persistence: desktop/localmap-data/domains/<id>/{manifest,occupancy}.json
#  Advanced: Import .stcm still available for legacy Aurora static renders.
# ============================================================================
$script:LocalMapViewerDir = Join-Path $SCRIPT_DIR 'localmap-viewer'
$script:LocalMapIndex     = Join-Path $script:LocalMapViewerDir 'index.html'
$script:LocalMapFeed      = Join-Path $script:LocalMapViewerDir 'feed.json'
$script:LocalMapSample    = Join-Path $script:LocalMapViewerDir 'sample.json'
$script:LocalMapDataDir   = Join-Path $SCRIPT_DIR 'localmap-data'
$script:LocalMapDomainsDir= Join-Path $script:LocalMapDataDir 'domains'
$script:LocalMapDomainsIndex = Join-Path $script:LocalMapDataDir 'domains.json'
$script:MapHostMode       = 'none'   # webview2 | webbrowser | external
$script:MapWebView        = $null
$script:MapBrowser        = $null
$script:ActiveDomainId    = 'warehouse-bay-a'

$mapLayout=New-Object System.Windows.Forms.TableLayoutPanel
$mapLayout.Dock='Fill'; $mapLayout.ColumnCount=1; $mapLayout.RowCount=2; $mapLayout.Padding='12,8,12,8'; $mapLayout.BackColor=$bg
[void]$mapLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,108)))
[void]$mapLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))

$mapBar=New-Object System.Windows.Forms.Panel; $mapBar.Dock='Fill'; $mapBar.BackColor=$panelBg
$lblIpM=New-Object System.Windows.Forms.Label; $lblIpM.Text='IP:'; $lblIpM.AutoSize=$true; $lblIpM.Location='12,14'; Set-K1Label $lblIpM 'muted'; $mapBar.Controls.Add($lblIpM)
$ipMap=New-Object System.Windows.Forms.TextBox; $ipMap.Size='120,24'; $ipMap.Location='38,10'; $ipMap.Font=$mono; $ipMap.Text=$(if($script:RobotIP){$script:RobotIP}else{$K1_DEFAULT_IP}); Set-K1Field $ipMap; $mapBar.Controls.Add($ipMap)

$btnMapRefresh=New-Object System.Windows.Forms.Button; $btnMapRefresh.Text='Refresh'; $btnMapRefresh.Size='88,30'; $btnMapRefresh.Location='170,8'; Set-K1PrimaryButton $btnMapRefresh; $mapBar.Controls.Add($btnMapRefresh)
$btnMapSample=New-Object System.Windows.Forms.Button; $btnMapSample.Text='Load sample'; $btnMapSample.Size='108,30'; $btnMapSample.Location='266,8'; Set-K1GhostButton $btnMapSample; $mapBar.Controls.Add($btnMapSample)
$btnMapClear=New-Object System.Windows.Forms.Button; $btnMapClear.Text='Clear'; $btnMapClear.Size='72,30'; $btnMapClear.Location='382,8'; Set-K1GhostButton $btnMapClear; $mapBar.Controls.Add($btnMapClear)
$btnMapReset=New-Object System.Windows.Forms.Button; $btnMapReset.Text='Reset view'; $btnMapReset.Size='96,30'; $btnMapReset.Location='462,8'; Set-K1GhostButton $btnMapReset; $mapBar.Controls.Add($btnMapReset)
$chkMapPose=New-Object System.Windows.Forms.CheckBox; $chkMapPose.Text='Show robot pose'; $chkMapPose.AutoSize=$true; $chkMapPose.Location='570,14'; Set-K1Check $chkMapPose 'accent'; $chkMapPose.Checked=$true; $chkMapPose.BackColor=$panelBg; $mapBar.Controls.Add($chkMapPose)
$chkMapFollow=New-Object System.Windows.Forms.CheckBox; $chkMapFollow.Text='Follow pose'; $chkMapFollow.AutoSize=$true; $chkMapFollow.Location='710,14'; Set-K1Check $chkMapFollow; $chkMapFollow.Checked=$true; $chkMapFollow.BackColor=$panelBg; $mapBar.Controls.Add($chkMapFollow)
$btnMapOpen=New-Object System.Windows.Forms.Button; $btnMapOpen.Text='Open in browser'; $btnMapOpen.Size='120,30'; $btnMapOpen.Location='830,8'; Set-K1GhostButton $btnMapOpen; $mapBar.Controls.Add($btnMapOpen)

$lblDomain=New-Object System.Windows.Forms.Label; $lblDomain.Text='Domain:'; $lblDomain.AutoSize=$true; $lblDomain.Location='12,48'; Set-K1Label $lblDomain 'muted'; $mapBar.Controls.Add($lblDomain)
$cmbDomain=New-Object System.Windows.Forms.ComboBox; $cmbDomain.DropDownStyle='DropDownList'; $cmbDomain.Size='220,24'; $cmbDomain.Location='72,44'; $cmbDomain.Font=$font; $cmbDomain.FlatStyle='Flat'; $cmbDomain.BackColor=$surface2; $cmbDomain.ForeColor=$text; $mapBar.Controls.Add($cmbDomain)
$btnDomainNew=New-Object System.Windows.Forms.Button; $btnDomainNew.Text='+ New domain'; $btnDomainNew.Size='118,28'; $btnDomainNew.Location='282,42'; Set-K1GhostButton $btnDomainNew; $mapBar.Controls.Add($btnDomainNew)
$btnDomainImport=New-Object System.Windows.Forms.Button; $btnDomainImport.Text='Import last run'; $btnDomainImport.Size='128,28'; $btnDomainImport.Location='408,42'; Set-K1PrimaryButton $btnDomainImport; $mapBar.Controls.Add($btnDomainImport)
# Advanced: legacy Aurora .stcm static render (kept off the primary chrome)
$btnMapRender=New-Object System.Windows.Forms.Button; $btnMapRender.Text='Import .stcm'; $btnMapRender.Size='100,26'; $btnMapRender.Location='548,44'; Set-K1GhostButton $btnMapRender; $mapBar.Controls.Add($btnMapRender)
$mapInfo=New-Object System.Windows.Forms.Label; $mapInfo.AutoSize=$false; $mapInfo.Size='980,26'; $mapInfo.Location='12,76'; $mapInfo.TextAlign='MiddleLeft'
$mapInfo.Text='Local Map — domains accumulate occupancy per environment. Switch chips in the 3D view or use Domain above.'
$mapInfo.ForeColor=$muted; $mapInfo.BackColor=$panelBg; $mapBar.Controls.Add($mapInfo)

$mapHost=New-Object System.Windows.Forms.Panel; $mapHost.Dock='Fill'; $mapHost.BackColor=$bg
# Compat: old PictureBox kept (hidden) so any leftover .stcm render path can still show a PNG.
$mapPic=New-Object System.Windows.Forms.PictureBox; $mapPic.Dock='Fill'; $mapPic.SizeMode='Zoom'; $mapPic.BackColor=$bg; $mapPic.Visible=$false
$mapHost.Controls.Add($mapPic)

function Invoke-LocalMapJs([string]$js){
    try{
        if($script:MapHostMode -eq 'webview2' -and $script:MapWebView){
            $script:MapWebView.CoreWebView2.ExecuteScriptAsync($js) | Out-Null
            return $true
        }
        if($script:MapHostMode -eq 'webbrowser' -and $script:MapBrowser -and $script:MapBrowser.Document){
            $script:MapBrowser.Document.InvokeScript('eval', @($js)) | Out-Null
            return $true
        }
    }catch{}
    return $false
}

function Get-LocalMapDomainDir([string]$id){
    return (Join-Path $script:LocalMapDomainsDir $id)
}

function Read-LocalMapDomainsIndex{
    if(-not (Test-Path $script:LocalMapDomainsIndex)){ return $null }
    try{ return (Get-Content -Raw -Path $script:LocalMapDomainsIndex | ConvertFrom-Json) }catch{ return $null }
}

function Write-LocalMapDomainsIndex($index){
    New-Item -ItemType Directory -Force -Path $script:LocalMapDataDir | Out-Null
    $index | ConvertTo-Json -Depth 6 | Set-Content -Path $script:LocalMapDomainsIndex -Encoding UTF8
}

function Sync-LocalMapFeedFromDomain([string]$id){
    if(-not $id){ return $false }
    $occ = Join-Path (Get-LocalMapDomainDir $id) 'occupancy.json'
    if(-not (Test-Path $occ)){ return $false }
    try{
        $raw = Get-Content -Raw -Path $occ
        # Ensure domain_id stamped for viewer feed filter
        if($raw -notmatch '"domain_id"'){
            $obj = $raw | ConvertFrom-Json
            $obj | Add-Member -NotePropertyName domain_id -NotePropertyValue $id -Force
            $raw = ($obj | ConvertTo-Json -Depth 8)
        }
        Set-Content -Path $script:LocalMapFeed -Value $raw -Encoding UTF8
        return $true
    }catch{ return $false }
}

function Update-LocalMapDomainCombo{
    $idx = Read-LocalMapDomainsIndex
    $script:MapDomainComboQuiet = $true
    try{
        $cmbDomain.Items.Clear()
        if(-not $idx -or -not $idx.domains){ return }
        $script:ActiveDomainId = [string]$idx.active
        foreach($d in $idx.domains){
            $label = ('{0}  ·  {1} cells  ·  {2} runs' -f $d.name, $d.cell_count, $d.run_count)
            [void]$cmbDomain.Items.Add($label)
            if([string]$d.id -eq $script:ActiveDomainId){ $cmbDomain.SelectedIndex = $cmbDomain.Items.Count - 1 }
        }
        if($cmbDomain.SelectedIndex -lt 0 -and $cmbDomain.Items.Count -gt 0){ $cmbDomain.SelectedIndex = 0 }
    } finally {
        $script:MapDomainComboQuiet = $false
    }
}

function Ensure-LocalMapDomains{
    New-Item -ItemType Directory -Force -Path $script:LocalMapDomainsDir | Out-Null
    $idx = Read-LocalMapDomainsIndex
    if($idx -and $idx.domains -and $idx.domains.Count -gt 0){
        $script:ActiveDomainId = [string]$idx.active
        if(-not $script:ActiveDomainId){ $script:ActiveDomainId = [string]$idx.domains[0].id }
        Sync-LocalMapFeedFromDomain $script:ActiveDomainId | Out-Null
        Update-LocalMapDomainCombo
        return
    }
    # Seed three demo domains if the data pack is missing
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $seeds = @(
        @{ id='assembly-factory'; name='Assembly Factory'; runs=6 },
        @{ id='warehouse-bay-a'; name='Warehouse Bay A'; runs=7 },
        @{ id='distribution-hub'; name='Distribution Hub'; runs=6 }
    )
    $domains = @()
    foreach($s in $seeds){
        $dir = Get-LocalMapDomainDir $s.id
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $occPath = Join-Path $dir 'occupancy.json'
        # Never copy sample.json into seeded domains (legacy sample was Kitchen occupancy).
        if(-not (Test-Path $occPath)){
            $emptyOcc = @{ domain_id=$s.id; res_m=0.08; range_m=3.5; pose=@{x=0;y=0;yaw=0}; trail=@(); cells=@() }
            ($emptyOcc | ConvertTo-Json -Depth 6) | Set-Content -Path $occPath -Encoding UTF8
        }
        $cells = 0
        try{ $cells = ((Get-Content -Raw $occPath | ConvertFrom-Json).cells | Measure-Object).Count }catch{}
        $man = @{ id=$s.id; name=$s.name; created=$now; updated=$now; run_count=$s.runs; cell_count=$cells; notes='seeded' }
        ($man | ConvertTo-Json -Depth 4) | Set-Content -Path (Join-Path $dir 'manifest.json') -Encoding UTF8
        $domains += @{ id=$s.id; name=$s.name; updated=$now; run_count=$s.runs; cell_count=$cells }
    }
    $idx = @{ active='warehouse-bay-a'; domains=$domains }
    Write-LocalMapDomainsIndex $idx
    $script:ActiveDomainId = 'warehouse-bay-a'
    Sync-LocalMapFeedFromDomain 'warehouse-bay-a' | Out-Null
    Update-LocalMapDomainCombo
}

function Get-LocalMapDomainIdByComboIndex([int]$i){
    $idx = Read-LocalMapDomainsIndex
    if(-not $idx -or -not $idx.domains -or $i -lt 0 -or $i -ge $idx.domains.Count){ return $null }
    return [string]$idx.domains[$i].id
}

function Switch-LocalMapDomain([string]$id){
    if(-not $id){ return }
    $dir = Get-LocalMapDomainDir $id
    if(-not (Test-Path $dir)){ $mapInfo.Text = ("Domain folder missing: {0}" -f $id); return }
    $idx = Read-LocalMapDomainsIndex
    if($idx){ $idx.active = $id; Write-LocalMapDomainsIndex $idx }
    $script:ActiveDomainId = $id
    Sync-LocalMapFeedFromDomain $id | Out-Null
    Update-LocalMapDomainCombo
    [void](Invoke-LocalMapJs ("window.k1LocalMap && window.k1LocalMap.switchDomain('{0}')" -f $id.Replace("'","\'")))
    $meta = $null
    try{ $meta = Get-Content -Raw (Join-Path $dir 'manifest.json') | ConvertFrom-Json }catch{}
    $name = if($meta){$meta.name}else{$id}
    $mapInfo.Text = ("Active domain: {0}  ·  switch chips in viewer or Domain combo · runs merge into this map" -f $name)
}

function New-LocalMapDomain([string]$name){
    $n = ($name -as [string]).Trim()
    if(-not $n){ return $null }
    $id = ($n.ToLower() -replace '[^a-z0-9]+','-').Trim('-')
    if(-not $id){ $id = ('domain-{0}' -f [guid]::NewGuid().ToString('N').Substring(0,8)) }
    $dir = Get-LocalMapDomainDir $id
    if(Test-Path $dir){
        $mapInfo.Text = ("Domain already exists: {0}" -f $id)
        Switch-LocalMapDomain $id
        return $id
    }
    # Always start EMPTY: no occupancy cells, no asset instances, no copied seed props.
    # Content arrives via Import last run / Rerun placer / autofill / ?demo_assets=1.
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $occ = @{ domain_id=$id; res_m=0.08; range_m=3.5; pose=@{x=0;y=0;yaw=0}; trail=@(); cells=@() }
    ($occ | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $dir 'occupancy.json') -Encoding UTF8
    $inst = @{ domain_id=$id; updated=$now; notes='operator-created empty domain'; instances=@() }
    ($inst | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $dir 'instances.json') -Encoding UTF8
    $man = @{ id=$id; name=$n; created=$now; updated=$now; run_count=0; cell_count=0; notes='operator-created-empty' }
    ($man | ConvertTo-Json -Depth 4) | Set-Content -Path (Join-Path $dir 'manifest.json') -Encoding UTF8
    $idx = Read-LocalMapDomainsIndex
    if(-not $idx){ $idx = @{ active=$id; domains=@() } }
    if(-not $idx.domains){ $idx.domains = @() }
    $list = @($idx.domains) + @(@{ id=$id; name=$n; updated=$now; run_count=0; cell_count=0 })
    $idx.domains = $list
    $idx.active = $id
    Write-LocalMapDomainsIndex $idx
    Switch-LocalMapDomain $id
    [void](Invoke-LocalMapJs ("window.k1LocalMap && window.k1LocalMap.refreshDomains && window.k1LocalMap.refreshDomains({ forceId: '{0}' })" -f $id.Replace("'","\'")))
    $mapInfo.Text = ("Created empty domain '{0}' — Import last run / Rerun placer / autofill to add content." -f $n)
    return $id
}

function Merge-LocalMapOccupancy($base, $incoming, [string]$domainId){
    $res = 0.08
    if($incoming.res_m){ $res = [double]$incoming.res_m }
    elseif($base.res_m){ $res = [double]$base.res_m }
    $map = @{}
    foreach($src in @($base,$incoming)){
        if(-not $src -or -not $src.cells){ continue }
        foreach($c in $src.cells){
            $kx = [int][Math]::Round(([double]$c.x) / $res)
            $ky = [int][Math]::Round(([double]$c.y) / $res)
            $k = '{0}:{1}' -f $kx,$ky
            $hits = 1; if($c.hits){ $hits = [int]$c.hits }
            if($map.ContainsKey($k)){
                $map[$k].hits = [int]$map[$k].hits + $hits
                $map[$k].x = ([double]$map[$k].x + [double]$c.x) / 2.0
                $map[$k].y = ([double]$map[$k].y + [double]$c.y) / 2.0
            } else {
                $map[$k] = @{ x=[double]$c.x; y=[double]$c.y; hits=$hits }
            }
        }
    }
    $cells = @($map.Values)
    $range = 3.5
    if($base.range_m -and [double]$base.range_m -gt $range){ $range = [double]$base.range_m }
    if($incoming.range_m -and [double]$incoming.range_m -gt $range){ $range = [double]$incoming.range_m }
    $pose = @{ x=0; y=0; yaw=0 }
    if($incoming.pose){ $pose = $incoming.pose }
    elseif($base.pose){ $pose = $base.pose }
    return @{ domain_id=$domainId; res_m=$res; range_m=$range; pose=$pose; cells=$cells }
}

function Import-LocalMapRunIntoActive([string]$ip){
    $id = $script:ActiveDomainId
    if(-not $id){ $mapInfo.Text='Select or create a domain first.'; return }
    $dir = Get-LocalMapDomainDir $id
    $occPath = Join-Path $dir 'occupancy.json'
    $base = $null
    if(Test-Path $occPath){ try{ $base = Get-Content -Raw $occPath | ConvertFrom-Json }catch{} }
    $incoming = $null
    $source = 'sample'
    if($ip){
        $remoteCandidates = @(
            '/tmp/k1_localmap.json',
            '/home/booster/localmap/latest.json',
            '/tmp/k1_localmap_feed.json'
        )
        foreach($remote in $remoteCandidates){
            try{
                $tmp = Join-Path $env:TEMP ('k1_lm_merge_{0}.json' -f ([guid]::NewGuid().ToString('N')))
                $psi = New-Object System.Diagnostics.ProcessStartInfo
                $psi.FileName = 'scp'; $psi.Arguments = ("-o BatchMode=yes -o ConnectTimeout=4 {0}@{1}:{2} `"{3}`"" -f $K1_SSH_USER,$ip,$remote,$tmp)
                $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardOutput = $true
                $p = [System.Diagnostics.Process]::Start($psi)
                if(-not $p.WaitForExit(6000)){ try{$p.Kill()}catch{}; continue }
                if($p.ExitCode -eq 0 -and (Test-Path $tmp) -and ((Get-Item $tmp).Length -gt 8)){
                    $incoming = Get-Content -Raw $tmp | ConvertFrom-Json
                    $source = $remote
                    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
                    break
                }
                Remove-Item -Force $tmp -ErrorAction SilentlyContinue
            }catch{}
        }
    }
    if(-not $incoming){
        # Fail closed: do not merge sample.json (would pollute empty / wrong domains).
        $mapInfo.Text = 'No run dump on robot — connect K1 or provide a localmap JSON to merge. Sample merge disabled.'
        return
    }
    $merged = Merge-LocalMapOccupancy $base $incoming $id
    ($merged | ConvertTo-Json -Depth 8) | Set-Content -Path $occPath -Encoding UTF8
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $manPath = Join-Path $dir 'manifest.json'
    $man = $null
    if(Test-Path $manPath){ try{ $man = Get-Content -Raw $manPath | ConvertFrom-Json }catch{} }
    if(-not $man){ $man = @{ id=$id; name=$id; created=$now } }
    $man.updated = $now
    $man.cell_count = @($merged.cells).Count
    $rc = 0; if($man.run_count){ $rc = [int]$man.run_count }
    $man.run_count = $rc + 1
    ($man | ConvertTo-Json -Depth 4) | Set-Content -Path $manPath -Encoding UTF8
    $idx = Read-LocalMapDomainsIndex
    if($idx -and $idx.domains){
        foreach($d in $idx.domains){
            if([string]$d.id -eq $id){
                $d.updated = $now
                $d.cell_count = $man.cell_count
                $d.run_count = $man.run_count
            }
        }
        $idx.active = $id
        Write-LocalMapDomainsIndex $idx
    }
    Sync-LocalMapFeedFromDomain $id | Out-Null
    Update-LocalMapDomainCombo
    [void](Invoke-LocalMapJs "window.k1LocalMap && window.k1LocalMap.refreshDomains()")
    $mapInfo.Text = ("Merged into '{0}' from {1} — {2} cells · {3} runs (accumulate, not replace)." -f $man.name,$source,$man.cell_count,$man.run_count)
}

function Write-LocalMapFeedFromSample{
    if(Sync-LocalMapFeedFromDomain $script:ActiveDomainId){ return $true }
    if(Test-Path $script:LocalMapSample){
        Copy-Item -Force $script:LocalMapSample $script:LocalMapFeed
        return $true
    }
    return $false
}

function Refresh-LocalMapFromRobot([string]$ip){
    # Pull dump and MERGE into the active domain (accumulate geometry over runs).
    $mapInfo.Text = ("Refreshing → merge into domain '{0}' from {1}..." -f $script:ActiveDomainId,$ip)
    Import-LocalMapRunIntoActive $ip
}

function Initialize-LocalMapHost{
    if(-not (Test-Path $script:LocalMapIndex)){
        $mapInfo.Text = 'localmap-viewer/index.html missing — open desktop/README-UI.md'
        return
    }
    Ensure-LocalMapDomains
    Write-LocalMapFeedFromSample | Out-Null

    # Prefer WebView2 (Chromium). DLL may sit next to the app or in the NuGet cache.
    $wvLoaded = $false
    try{
        $wvAsm = [AppDomain]::CurrentDomain.GetAssemblies() | Where-Object { $_.GetName().Name -eq 'Microsoft.Web.WebView2.WinForms' } | Select-Object -First 1
        if(-not $wvAsm){
            $searchRoots = @($SCRIPT_DIR, (Join-Path $env:USERPROFILE '.nuget\packages\microsoft.web.webview2'))
            foreach($root in $searchRoots){
                if(-not (Test-Path $root)){ continue }
                $hit = Get-ChildItem -Path $root -Recurse -Filter 'Microsoft.Web.WebView2.WinForms.dll' -ErrorAction SilentlyContinue | Select-Object -First 1
                if($hit){
                    Add-Type -Path $hit.FullName
                    $core = Join-Path $hit.DirectoryName 'Microsoft.Web.WebView2.Core.dll'
                    if(Test-Path $core){ Add-Type -Path $core }
                    $wvLoaded = $true
                    break
                }
            }
        } else { $wvLoaded = $true }
        if($wvLoaded -or $wvAsm){
            $wv = New-Object Microsoft.Web.WebView2.WinForms.WebView2
            $wv.Dock = 'Fill'
            $mapHost.Controls.Add($wv)
            $wv.BringToFront()
            $script:MapWebView = $wv
            $script:MapHostMode = 'webview2'
            [void]$wv.EnsureCoreWebView2Async($null)
            $wv.Add_CoreWebView2InitializationCompleted({
                param($s,$e)
                if($e.IsSuccess){
                    $s.CoreWebView2.Navigate(([Uri]$script:LocalMapIndex).AbsoluteUri)
                    $mapInfo.Text = ('Local Map 3D · domain {0} (WebView2) — chips switch areas · Import last run merges' -f $script:ActiveDomainId)
                } else {
                    $mapInfo.Text = ('WebView2 init failed: {0} — use Open in browser' -f $e.InitializationException.Message)
                }
            })
            return
        }
    }catch{
        # fall through to WebBrowser
    }

    try{
        $wb = New-Object System.Windows.Forms.WebBrowser
        $wb.Dock = 'Fill'; $wb.ScriptErrorsSuppressed = $true
        $mapHost.Controls.Add($wb); $wb.BringToFront()
        $script:MapBrowser = $wb
        $script:MapHostMode = 'webbrowser'
        $wb.Navigate(([Uri]$script:LocalMapIndex).AbsoluteUri)
        $mapInfo.Text = 'Local Map hosted in WebBrowser (IE engine). Prefer WebView2 or Open in browser for full Three.js.'
        return
    }catch{}

    $script:MapHostMode = 'external'
    $fallback = New-Object System.Windows.Forms.Label
    $fallback.Dock='Fill'; $fallback.TextAlign='MiddleCenter'; $fallback.Font=$fontHero
    $fallback.ForeColor=$muted; $fallback.BackColor=$bg
    $fallback.Text = "3D viewer ready`r`nClick  Open in browser  — see desktop/README-UI.md"
    $mapHost.Controls.Add($fallback)
    $mapInfo.Text = 'No embedded browser — Open in browser launches the Three.js Local Map viewer.'
}

$btnMapSample.Add_Click({
    Write-LocalMapFeedFromSample | Out-Null
    if(-not (Invoke-LocalMapJs ("window.k1LocalMap && window.k1LocalMap.switchDomain('{0}')" -f $script:ActiveDomainId))){
        if(-not (Invoke-LocalMapJs "window.k1LocalMap && window.k1LocalMap.loadSample()")){
            try{ Start-Process $script:LocalMapIndex }catch{}
        }
    }
    $mapInfo.Text = ('Domain occupancy reloaded ({0}).' -f $script:ActiveDomainId)
})
$btnMapClear.Add_Click({
    [void](Invoke-LocalMapJs "window.k1LocalMap && window.k1LocalMap.clear()")
    $id = $script:ActiveDomainId
    if($id){
        $occ = @{ domain_id=$id; res_m=0.08; range_m=3.5; pose=@{x=0;y=0;yaw=0}; cells=@() }
        $dir = Get-LocalMapDomainDir $id
        if(Test-Path $dir){
            ($occ | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $dir 'occupancy.json') -Encoding UTF8
        }
    }
    try{ if(Test-Path $script:LocalMapFeed){ Set-Content -Path $script:LocalMapFeed -Value '{"res_m":0.08,"range_m":3.5,"pose":{"x":0,"y":0,"yaw":0},"cells":[]}' -Encoding UTF8 } }catch{}
    $mapInfo.Text = ('Cleared active domain ({0}) occupancy (manifest run count kept).' -f $id)
})
$btnMapReset.Add_Click({
    [void](Invoke-LocalMapJs "window.k1LocalMap && window.k1LocalMap.resetView()")
    $mapInfo.Text = 'Camera reset.'
})
$chkMapPose.Add_CheckedChanged({
    $on = if($chkMapPose.Checked){'true'}else{'false'}
    [void](Invoke-LocalMapJs ("window.k1LocalMap && window.k1LocalMap.setShowRobot({0})" -f $on))
})
$chkMapFollow.Add_CheckedChanged({
    $on = if($chkMapFollow.Checked){'true'}else{'false'}
    [void](Invoke-LocalMapJs ("window.k1LocalMap && window.k1LocalMap.setFollowPose({0})" -f $on))
})
$btnMapRefresh.Add_Click({
    $ip = $ipMap.Text.Trim(); if(-not $ip){ $mapInfo.Text='Enter robot IP.'; return }
    $script:RobotIP = $ip
    Refresh-LocalMapFromRobot $ip
})
$btnMapOpen.Add_Click({
    if(Test-Path $script:LocalMapIndex){ Start-Process $script:LocalMapIndex }
    else { $mapInfo.Text = 'Viewer HTML missing.' }
})
$script:MapDomainComboQuiet = $false
$cmbDomain.Add_SelectedIndexChanged({
    if($script:MapDomainComboQuiet){ return }
    $id = Get-LocalMapDomainIdByComboIndex $cmbDomain.SelectedIndex
    if($id -and $id -ne $script:ActiveDomainId){ Switch-LocalMapDomain $id }
})
$btnDomainNew.Add_Click({
    $name = [Microsoft.VisualBasic.Interaction]::InputBox(
        "Name this environment (assembly factory, warehouse bay, distribution hub…).`r`nFollow/capture runs will accumulate into this domain.",
        'New Local Map domain',
        'New area'
    )
    if($name){ [void](New-LocalMapDomain $name) }
})
$btnDomainImport.Add_Click({
    $ip = $ipMap.Text.Trim()
    Import-LocalMapRunIntoActive $ip
})

# Soft-bridge for viewer "+ New domain" when running in WebView2 via polling / host script injection after nav
# Viewer calls window.k1LocalMapHostCreateDomain(json) — define a JS stub that posts back via document title heartbeat if needed.
# For reliability we also re-inject after domain combo changes:
function Inject-LocalMapHostBridge{
    $js = @'
window.k1LocalMapHostCreateDomain = function(payload){
  try {
    var o = (typeof payload === 'string') ? JSON.parse(payload) : payload;
    document.title = 'k1domain:create:' + encodeURIComponent(JSON.stringify(o));
  } catch(e) {}
};
window.k1LocalMapOnDomainChange = function(id){
  document.title = 'k1domain:active:' + encodeURIComponent(id || '');
};
'@
    [void](Invoke-LocalMapJs $js)
}

$mapLayout.Controls.Add($mapBar,0,0); $mapLayout.Controls.Add($mapHost,0,1)
$tabMap.Controls.Add($mapLayout)
# VisualBasic for InputBox (New domain)
try{ Add-Type -AssemblyName Microsoft.VisualBasic }catch{}
Ensure-LocalMapDomains
Initialize-LocalMapHost
# Poll document title for domain create/switch events from the HTML chips
$mapDomainTimer = New-Object System.Windows.Forms.Timer
$mapDomainTimer.Interval = 700
$mapDomainTimer.Add_Tick({
    try{
        Inject-LocalMapHostBridge
        $title = $null
        if($script:MapHostMode -eq 'webview2' -and $script:MapWebView -and $script:MapWebView.CoreWebView2){
            $title = $script:MapWebView.CoreWebView2.DocumentTitle
        } elseif($script:MapHostMode -eq 'webbrowser' -and $script:MapBrowser){
            $title = $script:MapBrowser.DocumentTitle
        }
        if(-not $title){ return }
        if($title.StartsWith('k1domain:create:')){
            $payload = [uri]::UnescapeDataString($title.Substring('k1domain:create:'.Length))
            $obj = $payload | ConvertFrom-Json
            if($obj.name){ [void](New-LocalMapDomain ([string]$obj.name)) }
            [void](Invoke-LocalMapJs "document.title='K1 Local Map'")
        } elseif($title.StartsWith('k1domain:active:')){
            $id = [uri]::UnescapeDataString($title.Substring('k1domain:active:'.Length))
            if($id -and $id -ne $script:ActiveDomainId){
                $idx = Read-LocalMapDomainsIndex
                if($idx){ $idx.active = $id; Write-LocalMapDomainsIndex $idx }
                $script:ActiveDomainId = $id
                Sync-LocalMapFeedFromDomain $id | Out-Null
                $script:MapDomainComboQuiet = $true
                Update-LocalMapDomainCombo
                $script:MapDomainComboQuiet = $false
            }
            [void](Invoke-LocalMapJs "document.title='K1 Local Map'")
        }
    }catch{}
})
$mapDomainTimer.Start()

# Advanced: legacy Aurora .stcm / .vslam / .ply static PNG render (not primary UX)
$btnMapRender.Add_Click({
    $ofd=New-Object System.Windows.Forms.OpenFileDialog
    $ofd.Filter='SLAM maps + clouds (*.stcm;*.vslam;*.ply)|*.stcm;*.vslam;*.ply|All files (*.*)|*.*'
    $ofd.InitialDirectory=[Environment]::GetFolderPath('Desktop')
    if($ofd.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK){ return }
    $map=$ofd.FileName
    $isPly = ([IO.Path]::GetExtension($map).ToLower() -eq '.ply')
    $py=$null
    foreach($cand in @((Join-Path $env:USERPROFILE 'anaconda3\python.exe'),(Join-Path $env:USERPROFILE 'AppData\Local\anaconda3\python.exe'),(Join-Path $env:USERPROFILE 'miniconda3\python.exe'))){ if(Test-Path $cand){ $py=$cand; break } }
    if(-not $py){ try{ $g=(Get-Command python.exe -ErrorAction SilentlyContinue); if($g -and ($g.Source -notmatch 'WindowsApps')){ $py=$g.Source } }catch{} }
    if(-not $py){ $mapInfo.Text='No Python found. The legacy .stcm renderer needs anaconda (numpy + PIL).'; return }
    $rel = if($isPly){'eval\ply_view.py'}else{'eval\stcm_grid.py'}
    $tool=Join-Path $REPO_ROOT $rel
    if(-not (Test-Path $tool)){ $mapInfo.Text=("Renderer not found at {0}" -f $tool); return }
    $png=Join-Path $env:TEMP ('k1_map_{0}.png' -f ([guid]::NewGuid().ToString('N')))
    $mapInfo.Text='Rendering legacy map (large maps take a few seconds)...'; $mapInfo.Refresh()
    try{
        $out = & $py $tool $map '--png' $png 2>&1 | Out-String
        if(Test-Path $png){
            $mapPic.Visible = $true
            if($script:MapWebView){ $script:MapWebView.Visible = $false }
            if($script:MapBrowser){ $script:MapBrowser.Visible = $false }
            $mapPic.BringToFront()
            if($mapPic.Image){ $mapPic.Image.Dispose() }
            $bytes=[IO.File]::ReadAllBytes($png)
            $ms=New-Object System.IO.MemoryStream(,$bytes)
            $mapPic.Image=[System.Drawing.Image]::FromStream($ms)
            $meta=($out -split "`n" | Where-Object { $_ -match '(grid|cloud) .+ m' } | Select-Object -First 1)
            if(-not $meta){ $meta='' }
            $kind = if($isPly){'3D cloud'}else{'2D grid'}
            $mapInfo.Text=("{0}  [{1}]   |   {2}   (legacy Import .stcm — use Load sample for interactive Local Map)" -f (Split-Path $map -Leaf), $kind, $meta.Trim())
        } else {
            $mapInfo.Text=("Render failed: {0}" -f ($out.Trim() -replace "`r?`n",'  '))
        }
    }catch{ $mapInfo.Text=("Render error: {0}" -f $_) }
})

# ============================================================================
#  Log helpers
# ============================================================================
function Add-LogTo($box,[string]$msg,$color){ if(-not $color){$color=[System.Drawing.Color]::Gainsboro}; $box.SelectionStart=$box.TextLength; $box.SelectionColor=$color; $box.AppendText($msg+"`r`n"); $box.ScrollToCaret() }
function Add-Log([string]$m,$c){ Add-LogTo $logBox $m $c }
function Add-Log2([string]$m,$c){ Add-LogTo $logBox2 $m $c }
function Add-LogLive([string]$m,$c){ Add-LogTo $liveLog $m $c }
function Add-LogCtrl([string]$m,$c){ Add-LogTo $ctrlLog $m $c }
function Add-LogFiles([string]$m,$c){ Add-LogTo $previewBox $m $c }
function Add-LogTrack([string]$m,$c){ Add-LogTo $trackLog $m $c }

function Confidence-Color([string]$label){ switch($label){ 'High'{return $green} 'Medium'{return $amber} default{return [System.Drawing.Color]::Gray} } }
function Get-SelectedIP { if($list.SelectedItems.Count -gt 0){ return $list.SelectedItems[0].SubItems[1].Text }; return $null }
function Set-RobotIP([string]$ip){ if(-not $ip){return}; $script:RobotIP=$ip; $ipBox.Text=$ip; $ip2Box.Text=$ip; $ipLive.Text=$ip; $ipCtrl.Text=$ip; if($ipFiles){ $ipFiles.Text=$ip }; if($ipTrack){ $ipTrack.Text=$ip }; if($ipMap){ $ipMap.Text=$ip } }

# ============================================================================
#  Scan control + timer (Discover)
# ============================================================================
$renderedCount=0
function Start-Scan {
    if($sync.Running){return}
    $list.Items.Clear(); $sync.Results.Clear(); $sync.Log.Clear(); $sync.Progress=0; $sync.Total=0
    $sync.Cancel=$false; $sync.Done=$false; $sync.Running=$true; $script:renderedCount=0; $progress.Value=0
    $scanBtn.Enabled=$false; $stopBtn.Enabled=$true; $statusLbl.Text='Scanning...'; Add-Log '--- Starting scan ---' $accent
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open(); $rs.SessionStateProxy.SetVariable('sync',$sync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($scanScript); $sync.PS=$ps; $sync.Handle=$ps.BeginInvoke()
}
function Stop-Scan { $sync.Cancel=$true; $statusLbl.Text='Stopping...' }

# ============================================================================
#  Live view start/stop
# ============================================================================
$liveReader = {
    $stream = $proc.StandardOutput.BaseStream
    $magic = [byte[]](0x4B,0x31,0x46,0x31)
    function ReadExact($s,$n){ $buf=New-Object byte[] $n; $off=0; while($off -lt $n){ $r=$s.Read($buf,$off,$n-$off); if($r -le 0){return $null}; $off+=$r }; return ,$buf }
    $m=0
    try{
        while(-not $liveSync.Stop){
            $b=$stream.ReadByte(); if($b -lt 0){break}
            if($b -eq $magic[$m]){ $m++ } elseif($b -eq $magic[0]){ $m=1 } else { $m=0 }
            if($m -eq 4){
                $m=0
                $sb=$stream.ReadByte(); if($sb -lt 0){break}   # lock status: 0 none / 1 acquiring / 2 LOCKED
                $lenb=ReadExact $stream 4; if($null -eq $lenb){break}
                $len=([int]$lenb[0] -shl 24) -bor ([int]$lenb[1] -shl 16) -bor ([int]$lenb[2] -shl 8) -bor [int]$lenb[3]
                if($len -le 0 -or $len -gt 8000000){ continue }
                $jpg=ReadExact $stream $len; if($null -eq $jpg){break}
                $liveSync.Jpeg=$jpg; $liveSync.Lock=$sb; $liveSync.Seq=$liveSync.Seq+1; $liveSync.Frames=$liveSync.Frames+1
            }
        }
    } catch { $liveSync.Err=$_.Exception.Message }
    $liveSync.Done=$true
}
function Start-Live {
    if($script:LiveOn){return}
    $ip=$ipLive.Text.Trim(); if(-not $ip){ Add-LogLive 'Enter the robot IP first.' $amber; return }
    # SINGLE CAMERA CONSUMER: the Tracker stream and the Control-tab Follow also
    # decode the head camera, so stop them first -- the K1 never streams the camera
    # twice. (Stops an active follow safely: its cleanup returns the robot to PREP.)
    if($script:TrackOn){ Add-LogLive 'Stopping Tracker (single camera consumer)...' $amber; Stop-Tracker }
    if($script:FollowOn){ Add-LogLive 'Stopping Control-tab Follow (single camera consumer)...' $amber; Stop-Follow }
    $script:RobotIP=$ip
    Open-OperatorSession 'live'   # the Live view owns the link too: hold the Auto-Tune loop off it (shared contract)
    Add-LogLive ("Deploying camera streamer to {0} ..." -f $ip) $accent
    if(-not (Deploy-RobotFiles $ip)){ Add-LogLive 'Deploy failed (helper scripts missing).' $red; Close-OperatorSession 'live'; return }
    # clear any orphaned streamer from a previous session (a killed ssh can leave the remote python running)
    try{ $kp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -f stream_cam.py')) -NoNewWindow -PassThru; $null=$kp.WaitForExit(6000) }catch{}
    $liveSync.Stop=$false; $liveSync.Done=$false; $liveSync.Jpeg=$null; $liveSync.Seq=0; $liveSync.Frames=0; $liveSync.Err=''
    $script:lastSeq=-1; $script:liveFpsFrames=0; $script:lastLock=-1; $liveSync.Lock=0
    $lockBadge.Text='  MARKER: --  '; $lockBadge.BackColor=[System.Drawing.Color]::Gray
    $topic=$topicCombo.SelectedItem; $fps=$fpsCombo.SelectedItem; $q=$qCombo.SelectedItem
    $remote="bash /home/booster/run_stream.sh $topic $fps $q"
    $argStr=(Get-SshOptString)+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogLive 'Failed to start ssh.' $red; Close-OperatorSession 'live'; return }
    $script:LiveProc=$proc
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('liveSync',$liveSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($liveReader); [void]$ps.BeginInvoke()
    $script:LivePS=$ps; $script:LiveRS=$rs; $script:LiveOn=$true
    $startLiveBtn.Enabled=$false; $stopLiveBtn.Enabled=$true
    $liveStatus.Text="Connecting to $ip ($topic)... first frames take a few seconds."
    Add-LogLive ("Streaming {0} @ {1}fps q{2}. If no frames appear, the camera is idle (try 'Enable cam')." -f $topic,$fps,$q)
    $statusLbl.Text="Live view: connecting to $ip"
}
function Stop-Live {
    if(-not $script:LiveOn){return}
    $liveSync.Stop=$true
    try{ if($script:LiveProc -and -not $script:LiveProc.HasExited){ $script:LiveProc.Kill() } }catch{}
    try{ if($script:LivePS){ $script:LivePS.Dispose() } }catch{}
    try{ if($script:LiveRS){ $script:LiveRS.Close() } }catch{}
    $script:LiveOn=$false; $startLiveBtn.Enabled=$true; $stopLiveBtn.Enabled=$false
    $script:lastLock=-1; $lockBadge.Text='  MARKER: --  '; $lockBadge.BackColor=[System.Drawing.Color]::Gray
    $liveStatus.Text='Stopped.'; Add-LogLive 'Live view stopped.' $amber
    Close-OperatorSession 'live'
}
function Enable-Camera {
    $ip=$ipLive.Text.Trim(); if(-not $ip){return}
    Add-LogLive 'Attempting camera enable (X5CameraClient NormalEnable, beta)...' $accent
    $remote='bash -lc "source /opt/ros/humble/setup.bash 2>/dev/null; /home/booster/enable_camera 127.0.0.1 2"'
    $argStr=(Get-SshOptString)+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $p=New-Object System.Diagnostics.Process; $p.StartInfo=$psi; [void]$p.Start()
    $o=$p.StandardOutput.ReadToEnd(); $null=$p.WaitForExit(9000); try{ if(-not $p.HasExited){$p.Kill()} }catch{}
    Add-LogLive ("enable_camera: "+($o -replace "`r?`n",' | ')) ([System.Drawing.Color]::Gainsboro)
    Add-LogLive 'If it shows ChangeMode -> 100, the camera RPC service is not answering in this robot state.' $amber
}

# ============================================================================
#  Tracker - marker-seeded markerless person-follow (annotated stream cockpit)
#  Transport cloned from Start-Live/Stop-Live: ssh WITHOUT -tt (a PTY corrupts
#  the binary JPEG stream), RedirectStandardOutput, kill-to-stop. The node traps
#  SIGHUP on the killed ssh -> clean stop + ChangeMode(kPrepare); the bridge also
#  safes on its own stdin-EOF. Plus an independent pkill on stop (belt & braces).
# ============================================================================
$trackReader = {
    $stream = $proc.StandardOutput.BaseStream
    $magic = [byte[]](0x4B,0x31,0x46,0x31)
    function ReadExact($s,$n){ $buf=New-Object byte[] $n; $off=0; while($off -lt $n){ $r=$s.Read($buf,$off,$n-$off); if($r -le 0){return $null}; $off+=$r }; return ,$buf }
    $m=0
    try{
        while(-not $trackSync.Stop){
            $b=$stream.ReadByte(); if($b -lt 0){break}
            if($b -eq $magic[$m]){ $m++ } elseif($b -eq $magic[0]){ $m=1 } else { $m=0 }
            if($m -eq 4){
                $m=0
                $sb=$stream.ReadByte(); if($sb -lt 0){break}   # status: 0 search /1 acquire /2 follow /3 lost
                $lenb=ReadExact $stream 4; if($null -eq $lenb){break}
                $len=([int]$lenb[0] -shl 24) -bor ([int]$lenb[1] -shl 16) -bor ([int]$lenb[2] -shl 8) -bor [int]$lenb[3]
                if($len -le 0 -or $len -gt 8000000){ continue }
                $jpg=ReadExact $stream $len; if($null -eq $jpg){break}
                $trackSync.Jpeg=$jpg; $trackSync.Lock=$sb; $trackSync.Seq=$trackSync.Seq+1; $trackSync.Frames=$trackSync.Frames+1
            }
        }
    } catch { $trackSync.Err=$_.Exception.Message }
    $trackSync.Done=$true
}

# Slider->float arg conversion: Distance ticks are 0.1 m (value/10), Speed ticks are 0.01 m/s (value/100).
function Get-TrackStandoff { return ([math]::Round($distTrack.Value/10.0,2)).ToString([System.Globalization.CultureInfo]::InvariantCulture) }
function Get-TrackVxMax    { return ([math]::Round($spdTrack.Value/100.0,2)).ToString([System.Globalization.CultureInfo]::InvariantCulture) }
# Build the extra follow-mode flags from the Tracker 'Perception' row. Empty string
# (all defaults: global appearance, no coast/reacquire/fence) reproduces today's launch.
function Get-TrackExtraArgs {
    $a=@()
    $app = if($trackApp -and $trackApp.SelectedItem){ [string]$trackApp.SelectedItem } else { 'global' }
    if($app -eq 'striped'){ $a += '--appearance striped' }
    elseif($app -eq 'osnet'){ $a += ('--appearance osnet --reid-engine {0}' -f $script:ReidEngine) }
    if($trackCoast -and $trackCoast.Checked){ $a += '--coast-frames 8' }
    if($trackReacq -and $trackReacq.Checked){ $a += '--auto-reacquire' }   # passive/audit-only unless armed below
    # DANGER: actually re-lock (not audit-only). OSNet only (node refuses it on weak backends).
    # Arming is meaningless without the vote running, so ensure --auto-reacquire is present too.
    if($trackArmReloc -and $trackArmReloc.Checked -and $app -eq 'osnet'){
        if(-not ($trackReacq -and $trackReacq.Checked)){ $a += '--auto-reacquire' }
        # STRICTER ARMED RE-LOCK (2026-09-03, operator request: must not re-lock onto ANOTHER
        # person after a loss). Armed re-lock has DRIVE authority -- a wrong re-lock walks the
        # robot at a stranger -- so it is gated harder than the audit vote:
        #   --reloc-arm-margin 0.35 (was 0.30): the winner must beat the RUNNER-UP by this much.
        #     This is the real anti-wrong-person guard: a look-alike can score high in absolute
        #     terms, but should not out-score you by a wide gap. Only bites in multi-person
        #     scenes, which is exactly the risk case. NOT set higher: at 0.45 a DECISIVE win
        #     (g=0.85 vs runner-up 0.50, gap 0.35) is refused too. 0.35 ADMITS that clear win while
        #     still refusing a look-alike gap (g=0.90 vs 0.62 = 0.28). Set higher and armed re-lock
        #     effectively never fire and push every loss back to manual re-seeding.
        #   --reloc-arm-streak 12 (was 8): consecutive confirming frames before it may re-lock.
        #   --reloc-floor 0.68 (osnet-resolved default 0.55): raises the absolute bar. Field
        #     relocks were observed at g=0.63/0.72/0.77 -- 0.68 refuses the weakest of those.
        # Ladder invariants hold: reloc_floor > anchor_floor 0.35; arm_margin >= reloc_margin
        # 0.20; arm_streak >= reloc_streak 5. Cost of strictness: more manual re-seeding after
        # a hard loss, which is the SAFE direction to fail.
        $a += '--arm-reacquire --reloc-arm-margin 0.35 --reloc-arm-streak 12 --reloc-floor 0.68'
    }
    if($trackFence -and $trackFence.Checked){ $a += '--max-follow-range 4.0' }
    # Gap steering. audit => --sector-audit audit as well, so the SECTOR lines that explain each
    # decision are in the same log. on => sector audit stays off (the GAP-STEER line already says
    # what it did) to keep the 1 Hz logging down while it is actually steering.
    if($trackGap -and $trackGap.SelectedItem -and [string]$trackGap.SelectedItem -ne 'off'){
        $g = [string]$trackGap.SelectedItem
        if($g -eq 'audit'){ $a += '--gap-steer audit --sector-audit audit' }
        # WIDER BERTH (2026-09-03): widen by turning EARLIER, not harder. Engaging late (0.98 m)
        # forces a sharp deviation; engaging at ~1.22 m gives 0.24 m more runway for the same
        # lateral clearance at a SMALLER peak bearing -- and peak bearing is exactly what costs
        # the lock (today's three losses were at -27, -32 and +31 deg).
        #   trigger-frac 0.35 -> 0.65   engage below ~1.22 m instead of ~0.98 m
        #   rate 0.20 -> 0.24           slightly firmer arc (settles ~30 deg at relax 0.5)
        #   max-bearing 35 -> 40        headroom for that 30 deg, still 13 deg inside the FOV edge
        # NO --gap-steer-trigger-frac HERE. The 0.65 override predated the node's 2026-09-04 raise
        # to 0.80, which existed precisely to undo the engage-point shrink caused by lowering
        # brake_start 1.5 -> 1.15 (the trigger is a FRACTION of the brake zone, so narrowing the
        # zone silently pulled the engage point in). With the stale override the app engaged at
        # 0.99 m instead of 1.06 m -- deeper into the bearing regime where locks drop. Letting the
        # node default rule steers strictly EARLIER and touches no brake value.
        else               { $a += '--gap-steer on --gap-steer-rate 0.24 --gap-steer-max-bearing-deg 40' }
    }
    # Obstacle brake: depth forward-clearance reflex. Only ever REDUCES forward vx (yaw untouched),
    # ignores the operator being followed, and fails to stop when depth is missing -- it composes with
    # the forbid_forward keystone rather than adding a second forward-authorizing path.
    if($trackObstacle -and $trackObstacle.Checked){
        # --obstacle-band-bot 0.75 (default 0.68): FIELD-MEASURED 2026-09-03. The head camera sits at
        # 0.86 m with a horizontal axis (fitted from per-row floor returns, model vs measured +/-0.05 m
        # over 4 rows). At the 0.68 default the band only sees ABOVE 0.53 m at 1 m range, so a chair
        # seat (~0.45 m) is invisible until ~0.6 m -- the robot hit a chair this way: clearance jumped
        # 1.38 m -> 0.59 m between samples, straight past the grading zone into the stop band. At 0.75
        # the band sees above 0.38 m at 1 m while the FLOOR does not appear until 1.98 m, safely outside
        # the 1.5 m trigger. Do NOT also raise --obstacle-brake-start to 2.0: it would put that floor
        # return inside the braking zone and brake on the ground continuously.
        # DESENSITISED 2026-09-03 after the brake held vx=0.00 on edges/speckle in a cluttered
        # room. The reflex triggered on the 8th percentile of corridor depth with only 40 valid
        # pixels, so a handful of close returns (a glancing table edge, a depth speckle) could
        # latch a full stop. Now it must see a REAL object:
        # WIDER CORRIDOR 2026-09-03 (corridor-frac 0.35 -> 0.55): the robot KEPT RUNNING INTO A
        # CHAIR. The 49.6 deg cone shrinks with range and at 0.5 m covered 0.46 m -- the robot is
        # 0.45 m wide, so an obstacle just outside the cone was invisible to the brake while still
        # squarely in the shoulder path. Gap steering compounded it: as the body turned, the chair
        # slid out of the CENTRE corridor, centre read clear, the brake released, and it drove
        # diagonally into the thing it was avoiding. 0.55 = 72 deg -> 1.02 m at 0.7 m and 0.73 m at
        # 0.5 m, i.e. body width plus real margin all the way to contact range.
        # RE-SENSITISED 2026-09-03 after a field cliff: CLEARANCE went 1.41 -> 0.43 m in ONE 1 Hz
        # sample while travelling only ~0.18 m, i.e. the obstacle APPEARED rather than approached.
        # Cause was min-valid 150: a chair leg or table edge subtends few depth pixels at 1.5 m and
        # plenty at 0.4 m, so requiring 150 made thin objects invisible until close. Walked back:
        #   pctile 20 -> 12     react to nearer returns sooner (still robust vs a raw min)
        #   min-valid 150 -> 70 thin/distant objects register again
        #   brake-stop .6 -> .7 stop ~10 cm further out
        #   aged stays 5        that is the anti-glitch guard, NOT a sensitivity knob
        #   brake-start stays 1.5 -- raising it would put the 1.98 m floor return inside the zone
        # Superseded settings (kept for the record):
        #   pctile 8 -> 20      ignore the closest few % (noise/thin edges stop dominating)
        #   min-valid 40 -> 150 require a genuine footprint, not a speckle
        #   aged 3 -> 5         longer median; a transient cannot latch a stop
        #   brake-stop .7 -> .6 ~10 cm closer before forward is refused
        # brake-start stays 1.5: it is COUPLED to the band -- at band-bot 0.75 the floor first
        # returns at 1.98 m, so a 2.0 m trigger would brake on the ground continuously.
        # NOTE this trades margin for smoothness in the fail-DANGEROUS direction (brakes later,
        # less). Pair with --vx-max 0.15 indoors; a gait cannot stop instantly inside 0.6 m.
        $a += ('--obstacle-brake --obstacle-band-bot 0.75 --obstacle-corridor-frac 0.55 --obstacle-pctile 12 ' +
               '--obstacle-min-valid 70 --obstacle-aged 5 --obstacle-brake-stop 0.7')
        # Plan A: only emit when both Obstacle brake and Class brake are checked (node also
        # requires --obstacle-brake; default Class brake unticked = byte-identical).
        if($trackClassBrake -and $trackClassBrake.Checked){
            $a += '--obstacle-class-brake on'
        }
    }
    # Head probe: one-shot startup head calibration (see the checkbox comment). Independent of
    # every follow feature -- it only measures and logs, then re-centres the head.
    # Escape behaviour when stopped with no reachable heading. Both are new motion, so they are
    # opt-in; spin is preferred over reverse because rotating keeps the sensors on the world.
    if($trackEscape -and $trackEscape.SelectedItem -and [string]$trackEscape.SelectedItem -ne 'off'){
        $a += '--body-scan on'
        if([string]$trackEscape.SelectedItem -eq 'spin+back'){ $a += '--reverse-when-stuck on' }
        # Escape's retrace anchor, the reverse distance budget, and the obstacle-memory dead
        # reckoning all read odometry, and --odom-topic was only ever supplied by the Rerun
        # checkbox -- with Rerun off, arming Escape produced features that silently did nothing
        # (2026-09-05 audit, confirmed: _body_scan_step and _reverse_step bail on latest_odom None).
        # A duplicate of the Rerun line's identical flag is harmless: argparse keeps the last one.
        $a += '--odom-topic /odometer_state'
    }
    # Floor reject: stop reading the floor plane as an obstacle (see the control's comment). Only
    # meaningful with the footprint hit box, which is the only path carrying a height model -- the
    # node ignores it otherwise, but sending it on the frac path would imply it does something.
    if($trackFloor -and $trackFloor.Checked -and
       $trackHitBox -and [string]$trackHitBox.SelectedItem -eq 'footprint'){
        $a += '--ground-reject on'
    }
    # Local map: short-horizon occupancy memory (see the control's comment). Footprint-gated for the
    # same reason as Floor reject -- the cell placement uses the hit box's height model, so it is inert
    # (and misleading) on the image-fraction path. Needs odometry to place cells in a stable frame;
    # --odom-topic is idempotent (argparse keeps the last), so a duplicate of the Escape/Rerun line is
    # harmless. Only ever REDUCES clearance; the three 2026-09-05 audit defects are fixed and gated.
    if($trackLocalMap -and $trackLocalMap.Checked -and
       $trackHitBox -and [string]$trackHitBox.SelectedItem -eq 'footprint'){
        $a += '--localmap on'
        $a += '--odom-topic /odometer_state'
    }
    # Map assist: the pre-built Aurora 3D map reinforces a live obstacle the local avoidance already
    # sees (confirm-only, never brakes alone, never releases). Footprint-gated like Local map.
    # POSE comes from the Aurora odom-floor bridge on /aurora_odom (a drift-free MAP-frame pose), NOT
    # the robot's own /odometer_state -- the map points live in the .vslam/.ply map frame, so the pose
    # must be in that frame or every confirmation is placed at the wrong bearing. The .ply here is the
    # SAME drive capture the bridge relocalizes in (drive_20260908_101056 -- 2026-09-08 re-map, 10251
    # points in the map-assist height band vs 1868 in the old 172257 map), so the frames match. KEEP THIS
    # .ply AND start_bridge.sh's .vslam ON THE SAME drive_* timestamp or the confirm-gate compares a
    # live obstacle against a mis-placed map and silently never confirms.
    # REQUIRES the bridge running on the robot (aurora_odom_bridge.py, bootstrapped); if it is not,
    # /aurora_odom is silent and map-assist (and localmap, which shares --odom-topic) fail closed to
    # live-depth -- safe, just inert. This --odom-topic is emitted after the Local-map one so argparse
    # keeps /aurora_odom; do NOT also enable Rerun (it re-emits /odometer_state and would win).
    if($trackMapAssist -and $trackMapAssist.Checked -and
       $trackHitBox -and [string]$trackHitBox.SelectedItem -eq 'footprint'){
        $a += '--map-assist /home/booster/localmap/drive_20260908_104304.ply'
        $a += '--odom-topic /aurora_odom'
    }
    # Hit box: select depth by the robot's real extent (width, depth, height) instead of an image
    # fraction. Sends the URDF-derived dimensions explicitly so the geometry is visible in the log.
    if($trackHitBox -and $trackHitBox.SelectedItem -and [string]$trackHitBox.SelectedItem -eq 'footprint'){
        $a += ('--corridor-mode footprint --robot-width-m 0.46 --robot-length-m 0.20 ' +
               '--robot-height-m 1.00 --camera-height-m 0.86 --floor-margin-m 0.06 ' +
               '--corridor-margin-m 0.15')
    }
    # Head scan: sweep the head when the brake has us stopped and pick the freest heading.
    # The scan needs /head_pose, which the node only subscribes when a head feature asks for it --
    # --head-scan does, so no extra flag is required here.
    if($trackScan -and $trackScan.SelectedItem -and [string]$trackScan.SelectedItem -ne 'off'){
        # 'patient' is a UI-ONLY name and must be TRANSLATED, not passed through: --head-scan takes
        # only off/audit/on, and sending it verbatim made argparse reject the whole command line and
        # the node refuse to start. It means "on, but wait longer before sweeping".
        $scanMode = [string]$trackScan.SelectedItem
        if($scanMode -eq 'patient'){
            $a += '--head-scan on'
            $a += '--head-scan-dwell-s 4.0'
        } else {
            $a += ('--head-scan ' + $scanMode)
        }
    }
    if ($trackHeadProbe.Checked) {
        $a += '--head-probe'
    }
    # Rerun observability -> record a scrubbable .rrd on the robot. Default OFF; byte-identical when off.
    # The node auto-disables Rerun (RERUN-DISABLED-SLOW) if the loop goes over budget with it on, so the
    # gait is never held hostage to logging -- but the loop-cost gate (RERUN_PLAN.md) is still the
    # operator's call before ticking this WITH DRIVE.
    # Recording ON also enables planar odometry (P6.1a/P8): --odom-topic puts /odom/x,y,theta on the
    # .rrd so the offline stitch (eval/rrd_map.py) has per-frame poses. Guarded on the node side (msg
    # type absent -> odom disabled, follow proceeds); recording-only, never feeds the control law.
    # Loop-cost headroom for a capture (RERUN_PLAN "raise it if the gate is tight"): the follow's own
    # p99 spikes over the 10Hz budget (3 nets, gesture-pose every frame), so the default gate
    # (image-every-n 3 / overrun-frames 8) sheds Rerun early and we lose the recording. Decimate images
    # more (fewer encodes) and tolerate longer transient over-budget streaks so Rerun STAYS ON for the
    # whole capture. Scalars (/follow, /cmd, /odom) still log every tick; only RGB/depth thin out.
    # Session length: the node's default watchdog is 120s -- too short for a full capture (room look-
    # around / sustained pass follow). Raise to 300s. The node still stops first (with _graceful_stop
    # safing the robot); the deadman + operator STOP stay live so you can stop earlier anytime; the app
    # backstop (Track/FollowMaxSec=315) only fires if the node hangs past its own stop.
    # 300 -> 1200 s (4x) on request. The UI backstops at TrackMaxSec/FollowMaxSec must stay ABOVE
    # this or they fire first and kill the session instead of letting the node stop gracefully --
    # they were 315 against 300, so raising this alone would have changed nothing.
    $a += ('--max-seconds {0}' -f $script:K1SessionCapSec)   # matches K1_MAX_SEC; UI backstop TrackMaxSec = cap+30
    if($trackRerun -and $trackRerun.Checked){ $a += ('--rerun --rerun-mode save --rerun-dir {0} --odom-topic /odometer_state --rerun-image-every-n 5 --rerun-overrun-frames 24' -f $script:RerunDir) }
    # Deadman HB: node gates velocity on a fresh /tmp/k1_hb mtime AND env-arms the bridge's own
    # heartbeat watchdog. The app-side relay (Start-HbRelay) is started by Start-Tracker.
    if($trackHbChk -and $trackHbChk.Checked){ $a += '--require-heartbeat' }
    # Lock trigger (gesture control / A/B to retire ArUco). A/B (--lock-trigger both) OVERRIDES the
    # gesture toggle: ArUco still DRIVES the seed while gesture audits (GBIND lines) -- the safe way
    # to compare before flipping the default. gesture/both both need the pose model on the robot.
    $lt='aruco'
    if($chkGesture -and $chkGesture.Checked){ $lt='gesture' }
    if($chkAB -and $chkAB.Checked){ $lt='both' }
    # NOTE: --gesture-stop (in-follow both-hands WAIT) is DELIBERATELY OFF here. It runs the pose model
    # DURING follow (S_TRACK); with a slow .pt/torch model that stalls the 10Hz control loop mid-stride
    # and destabilizes the gait (observed: robot tripping after a gesture lock). Re-enable ONLY after the
    # model is fast (ONNX/TRT) AND GESTURE-MS p99 in follow is measured safe. Use voice/button WAIT instead.
    if($lt -ne 'aruco'){ $a += ('--lock-trigger {0} --gesture-model {1} --gesture-debug' -f $lt,$script:GestureModel) }
    $a += '--commands'   # watched-file command channel (/tmp/k1_cmd) for the Cmd buttons + voice
    # FINAL --odom-topic precedence (fixes an old-vs-new-parameter conflict): map-assist needs the
    # bridge's MAP-frame pose on /aurora_odom, but Local map / Escape / Rerun all emit
    # --odom-topic /odometer_state and argparse keeps the LAST one -- with Rerun on, its line (above)
    # would silently pin map-assist to the ROBOT-frame topic, so it confirms against a mis-placed map
    # and does nothing. Re-emit /aurora_odom here, after every other block, so map-assist is never
    # overridden. Footprint-gated to match the map-assist block; absent (others win) when map-assist off.
    if($trackMapAssist -and $trackMapAssist.Checked -and
       $trackHitBox -and [string]$trackHitBox.SelectedItem -eq 'footprint'){
        $a += '--odom-topic /aurora_odom'
    }
    return ($a -join ' ')
}

# Big state badge (UI thread only): colored by the per-frame status byte.
function Set-TrackBadge([int]$st){
    switch($st){
        0 { $trackBadge.Text='SEARCHING';          $trackBadge.BackColor=[System.Drawing.Color]::Gray }
        1 { $trackBadge.Text='ACQUIRING MARKER';    $trackBadge.BackColor=$amber }
        2 { $trackBadge.Text='LOCKED - FOLLOWING';  $trackBadge.BackColor=$green }
        3 { $trackBadge.Text='LOST - SHOW MARKER';  $trackBadge.BackColor=$red }
        default { $trackBadge.Text='IDLE';          $trackBadge.BackColor=[System.Drawing.Color]::Gray }
    }
}

# --- ReID-health badge (UI thread only). State is a short token; DEGRADED latches until a de-latch line. ---
$script:ReidBadgeState=''
$script:ReidLastGood='CUDA'   # last CONFIRMED GPU-class provider (TRT/CUDA); restored on a mid-run recovery
function Set-ReidBadge([string]$state){
    # Idempotent: skip repaint if unchanged (the node re-logs REID-ENGINE ok only once, but DEGRADED/
    # recovered can repeat). DEGRADED/HIST outrank a later CPU-EP/OK read within a session -> see Update-ReidBadge.
    if($state -eq $script:ReidBadgeState){ return }
    $script:ReidBadgeState=$state
    if($state -in @('TRT','CUDA','CPU')){ $script:ReidLastGood=$state }   # remember the last CONFIRMED provider (CPU too -- a recovery on a CPU-EP host must restore amber CPU, never a guessed green)
    switch($state){
        'TRT'     { $reidBadge.Text='REID: OSNet(TRT)';   $reidBadge.BackColor=$green }
        'CUDA'    { $reidBadge.Text='REID: OSNet(CUDA)';  $reidBadge.BackColor=$green }
        'CPU'     { $reidBadge.Text='REID: CPU-EP';       $reidBadge.BackColor=$amber }
        'HIST'    { $reidBadge.Text='REID: HIST fallback';$reidBadge.BackColor=$red }
        'DEGRADED'{ $reidBadge.Text='REID: DEGRADED';     $reidBadge.BackColor=$red }
        default   { $reidBadge.Text='REID: --';           $reidBadge.BackColor=[System.Drawing.Color]::Gray }
    }
}

# Parse one whitelisted stderr line for ReID health and drive the badge. Called from the frame-timer drain.
# Contract prefixes: 'REID-ENGINE ok ... providers=[...]' (init OK) / 'REID-ENGINE ... FAILED ...' or a
# color_hist fallback line (load-fail -> HIST) / 'REID-DEGRADED <reason> ...' (mid-run fault, latches).
function Update-ReidBadge([string]$line){
    if(-not $line -or $line -notmatch '^REID'){ return }
    # DEGRADED latch: once degraded, stay red until the node logs a recovery/de-latch line.
    if($line -match '^REID-DEGRADED'){
        # CPU-EP fallback is degraded-but-benign (amber, arm auto-refused) -- NOT the hard red all-none latch.
        # It is emitted at init BEFORE the 'REID-ENGINE ok providers=CPU' line, so special-case it here so the
        # amber CPU state is reachable (otherwise the DEGRADED guard below swallows the ok line).
        if($line -match '(?i)cpu-?ep'){ Set-ReidBadge 'CPU'; return }
        # mid-run recovery/de-latch -> restore the LAST CONFIRMED provider (TRT/CUDA), not a guessed one.
        if($line -match '(?i)recover|de-?latch|cleared'){ if($script:ReidBadgeState -eq 'DEGRADED'){ Set-ReidBadge $script:ReidLastGood }; return }
        Set-ReidBadge 'DEGRADED'; return
    }
    if($script:ReidBadgeState -eq 'DEGRADED'){ return }   # latched red overrides init chatter until recovery
    if($line -match '^REID-ENGINE'){
        if($line -match '(?i)fail|color_hist|hist[- ]?fallback'){ Set-ReidBadge 'HIST'; return }
        if($line -match '(?i)ok'){
            if($line -match '(?i)tensorrt|trt'){ Set-ReidBadge 'TRT' }
            elseif($line -match '(?i)cuda'){ Set-ReidBadge 'CUDA' }
            elseif($line -match '(?i)cpuexecutionprovider|cpu-?ep|cpu'){ Set-ReidBadge 'CPU' }
            else { Set-ReidBadge 'CUDA' }   # 'ok' with an unrecognized provider string -> assume GPU-class (green), not a fault
        }
    }
}

# P0 observability: colour a whitelisted node stderr line by fault family for the Tracker log.
# red = fault (FAILED / REID-DEGRADED / HIST / NO-FRAME / DEPTH DOWN|STARVED / RELOC REJECT|ERR),
# amber = warn (CPU-EP / RELOC HOLD), else the normal $accent.
function Get-TrackLineColor([string]$line){
    if(-not $line){ return $accent }
    # Advisory digests are data, not faults: a hint key or class name containing 'hist' or 'failed'
    # must not paint red. A dead auto-improve loop is a warning.
    if($line -match 'TUNE-STALE'){ return $amber }
    if($line -match '^(\[(tune-hints|autotune)\]|==== )'){ return $accent }
    # good-news / recovery lines are NOT faults (so 'DEPTH-STARVED cleared', 'REID-DEGRADED recovered',
    # 'forward vx re-enabled' don't read red via the STARVED/DEGRADED substrings below).
    if($line -match '(?i)cleared|re-enabled|recover|DRIVE-READY'){ return $green }
    # CPU-EP fallback: degraded-but-benign -> amber (checked before the red REID-DEGRADED so 'REID-DEGRADED
    # cpu-ep' is amber, matching the badge). Note 'cpu-?ep' does NOT match 'CPUExecutionProvider' in the
    # healthy providers list, so the good init line stays $accent.
    if($line -match '(?i)cpu-?ep'){ return $amber }
    # amber checked BEFORE red: 'DRIVE-ABORT ping/prep/walk failed' contains 'failed' and would
    # otherwise paint red; DRIVE-ABORT is a refusal (amber) by contract. Verified no red-family
    # line matches this amber regex.
    if($line -match '(?i)RELOC.*HOLD|DEPTH STALE|DRIVE-ABORT|ARM-REFUSED'){ return $amber }
    if($line -match '(?i)FAILED|REID-DEGRADED|HIST|NO-FRAME|DEPTH.*(DOWN|STARVED)|RELOC.*(REJECT|ERR)'){ return $red }
    return $accent
}
# Single-driver coordination: while the Tracker runs, lock out the Control-tab follow,
# the manual controller connect, and this tab's own DRIVE/ARM mode switches; restore on stop.
function Set-TrackLockout([bool]$tracking){
    $idle = -not $tracking
    if($followToggle){ $followToggle.Enabled = ($idle -and -not $script:CtrlOn) }
    if($followDriveChk){ $followDriveChk.Enabled = $idle }
    if($connCtrlBtn){ $connCtrlBtn.Enabled = ($idle -and -not $script:CtrlOn) }
    $trackDriveChk.Enabled = $idle
    $trackArmChk.Enabled = $idle
    $ipTrack.Enabled = $idle
    # Perception-row controls are launch-time settings -> lock them while a session runs.
    if($trackApp){ $trackApp.Enabled = $idle }
    if($trackCoast){ $trackCoast.Enabled = $idle }
    if($trackReacq){ $trackReacq.Enabled = $idle }
    if($trackFence){ $trackFence.Enabled = $idle }
    if($trackArmReloc){ $trackArmReloc.Enabled = $idle }
    # Gesture lock / A-B are LAUNCH-time settings -> lock them while a session runs (like Perception).
    if($chkGesture){ $chkGesture.Enabled = $idle }
    if($chkAB){ $chkAB.Enabled = $idle }
    # Tier-1 command buttons + voice are the INVERSE of the launch controls: usable ONLY while live.
    foreach($b in @($btnWait,$btnResume,$btnPark,$btnStatus,$btnFollowCmd)){ if($b){ $b.Enabled = $tracking } }
    if($chkVoice){
        $chkVoice.Enabled = $tracking
        if(-not $tracking -and $chkVoice.Checked){ $chkVoice.Checked = $false }   # session ended -> CheckedChanged stops voice
    }
}

# Send ONE Tier-1 command token to the running follow node via a short-lived ssh write to the watched
# file /tmp/k1_cmd (NEVER the node's PTY stdin, which carries the Ctrl-C deadman). De-escalating
# WAIT/PARK/STATUS go immediately; motion-initiating RESUME/FOLLOW require ARM MOTION + a typed confirm
# under DRIVE and carry the per-command 'ARM' credential (echo writes "RESUME ARM"). The node parses
# the first word as the command and 'ARM' as the credential.
function Send-FollowCmd([string]$tok){
    if(-not $script:TrackOn){ Add-LogTrack 'No follow session is running.' $amber; return }
    $ip=$ipTrack.Text.Trim(); if(-not $ip){ Add-LogTrack 'Enter the robot IP first.' $amber; return }
    $line=$tok
    if($tok -eq 'RESUME' -or $tok -eq 'FOLLOW'){
        if($script:TrackDrive){
            if(-not $trackArmChk.Checked){
                [System.Windows.Forms.MessageBox]::Show("Tick 'ARM MOTION' first - $tok will make the robot WALK.",'Not armed','OK','Warning')|Out-Null
                return
            }
            $r=[System.Windows.Forms.MessageBox]::Show("Interpreted as: $tok - the robot will WALK. Proceed?",'Confirm motion command',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return }
            $line="$tok ARM"
        }
    }
    # A slow earlier MOTION command (RESUME/FOLLOW [ARM]) is cancelled before the next send, so it can
    # never land AFTER a later one (e.g. HOLD) and walk the robot. An unfinished STOP/HOLD/PARK/STATUS is
    # NEVER cancelled: it may still be connecting, and killing it would drop a safe-down. The append plus
    # the node's STOP > HOLD priority settles the order of those.
    if($script:CmdProc -and -not $script:CmdProc.HasExited -and $script:CmdLine -match '^(RESUME|FOLLOW)\b'){
        try{ $script:CmdProc.Kill() }catch{}
        Add-LogTrack ("CMD '$($script:CmdLine)' was still sending -- cancelled before '$line'; it may or may not have reached the robot.") $amber
    }
    try{
        # APPEND, never overwrite: the node reads every queued line and applies STOP > HOLD priority, so a
        # later command can never erase an earlier one it has not read yet.
        $cmd="echo $line >> /tmp/k1_cmd"
        $sp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),$cmd)) -NoNewWindow -PassThru
        try{ $null=$sp.Handle }catch{}
        $script:CmdProc=$sp; $script:CmdLine=$line
        if($sp.WaitForExit(4000)){
            if($sp.ExitCode -eq 0){ Add-LogTrack ("CMD sent: $line") $accent }
            else { Add-LogTrack ("CMD NOT delivered: $line (ssh exit $($sp.ExitCode)). For a STOP use the gamepad / e-stop.") $red }
        } else {
            Add-LogTrack ("CMD not confirmed after 4 s: $line (still sending; the next command cancels it). For a STOP use the gamepad / e-stop.") $amber
        }
    }catch{ Add-LogTrack ("CMD send failed: $_") $red }
}

# Map a recognized spoken phrase to ONE frozen command-enum token (de-escalation words first;
# 'stop'/'wait' are honored on the recognized TEXT, never gated on ASR confidence). $null = ignore.
function Map-VoicePhrase([string]$t){
    switch -regex (($t).ToLower().Trim()){
        '^stop$'                        { return 'STOP' }
        '^(wait|hold|stay)$'            { return 'HOLD' }
        '^(park|stand down)$'           { return 'PARK' }
        '^status$'                      { return 'STATUS' }
        '^(resume|continue|carry on)$'  { return 'RESUME' }
        '^(follow|follow me|come)$'     { return 'FOLLOW' }
    }
    return $null
}

# Voice commands via the in-box .NET System.Speech recognizer (no Python/Node). A small keyword
# grammar feeds the SAME enum + Send-FollowCmd path the buttons use. Recognition fires on a worker
# thread, so the action is marshalled to the UI thread via $form.BeginInvoke. Motion words still go
# through Send-FollowCmd's ARM + typed-confirm gate under DRIVE. Crash-safe: any failure -> logged,
# toggle reset (e.g. no recognizer installed).
function Start-Voice {
    if($script:VoiceRec){ return }
    try{
        Add-Type -AssemblyName System.Speech
        $rec=New-Object System.Speech.Recognition.SpeechRecognitionEngine
        $ch=New-Object System.Speech.Recognition.Choices
        $ch.Add([string[]]@('stop','wait','hold','stay','park','stand down','status','resume','continue','carry on','follow','follow me','come'))
        $gb=New-Object System.Speech.Recognition.GrammarBuilder; $gb.Append($ch)
        $rec.LoadGrammar((New-Object System.Speech.Recognition.Grammar($gb)))
        $rec.SetInputToDefaultAudioDevice()
        $rec.add_SpeechRecognized({ param($eventSender,$e)
            $tok=Map-VoicePhrase ([string]$e.Result.Text)
            if($tok){ try{ [void]$form.BeginInvoke([Action[string]]{ param($t) Add-LogTrack ("VOICE -> $t") $accent; Send-FollowCmd $t }, $tok) }catch{} }
        })
        $rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
        $script:VoiceRec=$rec
        if($voiceStatus){ $voiceStatus.Text='Voice: listening'; $voiceStatus.ForeColor=$green }
        Add-LogTrack 'Voice ON - say: stop / wait / park / status / resume / follow.' $green
    }catch{
        Add-LogTrack ("Voice start failed (no recognizer?): $_") $red
        if($voiceStatus){ $voiceStatus.Text='Voice: error'; $voiceStatus.ForeColor=$red }
        if($chkVoice){ $script:VoiceGuard=$true; $chkVoice.Checked=$false; $script:VoiceGuard=$false }
    }
}
function Stop-Voice {
    if(-not $script:VoiceRec){ return }
    try{ $script:VoiceRec.RecognizeAsyncStop() }catch{}
    try{ $script:VoiceRec.Dispose() }catch{}
    $script:VoiceRec=$null
    if($voiceStatus){ $voiceStatus.Text='Voice: off'; $voiceStatus.ForeColor=[System.Drawing.Color]::DimGray }
    Add-LogTrack 'Voice OFF.' $amber
}

# OPERATOR SESSION signal -- shared contract with desktop/Auto-Tune-Loop.ps1. While the named mutex
# Global\K1-Operator-Session EXISTS, the loop starts no .rrd pull or bundle send and kills a running pull
# within 5 s, so a bulk transfer never shares the link with a launch pre-flight or a follow. Created NOT
# owned (its existence is the signal) before the pre-flight deploy; disposed when the session ends or the
# launch aborts. Local\ is the fallback if the Global namespace refuses.
# Several sessions own the robot link: a follow ('follow': Tracker or Control-tab, never both), the Live
# view ('live') and the manual loco controller ('ctrl'). Each opens/closes under its own owner name; the
# mutex exists while ANY owner holds it, and both calls are idempotent per owner.
$script:OperatorMutex = $null
$script:OperatorOwners = @{}
function Open-OperatorSession([string]$owner='follow'){
    if(-not $script:OperatorOwners){ $script:OperatorOwners = @{} }
    $script:OperatorOwners[$owner] = $true
    if($script:OperatorMutex){ return }
    try{ $script:OperatorMutex = New-Object System.Threading.Mutex($false, 'Global\K1-Operator-Session') }
    catch{ try{ $script:OperatorMutex = New-Object System.Threading.Mutex($false, 'Local\K1-Operator-Session') }catch{ $script:OperatorMutex = $null } }
}
function Close-OperatorSession([string]$owner='follow'){
    if(-not $script:OperatorOwners){ $script:OperatorOwners = @{} }
    $script:OperatorOwners.Remove($owner)
    if($script:OperatorOwners.Count -gt 0){ return }   # another session still owns the link
    try{ if($script:OperatorMutex){ $script:OperatorMutex.Dispose() } }catch{}
    $script:OperatorMutex = $null
}

# Stop any follow node on the robot and CONFIRM none is left: GONE, STILL-RUNNING, or UNKNOWN (ssh failed
# or timed out). SIGTERM -> the node's handler stops + ChangeMode(kPrepare); it gets 15 s to exit (the
# bridge shutdown alone is ~4 s, then the graceful settle and the CUDA/TRT teardown). The escaped \.
# keeps the pattern from matching this command's own shell line (pkill/pgrep -f match whole command
# lines). TimeoutMs covers ssh ConnectTimeout (8 s) + that 15 s loop + margin, so a slow but valid
# answer is never reported as "did not answer".
function Stop-RobotFollowNode([string]$ip,[int]$TimeoutMs=26000){
    $tmp = Join-Path $env:TEMP ('k1_kill_{0}.txt' -f ([IO.Path]::GetRandomFileName() -replace '\.',''))
    try{
        $cmd = 'pkill -TERM -f ''follow_person_k1\.py''; for i in $(seq 30); do pgrep -f ''follow_person_k1\.py'' >/dev/null; rc=$?; [ $rc -eq 1 ] && { echo NODE-GONE; exit 0; }; [ $rc -ne 0 ] && { echo NODE-ERR $rc; exit 0; }; sleep 0.5; done; echo NODE-STILL-RUNNING'
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), $cmd)) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
        try{ $null = $p.Handle }catch{}
        if(-not $p.WaitForExit($TimeoutMs)){ try{ $p.Kill() }catch{}; $null = $p.WaitForExit(2000); return 'UNKNOWN' }
        $out = [string](Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
        if($p.ExitCode -eq 0 -and $out -match 'NODE-GONE'){ return 'GONE' }
        if($p.ExitCode -eq 0 -and $out -match 'NODE-STILL-RUNNING'){ return 'STILL-RUNNING' }
        return 'UNKNOWN'
    }catch{ return 'UNKNOWN' }
    finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
}

function Start-Tracker([bool]$drive){
    if($script:TrackOn){ return $false }
    # SINGLE LOCO DRIVER RULE: never run alongside the Control-tab follow or the manual controller.
    if($script:FollowOn){
        [System.Windows.Forms.MessageBox]::Show("The Control-tab Follow is running. Stop it first - only one process may drive locomotion at a time.",'Tracker blocked','OK','Warning')|Out-Null
        return $false
    }
    if($script:CtrlOn){
        [System.Windows.Forms.MessageBox]::Show("The manual controller (Control tab) is connected. Disconnect it first - only one process may drive locomotion at a time.",'Tracker blocked','OK','Warning')|Out-Null
        return $false
    }
    if($drive){
        if(-not $trackArmChk.Checked){
            [System.Windows.Forms.MessageBox]::Show("Tick 'ARM MOTION' before Follow in DRIVE mode.",'Not armed','OK','Warning')|Out-Null
            return $false
        }
        $r=[System.Windows.Forms.MessageBox]::Show("Start FOLLOW in DRIVE mode?`r`n`r`nMarker = one-time lock onto the person, then the K1 will PHYSICALLY WALK to follow THAT PERSON (re-show the marker to re-seed). Standoff $((Get-TrackStandoff)) m, max speed $((Get-TrackVxMax)) m/s. Auto-stops after ~$($script:K1SessionCapSec)s (session cap), when the person is lost, or if the camera stalls. Clear the area and keep the e-stop handy.",'Confirm DRIVE follow',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -ne 'OK'){ return $false }
    }
    $ip=$ipTrack.Text.Trim(); if(-not $ip){ Add-LogTrack 'Enter the robot IP first.' $amber; return $false }
    $script:RobotIP=$ip
    # Pre-flight starts here: hold the Auto-Tune loop off the link from the first deploy byte onward.
    # Every abort below returns $false, and the toggle handler then closes the session.
    Open-OperatorSession
    if(-not $script:OperatorMutex){ Add-LogTrack 'Operator session not signalled (mutex create failed) -- the Auto-Tune loop may pull during this launch.' $amber }
    # A slow motion command from an earlier session must not land in this one.
    if($script:CmdProc -and -not $script:CmdProc.HasExited){ try{ $script:CmdProc.Kill() }catch{} }; $script:CmdProc=$null
    # ONE NODE AT A TIME: stop any previous follow node and CONFIRM it is gone BEFORE the deploy
    # overwrites its files (run_follow.sh also refuses to exec a second node -- exit 5). Fail-closed:
    # no confirmation, no launch.
    $old = Stop-RobotFollowNode $ip
    if($old -eq 'UNKNOWN'){ $old = Stop-RobotFollowNode $ip }   # one retry: a slow answer is not a refusal
    if($old -eq 'STILL-RUNNING'){ Add-LogTrack ('An earlier follow node is still running on the robot 15 s after SIGTERM -- NOT starting a second one. If it does not exit, force it:  ssh {0}@{1} "pkill -KILL -f ''follow_person_k1\.py''"  (the bridge stops on stdin EOF), then toggle Follow again. If the robot is moving, use the gamepad / e-stop.' -f $script:SshUser,$ip) $red; return $false }
    if($old -ne 'GONE'){ Add-LogTrack ("Could not confirm that no follow node is running on {0} (the robot did not answer) -- NOT launching. Toggle Follow again." -f $ip) $red; return $false }
    # SINGLE CAMERA CONSUMER: stop the Live View stream (it also decodes the head
    # camera) so the K1 only ever streams the camera once.
    if($script:LiveOn){ Add-LogTrack 'Stopping Live View (single camera consumer)...' $amber; Stop-Live }
    Add-LogTrack ("Deploying follow helpers to {0} ..." -f $ip) $accent
    if(-not (Deploy-FollowFiles $ip)){ Add-LogTrack ("Deploy failed: " + $(if($script:DeployErr){$script:DeployErr}else{"a helper under '$ROBOT_DIR' could not be deployed"})) $red; return $false }
    # Gesture / A-B selected -> make sure the pose model is on the robot first (auto-stage). On failure
    # the node still runs and falls back to the ArUco marker, so offer to continue rather than block.
    if(($chkGesture -and $chkGesture.Checked) -or ($chkAB -and $chkAB.Checked)){
        Resolve-GestureModel $ip   # P4.2b: prefer the TRT engine when built (else .onnx)
        $gst = Ensure-GestureModel $ip
        if($gst -eq 'UNKNOWN'){
            $r=[System.Windows.Forms.MessageBox]::Show("The robot at $ip did not answer the gesture-model check (usually a busy link). If the model is missing, gesture FALLS BACK to the ArUco marker (safe). Start anyway?",'Gesture model not checked',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return $false }
        } elseif($gst -ne 'PRESENT'){
            $r=[System.Windows.Forms.MessageBox]::Show("The gesture pose model isn't on the robot and couldn't be auto-staged. Gesture will FALL BACK to the ArUco marker (safe). Start anyway?",'Gesture model missing',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return $false }
        }
    }
    # P0 ITEM 6: OSNet backend selected -> PREFLIGHT the ReID ONNX (static, file-present half; the dynamic
    # 'init succeeded' half is the REID badge from the REID-ENGINE ok line). On a missing/unstageable engine
    # we do NOT force 'striped' (a documented lock-loser) and do NOT silently arm: the node falls back to a
    # colour histogram, the badge shows HIST (red), and the arm-gate auto-REFUSES armed re-lock. So: WARN + confirm.
    $reidApp = if($trackApp -and $trackApp.SelectedItem){ [string]$trackApp.SelectedItem } else { 'global' }
    if($reidApp -eq 'osnet'){
        $rst = Ensure-ReidModel $ip
        if($rst -eq 'UNKNOWN'){
            $r=[System.Windows.Forms.MessageBox]::Show("The robot at $ip did not answer the OSNet ReID engine check (usually a busy link). The node checks the engine itself at start: the REID badge shows OSNet(TRT)/OSNet(CUDA) when it loaded, HIST (red) when it did not -- and then ARMED markerless re-lock is auto-refused. Start anyway?",'ReID engine not checked',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return $false }
            Add-LogTrack 'ReID engine not checked (robot did not answer) -> watch the REID badge: OSNet = loaded, HIST = missing.' $amber
        } elseif($rst -ne 'PRESENT'){
            $r=[System.Windows.Forms.MessageBox]::Show("The OSNet ReID engine ($script:ReidEngine) isn't on the robot and couldn't be auto-staged. Deep re-ID will run on a COLOUR-HISTOGRAM fallback (shown red on the REID badge) and ARMED markerless re-lock will be auto-refused. Start anyway?",'ReID engine missing',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return $false }
            Add-LogTrack 'OSNet engine missing -> node falls back to histogram; armed re-lock auto-refused. Badge shows HIST.' $amber
        }
    }
    # Rerun selected -> stage rerun-sdk (offline wheels). Best-effort: a follow is NEVER blocked by
    # Rerun. On failure the node runs with Rerun disabled (no .rrd); offer to continue.
    if($trackRerun -and $trackRerun.Checked){
        if(-not (Ensure-RerunWheels $ip)){
            $r=[System.Windows.Forms.MessageBox]::Show("rerun-sdk isn't importable on the robot and couldn't be auto-staged from a local wheels\ dir. Rerun recording will be DISABLED (the follow runs normally, no .rrd). Start anyway?",'Rerun unavailable',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
            if($r -ne 'OK'){ return $false }
            # Uncheck so Get-TrackExtraArgs OMITS --rerun (review fix): the launch args now match the
            # message above -- the follow is byte-identical by construction, and a Test-RerunImport
            # false-negative can't silently start a recording the operator was told wouldn't exist.
            $trackRerun.Checked = $false
            Add-LogTrack 'rerun-sdk missing -> --rerun omitted for this start; re-tick Rerun to retry staging.' $amber
        }
    }
    # (the earlier follow node was stopped and confirmed gone at the top of the pre-flight)
    $trackSync.Stop=$false; $trackSync.Done=$false; $trackSync.Jpeg=$null; $trackSync.Seq=0; $trackSync.Frames=0; $trackSync.Err=''
    $script:trackLastSeq=-1; $script:trackFpsFrames=0; $script:trackLastLock=-1; $trackSync.Lock=0
    # CONTROLLER RUN OVERRIDE: a ticked 'Controller run' forces preview even if the operator hit the
    # drive button. Preview never opens the bridge, so the node cannot command velocity -- that is the
    # safety property that makes it OK to drive the robot by hand while it records.
    $ctrlRun = ($trackCtrlRun -ne $null -and $trackCtrlRun.Checked)
    $mode = if($drive -and -not $ctrlRun){'drive'}else{'preview'}
    if($ctrlRun -and $drive){ Add-LogTrack 'CONTROLLER RUN: drive request overridden to --preview (node will not command velocity). Drive with the gamepad.' $amber }
    $standoff=Get-TrackStandoff; $vxmax=Get-TrackVxMax; $extra=Get-TrackExtraArgs
    if($ctrlRun){ $extra = "$extra --profile capture" }   # RGB+depth+scalars+intrinsics+odom bundle
    $remote="K1_MAX_SEC=$($script:K1SessionCapSec) bash /home/booster/run_follow.sh $mode /boostercamera/head/raw/rgb --stream --standoff-m $standoff --vx-max $vxmax $extra"
    Add-LogTrack ("launch: " + $remote) $accent   # echo so the operator can verify --lock-trigger / --gesture-* flags
    # NO -tt (PTY would corrupt the binary JPEG stream). ServerAliveCountMax=1 -> a dropped
    # link triggers remote SIGHUP fast (~5s) for the WALKING case.
    $argStr=(Get-SshOptString)+' -o ServerAliveCountMax=1'+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogTrack 'Failed to start ssh.' $red; return $false }
    $script:TrackProc=$proc
    # Capture the node's STDERR (its --stream log lines) into the Tracker log. STDERR is a SEPARATE
    # pipe from the binary JPEG stdout, so this NEVER disturbs the frame stream. Background event ->
    # thread-safe queue -> drained on the UI thread by the frame timer. Filtered to the gesture/command/
    # safety lines so it stays readable (per-frame SEARCH/TRACK heartbeats excluded).
    $trackSync.ErrLines = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
    $errAction = {
        $d = $EventArgs.Data
        # P0 OSNet-health: ALSO pass the ReID/reloc/depth/frame-stall families (REID-ENGINE, REID-DEGRADED,
        # RELOC-*, DEPTH*/DEPTH-STARVED, NO-FRAME). These prefixes are emitted by follow_person_k1.py's log()
        # at column 0 (see the node<->app log-prefix contract); the UI badge + amber/red coloring parse them.
        # Also the launcher's advisory digests, printed by run_follow.sh before the node starts: the tune
        # hints and autotune label summary ([tune-hints] / [autotune] and their ==== banners) and the
        # [run_follow] TUNE-STALE alarm for a dead auto-improve loop. Filtered out, they never reached
        # the operator (review C12).
        if($d -and ($d -match '^(GDBG|GESTURE|LOCK-TRIGGER|SEED|LOCKED|AUTO-RELOCK|CMD|GBIND|DRIVE-|BRIDGE|ARM|HELD|RANGE-GATE|HB-|SLOW-LOOP|LOOP-MS|WATCHDOG|FRAME-ERR|RESUME|EXIT|MODE |REID|RELOC|DEPTH|NO-FRAME stall=|RGB|RERUN|\[tune-hints\]|\[autotune\]|==== (end )?(TUNE HINTS|AUTOTUNE)|\[run_follow\] (TUNE-STALE|tune hints|REFUSED))')){
            $Event.MessageData.Enqueue($d)
        }
    }
    try{ $script:TrackErrSub = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errAction -MessageData $trackSync.ErrLines; $proc.BeginErrorReadLine() }catch{ Add-LogTrack ("stderr capture failed: $_") $amber }
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('trackSync',$trackSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($trackReader); [void]$ps.BeginInvoke()
    $script:TrackPS=$ps; $script:TrackRS=$rs
    $script:TrackOn=$true; $script:TrackDrive=$drive; $script:TrackStart=[datetime]::Now
    # P6.2b: remember whether THIS session records a .rrd (rerun on, post any auto-uncheck above), so
    # Stop-Tracker fires the post-run offload only for capture sessions.
    $script:TrackRerunOn = [bool]($trackRerun -and $trackRerun.Checked)
    Set-TrackLockout $true
    Set-TrackBadge 0
    $script:ReidBadgeState=''; $script:ReidLastGood='CUDA'; Set-ReidBadge '--'   # clear stale health + provider memory from a prior session; repopulated from REID-ENGINE lines
    # Deadman HB: start the relay BEFORE the node launches so /tmp/k1_hb is fresh at the node's
    # first gate check (the node fails closed on a missing/stale file either way).
    if($trackHbChk -and $trackHbChk.Checked){ Start-HbRelay $ip }
    if($drive){
        Add-LogTrack ("FOLLOW DRIVE started ($ip): standoff $standoff m, max speed $vxmax m/s. Show the marker to lock onto the person, then the robot WALKS to follow THAT PERSON. ~10s camera warmup. Toggle OFF / STOP halts + returns to PREP.") $red
        $statusLbl.Text="Tracker DRIVE active: $ip"
    } else {
        Add-LogTrack ("FOLLOW PREVIEW started ($ip): standoff $standoff m, max speed $vxmax m/s (preview never moves). Show the marker to lock onto the person, then person-track. ~10s camera warmup.") $green
        $statusLbl.Text="Tracker preview active: $ip"
    }
    return $true
}

# --- Deadman-HB relay (P2 #12). The remote loop touches the hb file ONLY when a byte ARRIVES
# from this app's stdin pipe (`while read; do touch; done` -- no timers), so the file's mtime is
# PROOF of live app->robot connectivity. Any break in the chain (WiFi drop, app freeze, laptop
# sleep, kill) stops the touches within one byte-interval -> node zeroes velocity at 400ms and
# the bridge stands (kPrepare) at 1.5s. Bytes are pumped from the 40ms $mediaTimer (~25Hz).
function Start-HbRelay([string]$ip){
    Stop-HbRelay
    try{
        $psi=New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName='ssh.exe'
        $psi.Arguments=(Get-SshOptString)+" $($script:SshUser)@$ip `"while read -r _; do touch /tmp/k1_hb; done`""
        $psi.UseShellExecute=$false; $psi.RedirectStandardInput=$true; $psi.CreateNoWindow=$true
        $p=New-Object System.Diagnostics.Process; $p.StartInfo=$psi
        if($p.Start()){
            $p.StandardInput.AutoFlush=$true
            $script:HbProc=$p
            Add-LogTrack 'Deadman HB relay started (byte-driven touch; link loss stands the robot; link RETURN auto-resumes the follow -- tethered semantics, see UNTETHERED_FOLLOW.md blocker 3).' $green
        } else {
            Add-LogTrack 'Deadman HB relay FAILED to start -> node holds zero velocity (fail-closed).' $red
        }
    }catch{ Add-LogTrack ("Deadman HB relay error: $_ -> node holds zero velocity (fail-closed).") $red }
}
function Stop-HbRelay{
    try{ if($script:HbProc -and -not $script:HbProc.HasExited){ $script:HbProc.Kill() } }catch{}
    $script:HbProc=$null
}

# P6.2b: fire-and-forget post-session offload. Launch Offload-Run.ps1 (ssh bundle -> pull) DETACHED so
# the WinForms teardown never blocks and this can never throw into it. Only meaningful when the session
# RECORDED (rerun on); offload_run.sh bundles whatever exists and Pull-Run.ps1 verifies by hash.
function Invoke-Offload([string]$ip, [string]$profileLabel){
    try{
        if(-not $ip){ return }
        $script = Join-Path $SCRIPT_DIR 'Offload-Run.ps1'
        if(-not (Test-Path $script)){ Add-LogTrack 'Offload skip: Offload-Run.ps1 not found beside the app.' $amber; return }
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
            '-NoProfile','-ExecutionPolicy','Bypass','-File',$script,
            '-Ip',$ip,'-User',$script:SshUser,'-Profile',$profileLabel) | Out-Null   # no -Pass: key-only SSH; an empty value made Start-Process throw, a set K1PW showed on the command line
        Add-LogTrack ("Offload started (background): bundle on robot -> pull to runs\ (label $profileLabel).") $accent
    }catch{ try{ Add-LogTrack ("Offload skip: $_") $amber }catch{} }
}

function Stop-Tracker([bool]$procAlreadyDead=$false){
    if(-not $script:TrackOn){ return }
    # P6.2b: capture what the offload needs BEFORE the teardown resets it (TrackDrive is cleared below).
    $offloadDo = [bool]$script:TrackRerunOn
    $offloadProfile = if($script:TrackDrive){'tracker-drive'}else{'tracker-preview'}
    $offloadIp=''; try{ $offloadIp=$ipTrack.Text.Trim() }catch{}
    $trackSync.Stop=$true
    # No command still in flight may land in a stopped (or the next) session.
    if($script:CmdProc -and -not $script:CmdProc.HasExited){ try{ $script:CmdProc.Kill() }catch{} }; $script:CmdProc=$null
    # Tear down the stderr capture FIRST so the ErrorDataReceived handler stops firing across restarts.
    try{ if($script:TrackProc){ $script:TrackProc.CancelErrorRead() } }catch{}
    try{ if($script:TrackErrSub){ Unregister-Event -SubscriptionId $script:TrackErrSub.Id -ErrorAction SilentlyContinue; $script:TrackErrSub=$null } }catch{}
    # LOCAL kill FIRST and unconditionally - never gated behind a WinForms control read that
    # could throw during FormClosing. Tearing down the local ssh starts the remote SIGHUP path.
    try{ if($script:TrackProc -and -not $script:TrackProc.HasExited){ $script:TrackProc.Kill() } }catch{}
    Stop-HbRelay   # heartbeat relay dies with the follow; remote loop exits on stdin EOF
    # THEN an independent belt-and-suspenders pkill over a second short-lived ssh (control read guarded),
    # so the robot is safed even if the streaming socket is half-open. The node's SIGTERM handler does
    # stop + ChangeMode(kPrepare); killing the node also EOFs the bridge stdin, which safes loco too.
    $ip=''
    try{ $ip=$ipTrack.Text.Trim() }catch{}
    $gone='UNKNOWN'
    if($ip){ $gone = Stop-RobotFollowNode $ip }
    try{ if($script:TrackPS){ $script:TrackPS.Dispose() } }catch{}
    try{ if($script:TrackRS){ $script:TrackRS.Close() } }catch{}
    $script:TrackProc=$null; $script:TrackPS=$null; $script:TrackRS=$null
    $script:TrackOn=$false; $script:TrackDrive=$false
    # UI restore wrapped so a disposed control can't abort the safety work above
    try{
        Set-TrackLockout $false
        if($trackArmChk.Checked){ $trackArmChk.Checked=$false }
        $script:TrackToggleGuard=$true; $trackToggle.Checked=$false; $script:TrackToggleGuard=$false
        $trackToggle.Text='Follow: OFF'; $trackToggle.UseVisualStyleBackColor=$true; $trackToggle.ForeColor=[System.Drawing.SystemColors]::ControlText
        $script:trackLastLock=-1; Set-TrackBadge -1
        if($trackPic.Image){ $img=$trackPic.Image; $trackPic.Image=$null; $img.Dispose() }
        if($script:trackMs){ $script:trackMs.Dispose(); $script:trackMs=$null }
        if($gone -eq 'GONE'){ Add-LogTrack 'Follow stopped: no follow node left on the robot (checked).' $amber }
        elseif($gone -eq 'STILL-RUNNING'){ Add-LogTrack ('Follow stop NOT confirmed: the node was still running 15 s after SIGTERM. If the robot is moving, use the gamepad / e-stop. To force it:  ssh {0}@{1} "pkill -KILL -f ''follow_person_k1\.py''"  (the bridge stops on stdin EOF). The next launch refuses to start a second node.' -f $script:SshUser,$ip) $red }
        elseif(-not $ip){ Add-LogTrack 'Follow stopped locally, but there is no robot IP, so the robot-side stop was not sent or checked. If the robot is moving, use the gamepad / e-stop.' $red }
        else { Add-LogTrack 'Follow stop NOT confirmed: the robot did not answer the stop check. If the robot is moving, use the gamepad / e-stop.' $red }
        $statusLbl.Text='Tracker stopped.'
    }catch{}
    # P6.2b: AFTER the robot is safed + UI restored, fire the post-run offload for a capture session
    # (detached, non-blocking, never throws). $script:TrackRerunOn was captured to $offloadDo at the
    # top, before the teardown reset TrackDrive.
    if($offloadDo){ Invoke-Offload $offloadIp $offloadProfile }
    $script:TrackRerunOn = $false
    Close-OperatorSession   # session over -> the Auto-Tune loop may use the link again (shared contract)
}

# ============================================================================
#  Control connect / send
# ============================================================================
$ctrlReader = {
    try{
        while(-not $ctrlSync.Stop -and -not $proc.HasExited){
            $line=$proc.StandardOutput.ReadLine(); if($null -eq $line){break}
            $ctrlSync.Log.Enqueue($line)
        }
    }catch{ $ctrlSync.Log.Enqueue('[reader err] '+$_.Exception.Message) }
}
function Connect-Ctrl {
    if($script:CtrlOn){return}
    $ip=$ipCtrl.Text.Trim(); $iface=$ifaceBox.Text.Trim(); if(-not $iface){$iface='127.0.0.1'}
    if(-not $ip){ Add-LogCtrl 'Enter the robot IP first.' $amber; return }
    $script:RobotIP=$ip
    Open-OperatorSession 'ctrl'   # the manual controller owns the link too: hold the Auto-Tune loop off it (shared contract)
    Add-LogCtrl ("Deploying loco launcher to {0} ..." -f $ip) $accent
    Deploy-RobotFiles $ip | Out-Null
    $ctrlSync.Stop=$false; $ctrlSync.Log.Clear()
    $remote="bash /home/booster/run_loco.sh $iface"
    # -tt forces a PTY so interactive stdin (button commands) is actually forwarded to
    # the robot. Without it ssh swallows the keystrokes and the robot does nothing.
    $argStr='-tt '+(Get-SshOptString)+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardInput=$true; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogCtrl 'Failed to start ssh.' $red; Close-OperatorSession 'ctrl'; return }
    try{ $proc.StandardInput.AutoFlush=$true }catch{}
    try{ $proc.StandardInput.NewLine="`n" }catch{}   # LF only: the robot's getline compares against "gft" etc., a trailing CR breaks the match
    $script:CtrlProc=$proc
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('ctrlSync',$ctrlSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($ctrlReader); [void]$ps.BeginInvoke()
    $script:CtrlPS=$ps; $script:CtrlRS=$rs; $script:CtrlOn=$true
    $connCtrlBtn.Enabled=$false; $discCtrlBtn.Enabled=$true; $gftBtn.Enabled=$true
    $followToggle.Enabled=$false; $followDriveChk.Enabled=$false   # manual controller owns loco; no follow while connected
    if($trackToggle){ $trackToggle.Enabled=$false; $trackDriveChk.Enabled=$false; $trackArmChk.Enabled=$false }   # ...and no Tracker follow either
    $btnDamp.Enabled=$true; $btnStop.Enabled=$true
    $script:CtrlPrime=9   # auto-send a priming gft after ~9s so DDS discovery finishes before the first real command
    $connStatus.Text="Connected ($ip). Priming link (~9s for DDS discovery) - then Test link / commands respond. ARM for motion."
    Add-LogCtrl ("Loco controller launched (iface {0}). Warming up DDS discovery; the FIRST command can take ~10s after connect." -f $iface) $green
    $statusLbl.Text="Controller connected: $ip (priming...)"
}
function Disconnect-Ctrl {
    if(-not $script:CtrlOn){return}
    $ctrlSync.Stop=$true
    try{ if($script:CtrlProc -and -not $script:CtrlProc.HasExited){ $script:CtrlProc.Kill() } }catch{}
    try{ if($script:CtrlPS){ $script:CtrlPS.Dispose() } }catch{}
    try{ if($script:CtrlRS){ $script:CtrlRS.Close() } }catch{}
    $script:CtrlOn=$false; $connCtrlBtn.Enabled=$true; $discCtrlBtn.Enabled=$false; $gftBtn.Enabled=$false
    $armChk.Checked=$false; Set-MotionEnabled $false; $btnDamp.Enabled=$false; $btnStop.Enabled=$false
    $followToggle.Enabled=$true; $followDriveChk.Enabled=$true   # re-enable follow (DRIVE still needs a fresh ARM)
    if($trackToggle -and -not $script:TrackOn){ $trackToggle.Enabled=$true; $trackDriveChk.Enabled=$true; $trackArmChk.Enabled=$true }   # re-enable Tracker too
    $connStatus.Text='Disconnected.'; Add-LogCtrl 'Controller disconnected.' $amber
    Close-OperatorSession 'ctrl'
}
function Send-Loco([string]$code){
    if(-not $script:CtrlOn -or -not $script:CtrlProc -or $script:CtrlProc.HasExited){ Add-LogCtrl 'Not connected.' $amber; return }
    try{ $script:CtrlProc.StandardInput.Write($code + "`n"); Add-LogCtrl ("> "+$code) $accent }
    catch{ Add-LogCtrl ('send failed: '+$_.Exception.Message) $red }
}
function Set-MotionEnabled([bool]$on){ foreach($b in $script:MotionButtons){ $b.Enabled=$on } }

# ============================================================================
#  Follow marker (QR) - toggle-driven start/stop (mirrors Connect-Ctrl)
# ============================================================================
$followReader = {
    try{
        while(-not $followSync.Stop -and -not $proc.HasExited){
            $line=$proc.StandardOutput.ReadLine(); if($null -eq $line){ break }
            $followSync.Log.Enqueue($line)
        }
    }catch{ $followSync.Log.Enqueue('[follow reader err] '+$_.Exception.Message) }
    $followSync.Log.Enqueue('[follow process ended]')
}

# Lock out everything that could fight the follower for the single loco channel.
function Set-FollowLockout([bool]$following){
    $manual = -not $following
    $connCtrlBtn.Enabled = ($manual -and -not $script:CtrlOn)
    if($script:CtrlOn){
        $discCtrlBtn.Enabled=$manual; $gftBtn.Enabled=$manual
        $btnDamp.Enabled=$manual; $btnStop.Enabled=$manual
        if($manual){ Set-MotionEnabled ([bool]$armChk.Checked) } else { Set-MotionEnabled $false }
    }
    $armChk.Enabled = $manual
    $followDriveChk.Enabled = $manual    # can't switch mode mid-follow (toggle itself stays live to switch OFF)
    # SINGLE LOCO DRIVER RULE: no Tracker follow while the Control-tab follow runs.
    # Never ENABLE the Tracker controls if the Tracker itself is active (defensive).
    $trackIdle = $manual -and -not $script:TrackOn
    if($trackToggle){ $trackToggle.Enabled = $trackIdle }
    if($trackDriveChk){ $trackDriveChk.Enabled = $trackIdle }
    if($trackArmChk){ $trackArmChk.Enabled = $trackIdle }
}

function Start-Follow([bool]$drive){
    if($script:FollowOn){ return $false }
    if($script:TrackOn){
        [System.Windows.Forms.MessageBox]::Show("The Tracker (tab 6) follow is running. Stop it first - only one process may drive locomotion at a time.",'Follow blocked','OK','Warning')|Out-Null
        return $false
    }
    if($script:CtrlOn){
        [System.Windows.Forms.MessageBox]::Show("The manual controller is connected. Disconnect it first - only one process may drive locomotion at a time.",'Follow blocked','OK','Warning')|Out-Null
        return $false
    }
    if($drive){
        if(-not $armChk.Checked){
            [System.Windows.Forms.MessageBox]::Show("Tick 'ARM MOTION' (connection box) before Follow DRIVE.",'Not armed','OK','Warning')|Out-Null
            return $false
        }
        $r=[System.Windows.Forms.MessageBox]::Show("Start FOLLOW in DRIVE mode?`r`n`r`nMarker = one-time lock onto the person, then the K1 will PHYSICALLY WALK to follow THAT PERSON (re-show the marker to re-seed). It auto-stops after ~$($script:K1SessionCapSec)s (session cap), when the person is lost, or if the camera stalls. Clear the area and keep the e-stop handy.",'Confirm DRIVE follow',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -ne 'OK'){ return $false }
    }
    $ip=$ipCtrl.Text.Trim(); if(-not $ip){ Add-LogCtrl 'Enter the robot IP (connection box) first.' $amber; return $false }
    $script:RobotIP=$ip
    # Same pre-flight contract as Start-Tracker: signal the operator session before the first deploy
    # byte, and confirm no earlier follow node is running. Every abort below returns $false and the
    # toggle handler closes the session.
    Open-OperatorSession
    $old = Stop-RobotFollowNode $ip
    if($old -eq 'UNKNOWN'){ $old = Stop-RobotFollowNode $ip }   # one retry: a slow answer is not a refusal
    if($old -eq 'STILL-RUNNING'){ Add-LogCtrl ('An earlier follow node is still running on the robot 15 s after SIGTERM -- NOT starting a second one. If it does not exit, force it:  ssh {0}@{1} "pkill -KILL -f ''follow_person_k1\.py''"  then toggle Follow again. If the robot is moving, use the gamepad / e-stop.' -f $script:SshUser,$ip) $red; return $false }
    if($old -ne 'GONE'){ Add-LogCtrl ("Could not confirm that no follow node is running on {0} (the robot did not answer) -- NOT launching. Toggle Follow again." -f $ip) $red; return $false }
    # SINGLE CAMERA CONSUMER: stop the Live View stream (it also decodes the head
    # camera) so the camera is only ever streamed once.
    if($script:LiveOn){ Add-LogCtrl 'Stopping Live View (single camera consumer)...' $amber; Stop-Live }
    Add-LogCtrl ("Deploying follow helpers to {0} ..." -f $ip) $accent
    if(-not (Deploy-FollowFiles $ip)){ Add-LogCtrl ("Deploy failed: " + $(if($script:DeployErr){$script:DeployErr}else{"a helper under '$ROBOT_DIR' could not be deployed"})) $red; return $false }
    $followSync.Stop=$false; $followSync.Log.Clear()
    $mode = if($drive){'drive'}else{'preview'}
    # Council 2026-09-11 #6/#7: Control-tab DRIVE must satisfy the same P1.2 deadman gate as Tracker
    # (--require-heartbeat + HB relay) and drop a half-open SSH link fast (ServerAliveCountMax=1).
    # Without these, the node refused DRIVE (fail-closed) while the UI implied it worked, and a
    # dropped link could leave ~15s of walking on the default CountMax.
    $hbFlag = if($drive){' --require-heartbeat'}else{''}
    $remote="K1_MAX_SEC=$($script:K1SessionCapSec) bash /home/booster/run_follow.sh $mode /boostercamera/head/raw/rgb$hbFlag"
    $argStr='-tt '+(Get-SshOptString)+' -o ServerAliveCountMax=1'+" $($script:SshUser)@$ip `"$remote`""
    # Deadman HB: start the relay BEFORE the node so /tmp/k1_hb is fresh at the first gate check.
    if($drive){ Start-HbRelay $ip }
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardInput=$true; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogCtrl 'Failed to start ssh.' $red; if($drive){ Stop-HbRelay }; return $false }
    try{ $proc.StandardInput.AutoFlush=$true }catch{}
    try{ $proc.StandardInput.NewLine="`n" }catch{}
    $script:FollowProc=$proc
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('followSync',$followSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($followReader); [void]$ps.BeginInvoke()
    $script:FollowPS=$ps; $script:FollowRS=$rs
    $script:FollowOn=$true; $script:FollowDrive=$drive; $script:FollowStart=[datetime]::Now
    Set-FollowLockout $true
    if($drive){
        $followStatus.Text="FOLLOW: DRIVE - marker = one-time lock onto the person, then follows that person (re-show marker to re-seed). ~10s camera warmup. Auto-stop in ~$($script:K1SessionCapSec)s / person lost / camera stall."; $followStatus.ForeColor=$red
        Add-LogCtrl 'FOLLOW DRIVE started (deadman HB armed). Show the marker to lock onto the person, then the robot WALKS to follow THAT PERSON (re-show marker to re-seed). Toggle OFF halts + returns to PREP.' $red
        $statusLbl.Text="Follow DRIVE active: $ip"
    } else {
        $followStatus.Text='FOLLOW: PREVIEW - marker = one-time lock onto the person, then tracks that person (re-show marker to re-seed). No motion. (camera ~10s warmup)'; $followStatus.ForeColor=$green
        Add-LogCtrl 'FOLLOW PREVIEW started. Detect-only, no motion: show marker to lock onto the person, then person-track.' $green
        $statusLbl.Text="Follow preview active: $ip"
    }
    return $true
}

function Stop-Follow([bool]$procAlreadyDead=$false){
    if(-not $script:FollowOn){ return }
    Stop-HbRelay   # deadman relay dies with Control-tab follow (same contract as Tracker)
    $followSync.Stop=$true
    if(-not $procAlreadyDead){
        # Ctrl-C on the PTY -> SIGINT -> python finally -> bridge stop + ChangeMode(kPrepare).
        try{ if($script:FollowProc -and -not $script:FollowProc.HasExited){ $script:FollowProc.StandardInput.Write([char]3) } }catch{}
        try{ if($script:FollowProc){ $null=$script:FollowProc.WaitForExit(900) } }catch{}
    }
    try{ if($script:FollowProc -and -not $script:FollowProc.HasExited){ $script:FollowProc.Kill() } }catch{}
    try{ if($script:FollowPS){ $script:FollowPS.Dispose() } }catch{}
    try{ if($script:FollowRS){ $script:FollowRS.Close() } }catch{}
    $script:FollowProc=$null; $script:FollowPS=$null; $script:FollowRS=$null
    $script:FollowOn=$false; $script:FollowDrive=$false
    Set-FollowLockout $false                              # restore manual controls
    if($armChk.Checked){ $armChk.Checked=$false }         # force re-ARM before any future DRIVE
    # reset the toggle UI without re-entering its handler
    $script:FollowToggleGuard=$true; $followToggle.Checked=$false; $script:FollowToggleGuard=$false
    $followToggle.Text='Follow Marker (QR): OFF'; $followToggle.UseVisualStyleBackColor=$true; $followToggle.ForeColor=[System.Drawing.SystemColors]::ControlText
    # Confirm on the robot (belt-and-braces SIGTERM after the Ctrl-C), and say what actually happened.
    $gone='UNKNOWN'; $ipF=''; try{ $ipF=$ipCtrl.Text.Trim() }catch{}
    if($ipF){ $gone = Stop-RobotFollowNode $ipF }
    if($gone -eq 'GONE'){
        $followStatus.Text='Follow stopped. No follow node left on the robot (checked).'
        Add-LogCtrl 'Follow stopped (Ctrl-C sent; no follow node left on the robot -- checked).' $amber
    } else {
        $followStatus.Text='Follow stop NOT confirmed -- check the robot.'
        Add-LogCtrl ('Follow stop NOT confirmed ({0}). If the robot is moving, use the gamepad / e-stop.' -f $(if($ipF){$gone}else{'no robot IP'})) $red
    }
    $followStatus.ForeColor=[System.Drawing.Color]::DimGray
    $statusLbl.Text='Follow stopped.'
    Close-OperatorSession   # session over -> the Auto-Tune loop may use the link again (shared contract)
}

# ============================================================================
#  UI media/log poll timer (live + control)
# ============================================================================
$mediaTimer=New-Object System.Windows.Forms.Timer; $mediaTimer.Interval=40
$mediaTimer.Add_Tick({
    # Deadman-HB byte pump (~25Hz at the 40ms tick): each byte makes the remote loop touch the
    # hb file once. A UI-thread freeze stops the pump -> deadman trips (intended: a frozen
    # operator app is NOT a live operator). Dead/failed relay writes are swallowed -- the node
    # is already fail-closed without fresh touches.
    if($script:HbProc){ try{ if(-not $script:HbProc.HasExited){ $script:HbProc.StandardInput.WriteLine('h') } }catch{} }
    # Control log drain (skip the PTY's echo of our own command codes)
    while($ctrlSync.Log.Count -gt 0){ $ln=$ctrlSync.Log.Dequeue(); if($ln){ $t=$ln.Trim(); if(-not $script:CtrlCmdSet[$t]){ Add-LogCtrl $ln } } }
    # Follow-marker log drain + UI-side completion/watchdog (mediaTimer = UI thread)
    while($followSync.Log.Count -gt 0){
        $fl=$followSync.Log.Dequeue()
        if($fl){
            $c=[System.Drawing.Color]::Gainsboro
            if($fl -like '*LOST*' -or $fl -like '*STALL*' -or $fl -like '*ERR*' -or $fl -like '*FAIL*' -or $fl -like '*ABORT*' -or $fl -like '*NO-FRAME*'){ $c=$amber }
            elseif($fl -like 'OK *' -or $fl -like 'MARK *' -or $fl -like '*DRIVE-ACTIVE*'){ $c=$green }
            Add-LogCtrl $fl $c
        }
    }
    if($script:FollowOn){
        if($script:FollowProc -and $script:FollowProc.HasExited){
            $ecF=$null; try{ $ecF=[int]$script:FollowProc.ExitCode }catch{}
            if($ecF -eq 5){ Add-LogCtrl 'Follow process exited 5 = REFUSED: a follow node was already running on the robot (or pgrep failed), so run_follow.sh did not start a second one. The cleanup below sends it SIGTERM; toggle Follow again in a few seconds.' $red }
            elseif($ecF -eq 3){ Add-LogCtrl 'Follow process exited 3 = COMPILE FAILED (run_follow.sh) -- see /home/booster/k1_compile.err on the robot.' $red }
            elseif($ecF -eq 4){ Add-LogCtrl 'Follow process exited 4 = DRIVE-ABORT (the node refused to walk; most often a stalled camera).' $amber }
            else { Add-LogCtrl ("Follow process exited ({0}); cleaning up." -f $(if($ecF -eq $null){'code unavailable'}else{$ecF})) $amber }
            Stop-Follow $true   # proc already dead: skip the SIGINT write + long wait (no UI stall)
        }
        elseif($script:FollowDrive -and (([datetime]::Now - $script:FollowStart).TotalSeconds -gt $script:FollowMaxSec)){
            Add-LogCtrl ("Follow session watchdog ({0}s) - stopping." -f $script:FollowMaxSec) $red
            Stop-Follow $false
        }
    }
    # Robot Files: refresh progress + completion
    if($filesSync.Running){
        while($filesSync.Log.Count -gt 0){ $ln=$filesSync.Log.Dequeue(); if($ln){ if($ln -like 'ERROR:*'){ Add-LogFiles $ln $red } else { Add-LogFiles $ln ([System.Drawing.Color]::Gainsboro) } } }
        if($filesSync.Done){ $filesSync.Running=$false; Complete-RefreshRobotFiles }
    }
    # Robot Files: file-view completion
    if($viewSync.Running -and $viewSync.Done){ $viewSync.Running=$false; Complete-ViewRemoteFile }
    # --- Tracker: process-exit + DRIVE watchdog (UI thread) -----------------
    if($script:TrackOn){
        if($script:TrackProc -and $script:TrackProc.HasExited){
            # ITEM 9: diagnose WHY the follow ssh exited. $script:TrackProc is a raw System.Diagnostics.Process
            # (New-Object + .Start()), so .ExitCode is reliable here after .HasExited. run_follow.sh exits 3 on
            # 'COMPILE FAILED'; other non-zero = DRIVE-ABORT / node crash; 0 = normal stop / our own kill.
            $ec = $null
            try{ $ec = [int]$script:TrackProc.ExitCode }catch{ $ec = $null }
            if($ec -eq $null){
                Add-LogTrack 'Follow process exited (exit code unavailable); cleaning up.' $amber
            }
            elseif($ec -eq 0){
                Add-LogTrack 'Follow process exited (0, normal stop); cleaning up.' $amber
            }
            else{
                # exit 3 = bridge COMPILE FAILED (diagnostics in k1_compile.err -- k1_follow.err
                # holds the PREVIOUS session at compile time); exit 4 = node REFUSED to drive
                # (DRIVE-ABORT, deliberate); exit 5 = run_follow.sh REFUSED a second node; anything else = crash.
                $tailFile = '/home/booster/k1_follow.err'
                if($ec -eq 3){ $tailFile='/home/booster/k1_compile.err'; Add-LogTrack 'Follow process exited 3 = COMPILE FAILED (run_follow.sh). See k1_compile.err tail below.' $red }
                elseif($ec -eq 5){ Add-LogTrack 'Follow process exited 5 = REFUSED: a follow node was already running on the robot (or pgrep failed), so run_follow.sh did not start a second one. The tail below is from THAT node; the cleanup below sends it SIGTERM. Toggle Follow again in a few seconds.' $red }
                elseif($ec -eq 4){
                    Add-LogTrack 'Follow process exited 4 = DRIVE-ABORT (node refused to walk -- see the amber DRIVE-ABORT line above and the tail below).' $amber
                    # Most DRIVE-ABORTs are stalled cameras (sensors not sustained-fresh). Point the
                    # operator at the recovery tool rather than auto-running it: this handler is on the
                    # UI thread and cam_health.sh --recover takes ~60-90s (would freeze the app), and a
                    # safe background+relaunch version needs async UI work + on-robot testing (deferred).
                    Add-LogTrack ('If the tail shows rgb/depth fps ~0 -> cameras stalled. Recover:  ssh {0}@{1} "bash /home/booster/cam_health.sh --recover"  then toggle Follow again.' -f $script:SshUser, $(try{$ipTrack.Text.Trim()}catch{'<ip>'})) $accent
                }
                else{ Add-LogTrack "Follow process exited $ec (crash). See k1_follow.err tail below." $red }
                # Bounded ssh tail of the remote stderr log into the Tracker log (red). Own System.Diagnostics.Process
                # with RedirectStandardOutput + WaitForExit(4000)+Kill so the UI thread can never hang. Reuses
                # $SSH_OPTS / $script:SshUser like the pkill paths (lines 1016/1079).
                $ipErr=''
                try{ $ipErr=$ipTrack.Text.Trim() }catch{}
                if($ipErr){
                    try{
                        $tpsi=New-Object System.Diagnostics.ProcessStartInfo
                        $tpsi.FileName='ssh.exe'
                        $tpsi.Arguments=(Get-SshOptString)+" $($script:SshUser)@$ipErr `"tail -n 20 $tailFile 2>/dev/null`""
                        $tpsi.UseShellExecute=$false; $tpsi.RedirectStandardOutput=$true; $tpsi.RedirectStandardError=$false; $tpsi.CreateNoWindow=$true
                        $tproc=New-Object System.Diagnostics.Process; $tproc.StartInfo=$tpsi
                        if($tproc.Start()){
                            # WAIT (bounded) BEFORE reading: ReadToEnd() blocks until the stream CLOSES, so
                            # calling it first would hang the UI thread forever on a wedged ssh (the 4s bound
                            # below would never be reached). A 20-line tail is far below the pipe buffer, so
                            # wait-then-read cannot deadlock; after Kill the pipe closes and the read returns.
                            if(-not $tproc.WaitForExit(4000)){ try{ $tproc.Kill() }catch{}; $null=$tproc.WaitForExit(1500) }
                            $tout=$tproc.StandardOutput.ReadToEnd()
                            if($tout){
                                $tailName = Split-Path $tailFile -Leaf
                                foreach($ln in ($tout -split "`r?`n")){ if($ln.Trim().Length -gt 0){ Add-LogTrack ("$tailName> "+$ln) $red } }
                            } else {
                                Add-LogTrack 'k1_follow.err> (empty or unreadable).' $amber
                            }
                        } else {
                            Add-LogTrack 'k1_follow.err> could not start ssh to fetch tail.' $amber
                        }
                    }catch{ Add-LogTrack ("k1_follow.err tail failed: $_") $amber }
                } else {
                    Add-LogTrack 'k1_follow.err> no robot IP available to fetch tail.' $amber
                }
            }
            Stop-Tracker $true   # proc already dead
        }
        elseif($script:TrackDrive -and (([datetime]::Now - $script:TrackStart).TotalSeconds -gt $script:TrackMaxSec)){
            Add-LogTrack ("Follow session watchdog ({0}s) - stopping." -f $script:TrackMaxSec) $red
            Stop-Tracker $false
        }
    }
    # --- Tracker: annotated frame + state badge + chime + status-transition log ---
    if($script:TrackOn -and $trackSync.ErrLines){
        $el=$null
        while($trackSync.ErrLines.TryDequeue([ref]$el)){
            Update-ReidBadge $el                                 # REID-* lines drive the persistent health badge
            Add-LogTrack $el (Get-TrackLineColor $el)             # amber/red for fault families, $accent otherwise
        }
    }
    if($script:TrackOn -and $trackSync.Seq -ne $script:trackLastSeq){
        $script:trackLastSeq=$trackSync.Seq
        try{
            $bytes=$trackSync.Jpeg
            if($bytes){
                $ms=New-Object System.IO.MemoryStream(,$bytes)
                $img=[System.Drawing.Image]::FromStream($ms)
                $old=$trackPic.Image; $oldMs=$script:trackMs
                $trackPic.Image=$img; $script:trackMs=$ms
                if($old){$old.Dispose()}; if($oldMs){$oldMs.Dispose()}
            }
        }catch{}
        $tk=[int]$trackSync.Lock
        if($tk -ne $script:trackLastLock){
            Set-TrackBadge $tk
            if($tk -eq 2 -and $script:trackLastLock -ne 2 -and -not $trackMuteChk.Checked){
                try{ [System.Media.SystemSounds]::Asterisk.Play() }catch{}
            }
            if($tk -ne 2 -and $script:trackLastLock -eq 2 -and -not $trackMuteChk.Checked){
                try{ [System.Media.SystemSounds]::Hand.Play() }catch{}   # lost the person
            }
            if($script:trackLastLock -ge 0){
                $msg='-> SEARCHING'; $c=[System.Drawing.Color]::Gainsboro
                switch($tk){
                    0 { $msg='-> SEARCHING';        $c=[System.Drawing.Color]::Gainsboro }
                    1 { $msg='-> ACQUIRING MARKER'; $c=$amber }
                    2 { $msg='-> LOCKED, FOLLOWING'; $c=$green }
                    3 { $msg='-> LOST, show marker'; $c=$red }
                }
                Add-LogTrack $msg $c
            }
            $script:trackLastLock=$tk
        }
    }
    # Live frame update
    if($script:LiveOn -and $liveSync.Seq -ne $script:lastSeq){
        $script:lastSeq=$liveSync.Seq
        try{
            $bytes=$liveSync.Jpeg
            if($bytes){
                $ms=New-Object System.IO.MemoryStream(,$bytes)
                $img=[System.Drawing.Image]::FromStream($ms)
                $old=$livePic.Image; $oldMs=$script:liveMs
                $livePic.Image=$img; $script:liveMs=$ms
                if($old){$old.Dispose()}; if($oldMs){$oldMs.Dispose()}
            }
        }catch{}
        # marker lock-on badge + audio cue on transition into LOCKED
        $lk=[int]$liveSync.Lock
        if($lk -ne $script:lastLock){
            if($lk -eq 2){ $lockBadge.Text='  MARKER LOCKED - READY  '; $lockBadge.BackColor=$green }
            elseif($lk -eq 1){ $lockBadge.Text='  acquiring marker...  '; $lockBadge.BackColor=$amber }
            else { $lockBadge.Text='  searching - no marker  '; $lockBadge.BackColor=[System.Drawing.Color]::Gray }
            if($lk -eq 2 -and $script:lastLock -ne 2 -and -not $muteChk.Checked){
                try{ [System.Media.SystemSounds]::Asterisk.Play() }catch{}
            }
            if($lk -ne 2 -and $script:lastLock -eq 2 -and -not $muteChk.Checked){
                try{ [System.Media.SystemSounds]::Hand.Play() }catch{}   # lock lost
            }
            $script:lastLock=$lk
        }
    }
})
# FPS / status (1s)
$fpsTimer=New-Object System.Windows.Forms.Timer; $fpsTimer.Interval=1000
$fpsTimer.Add_Tick({
    if($script:LiveOn){
        $f=$liveSync.Frames; $d=$f-$script:liveFpsFrames; $script:liveFpsFrames=$f
        if($f -gt 0){ $liveStatus.Text=("LIVE  {0}  -  {1} fps  ({2} frames)" -f $ipLive.Text,$d,$f); $liveStatus.ForeColor=$green }
        else { $liveStatus.Text="Waiting for camera frames... (camera may be idle)"; $liveStatus.ForeColor=$amber }
        if($liveSync.Done -and $f -eq 0){ $liveStatus.Text="Stream ended with no frames (camera idle / topic empty)." }
    }
    # Tracker FPS heartbeat in the status strip
    if($script:TrackOn){
        $tf=$trackSync.Frames; $td=$tf-$script:trackFpsFrames; $script:trackFpsFrames=$tf
        if($tf -gt 0){ $statusLbl.Text=("Tracker {0}  -  {1} fps  ({2} frames)" -f $ipTrack.Text.Trim(),$td,$tf) }
        elseif($trackSync.Done){ $statusLbl.Text='Tracker stream ended with no frames (camera idle / topic empty).' }
        else { $statusLbl.Text='Tracker: waiting for first annotated frame (~10s camera warmup)...' }
    }
    # Control: prime DDS discovery a few seconds after connect so the first real command responds
    if($script:CtrlPrime -gt 0 -and $script:CtrlOn){
        $script:CtrlPrime--
        if($script:CtrlPrime -le 0){
            Add-LogCtrl 'Priming link (gft) to complete DDS discovery...' $accent
            Send-Loco 'gft'
            $connStatus.Text='Link primed. Test link (gft) should now return pos/ori. ARM to enable motion.'
            $statusLbl.Text='Controller ready.'
        }
    }
})

# ============================================================================
#  Discover poll timer
# ============================================================================
$timer=New-Object System.Windows.Forms.Timer; $timer.Interval=250
$timer.Add_Tick({
    while($sync.Log.Count -gt 0){ $line=$sync.Log.Dequeue(); if($line){ Add-Log $line } }
    if($sync.Subnets -and $subnetLbl.Text -notmatch [regex]::Escape($sync.Subnets)){ $subnetLbl.Text='Subnets: '+$sync.Subnets }
    if($sync.Total -gt 0){ $progress.Maximum=$sync.Total; $progress.Value=[Math]::Min($sync.Progress,$sync.Total) }
    while($script:renderedCount -lt $sync.Results.Count){
        $r=$sync.Results[$script:renderedCount]
        $item=New-Object System.Windows.Forms.ListViewItem($r.Label); $item.UseItemStyleForSubItems=$false; $item.ForeColor=(Confidence-Color $r.Label); $item.Font=$fontBold
        [void]$item.SubItems.Add($r.IP);[void]$item.SubItems.Add($r.Hostname);[void]$item.SubItems.Add($r.Banner);[void]$item.SubItems.Add($r.Why); $item.Tag=$r
        [void]$list.Items.Add($item); $script:renderedCount++
    }
    if($sync.Done -and $sync.Running){
        $sync.Running=$false; $scanBtn.Enabled=$true; $stopBtn.Enabled=$false; $progress.Value=$progress.Maximum
        $rows=@($list.Items | ForEach-Object { $_ }); $sorted=$rows | Sort-Object { -1*($_.Tag.Score) }
        $list.BeginUpdate(); $list.Items.Clear(); foreach($it in $sorted){ [void]$list.Items.Add($it) }; $list.EndUpdate()
        if($list.Items.Count -gt 0){ $list.Items[0].Selected=$true; $top=$list.Items[0].Tag
            if($top.Label -eq 'High'){ $statusLbl.Text=("Likely K1: {0} ({1})" -f $top.IP,$top.Why) } else { $statusLbl.Text=("Scan done. Best guess: {0}" -f $top.IP) } }
        else { $statusLbl.Text='Scan done. No SSH hosts found.' }
        try{ $sync.PS.EndInvoke($sync.Handle) }catch{}; try{ $sync.PS.Runspace.Close(); $sync.PS.Dispose() }catch{}
    }
})

# ============================================================================
#  Verify / Connect (Discover) + SSH&Files actions
# ============================================================================
function Get-ConnectRecipe([string]$ip){ @"
================  K1 CONNECTION RECIPE  ================
Target robot IP : $ip
1) SSH:  ssh $K1_SSH_USER@$ip   (SSH key auth)
2) SDK over Fast-DDS connects by robot IP; on-robot loco iface = 127.0.0.1.
   Loco CLI: ~/Workspace/booster_robotics_sdk/build/b1_loco_example_client 127.0.0.1
3) Live camera topic: /boostercamera/head/rgb (sensor_msgs/Image).
=======================================================
"@ }
function Do-Verify([string]$ip){
    if(-not $ip){ [System.Windows.Forms.MessageBox]::Show('No IP.','K1 Finder','OK','Warning')|Out-Null; return }
    $statusLbl.Text="Verifying $ip ..."; Add-Log ("--- Verifying {0} ---" -f $ip) $accent; $form.Cursor='WaitCursor'
    $r=Test-K1Reachable $ip; $form.Cursor='Default'
    Add-Log ("  Ping     : {0}" -f $(if($r.Ping){'reachable'}else{'no reply'}))
    Add-Log ("  SSH (22) : {0}" -f $(if($r.SSH){'OPEN'}else{'closed'}))
    if($r.Banner){Add-Log ("  Banner   : {0}" -f $r.Banner)}; if($r.Hostname){Add-Log ("  Hostname : {0}" -f $r.Hostname)}
    if($r.SSH){ Set-Content -Path $LAST_TARGET_FILE -Value $ip -Encoding ASCII; Set-RobotIP $ip; Add-Log (Get-ConnectRecipe $ip) ([System.Drawing.Color]::PaleGreen); $statusLbl.Text="Reachable: $ip (SSH open). IP applied to all tabs." }
    else{ $statusLbl.Text="No SSH on $ip."; [System.Windows.Forms.MessageBox]::Show("SSH not open on $ip. Is the K1 on this network and powered on?",'Not reachable','OK','Warning')|Out-Null }
}
function Open-SshTerminal { $ip=$ip2Box.Text.Trim(); if(-not $ip){return}; Add-Log2 ("Opening SSH terminal to {0}@{1}" -f $K1_SSH_USER,$ip) $accent; Start-Process cmd.exe -ArgumentList '/k',"ssh -o StrictHostKeyChecking=accept-new $K1_SSH_USER@$ip" }
function Setup-SshKey {
    $ip=$ip2Box.Text.Trim(); if(-not $ip){return}
    Add-Log2 ("Installing SSH key on {0} (type the robot password once in the console)..." -f $ip) $accent
    $c='if not exist "%USERPROFILE%\.ssh\id_ed25519" ssh-keygen -t ed25519 -f "%USERPROFILE%\.ssh\id_ed25519" -N "" -q'
    $c+=' & type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh -o StrictHostKeyChecking=accept-new '+"$K1_SSH_USER@$ip"+' "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys && echo === KEY INSTALLED ==="'
    $c+=' & echo. & echo Done - you can close this window.'
    Start-Process cmd.exe -ArgumentList '/k',$c
}
function Test-Ssh2 { $ip=$ip2Box.Text.Trim(); if(-not $ip){return}; Add-Log2 ("Testing SSH on {0}..." -f $ip) $accent; $form.Cursor='WaitCursor'; $r=Test-K1Reachable $ip; $form.Cursor='Default'; Add-Log2 ("  Ping {0} / SSH {1}" -f $(if($r.Ping){'ok'}else{'no'}),$(if($r.SSH){'OPEN'}else{'closed'})); if($r.Banner){Add-Log2 ("  "+$r.Banner)} }
function Add-UploadFiles { $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect=$true; if($d.ShowDialog() -eq 'OK'){ foreach($f in $d.FileNames){ if(-not $fileList.Items.Contains($f)){[void]$fileList.Items.Add($f)} } } }
function Add-UploadFolder { $d=New-Object System.Windows.Forms.FolderBrowserDialog; if($d.ShowDialog() -eq 'OK'){ if(-not $fileList.Items.Contains($d.SelectedPath)){[void]$fileList.Items.Add($d.SelectedPath)} } }
function Do-Upload {
    $ip=$ip2Box.Text.Trim(); if(-not $ip){return}
    if($fileList.Items.Count -eq 0){ [System.Windows.Forms.MessageBox]::Show('Add files first.','K1 Finder','OK','Warning')|Out-Null; return }
    $remote=$remoteBox.Text.Trim(); if(-not $remote){$remote='/home/booster/'}
    $files=@($fileList.Items | ForEach-Object { [string]$_ }); $spec=('{0}@{1}:{2}' -f $K1_SSH_USER,$ip,$remote)
    if($pwlessChk.Checked){
        Add-Log2 ("--- Uploading {0} item(s) to {1} (passwordless) ---" -f $files.Count,$spec) $accent; $statusLbl.Text="Uploading to $ip..."; $form.Cursor='WaitCursor'
        $argList=@('-o','StrictHostKeyChecking=accept-new','-o','BatchMode=yes','-r')+$files+@($spec); $out=& scp.exe @argList 2>&1; $code=$LASTEXITCODE; $form.Cursor='Default'
        foreach($o in $out){ Add-Log2 ("  "+$o) }
        if($code -eq 0){ Add-Log2 ("Upload complete -> {0}" -f $spec) $green } else { Add-Log2 ("Upload failed (exit $code). Install the key or untick Passwordless.") $red }
    } else {
        $fileArgs=($files | ForEach-Object { '"'+$_+'"' }) -join ' '
        $cmd="scp -o StrictHostKeyChecking=accept-new -r $fileArgs "+'"'+$spec+'"'+' & echo. & echo [Done]'
        Add-Log2 "Launching scp in a console (type the robot password if asked)..." $accent; Start-Process cmd.exe -ArgumentList '/k',$cmd
    }
}

# ============================================================================
#  Robot Files - refresh: deploy tree_manifest.py, run it, pull the 3 files
# ============================================================================
# Runs ENTIRELY on a runspace thread: NO WinForms calls here. Inherits the
# global SSH_ASKPASS env, so scp/ssh run passwordless exactly like Deploy-RobotFiles.
$filesWorker = {
    function _Stage($s){ $filesSync.Stage=$s; $filesSync.Log.Enqueue($s) }
    # Start-Process -PassThru ExitCode is unreliable under SilentlyContinue (returns
    # $null), so success is judged by side-effects: the 3 files must land non-empty.
    # Clear stale copies first so a failed pull cannot masquerade as success.
    $targets = @('k1_tree.txt','k1_paths.json','k1_paths_list.txt')
    try {
        foreach($f in $targets){ $dst=Join-Path $work $f; if(Test-Path $dst){ Remove-Item $dst -Force -ErrorAction SilentlyContinue } }

        _Stage 'Uploading tree_manifest.py to /home/booster ...'
        $optsUp = $SSH_OPTS + @($manifestSrc, ("{0}@{1}:/home/booster/tree_manifest.py" -f $user,$ip))
        $p = Start-Process scp.exe -ArgumentList $optsUp -NoNewWindow -PassThru
        $null = $p.WaitForExit(25000)
        if(-not $p.HasExited){ try{$p.Kill()}catch{}; throw 'scp upload timed out (check IP / SSH).' }

        _Stage 'Running python3 /home/booster/tree_manifest.py on the robot ...'
        $optsRun = $SSH_OPTS + @(("{0}@{1}" -f $user,$ip), 'python3 /home/booster/tree_manifest.py')
        $p = Start-Process ssh.exe -ArgumentList $optsRun -NoNewWindow -PassThru
        $null = $p.WaitForExit(40000)
        if(-not $p.HasExited){ try{$p.Kill()}catch{}; throw 'tree_manifest.py timed out on robot.' }

        foreach($f in $targets){
            _Stage ("Pulling {0} ..." -f $f)
            $optsDn = $SSH_OPTS + @(("{0}@{1}:/home/booster/{2}" -f $user,$ip,$f), (Join-Path $work $f))
            $p = Start-Process scp.exe -ArgumentList $optsDn -NoNewWindow -PassThru
            $null = $p.WaitForExit(25000)
            if(-not $p.HasExited){ try{$p.Kill()}catch{}; throw ("scp of {0} timed out." -f $f) }
        }
        # Verify all three arrived non-empty (the real success signal).
        foreach($f in $targets){
            $dst=Join-Path $work $f
            if(-not (Test-Path $dst)){ throw ("{0} did not transfer - check IP / SSH / that python3 ran." -f $f) }
            if((Get-Item $dst).Length -le 0){ throw ("{0} is empty - tree_manifest.py may have failed on the robot." -f $f) }
        }
        $filesSync.Ok=$true
        _Stage 'Manifest pulled. Parsing...'
    } catch {
        $filesSync.Ok=$false
        $filesSync.Log.Enqueue('ERROR: '+$_.Exception.Message)
    } finally {
        $filesSync.Done=$true
    }
}

function Refresh-RobotFiles {
    if($filesSync.Running){ return }
    $ip=$ipFiles.Text.Trim()
    if(-not $ip){ Add-LogFiles 'Enter the robot IP first.' $amber; return }
    Set-RobotIP $ip   # keep all tabs in sync
    $manifestSrc = Join-Path $ROBOT_DIR 'tree_manifest.py'
    if(-not (Test-Path $manifestSrc)){ Add-LogFiles ('Missing helper: '+$manifestSrc) $red; $filesStatus.Text='tree_manifest.py not found next to the app.'; $filesStatus.ForeColor=$red; return }

    $previewBox.Clear()
    Add-LogFiles ('--- Refreshing robot file manifest from {0} ---' -f $ip) $accent
    $filesSync.Running=$true; $filesSync.Done=$false; $filesSync.Ok=$false; $filesSync.Stage=''; $filesSync.Log.Clear()
    $refreshFilesBtn.Enabled=$false; $filesStatus.Text='Working... deploying + running tree_manifest.py'; $filesStatus.ForeColor=$amber; $form.Cursor='AppStarting'
    $statusLbl.Text="Robot Files: refreshing from $ip ..."

    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('filesSync',$filesSync)
    $rs.SessionStateProxy.SetVariable('SSH_OPTS',$SSH_OPTS)
    $rs.SessionStateProxy.SetVariable('ip',$ip)
    $rs.SessionStateProxy.SetVariable('user',$script:SshUser)
    $rs.SessionStateProxy.SetVariable('manifestSrc',$manifestSrc)
    $rs.SessionStateProxy.SetVariable('work',$WORK)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($filesWorker)
    $filesSync.PS=$ps; $filesSync.RS=$rs; $filesSync.Handle=$ps.BeginInvoke()
}

# Called ON THE UI THREAD by $mediaTimer when the worker finishes.
function Complete-RefreshRobotFiles {
    try{ if($filesSync.PS){ $filesSync.PS.EndInvoke($filesSync.Handle) } }catch{}
    try{ if($filesSync.RS){ $filesSync.RS.Close() } }catch{}
    try{ if($filesSync.PS){ $filesSync.PS.Dispose() } }catch{}
    $filesSync.PS=$null; $filesSync.RS=$null; $filesSync.Handle=$null
    $refreshFilesBtn.Enabled=$true; $form.Cursor='Default'
    if($filesSync.Ok){
        $listFile=Join-Path $WORK 'k1_paths_list.txt'
        $jsonFile=Join-Path $WORK 'k1_paths.json'
        $n1=Build-RobotTree $listFile $filesTree
        $n2=Load-KeyPaths   $jsonFile $keysList
        $filesStatus.Text=("Loaded {0} tree nodes, {1} key paths from {2}." -f $n1,$n2,$ipFiles.Text.Trim()); $filesStatus.ForeColor=$green
        $statusLbl.Text="Robot Files: manifest loaded ($n1 nodes)."
        Add-LogFiles ("Done. {0} paths in tree, {1} keys. Double-click a FILE node (or an OK key row) to view it." -f $n1,$n2) $green
    } else {
        $filesStatus.Text='Refresh failed - see preview log. Verify the robot IP/SSH on the Discover tab.'; $filesStatus.ForeColor=$red
        $statusLbl.Text='Robot Files: refresh failed.'
    }
}

# ============================================================================
#  Robot Files - build TreeView from k1_paths_list.txt ("DIR|<path>"/"FILE|<path>")
# ============================================================================
function Build-RobotTree {
    param([string]$listFile,$treeView)
    if(-not (Test-Path $listFile)){ return 0 }
    $treeView.BeginUpdate()
    $treeView.Nodes.Clear()
    $script:_rtMap = @{}   # full-path (string) -> TreeNode

    function _EnsureDir([string]$full,$tv){
        if($script:_rtMap.ContainsKey($full)){ return $script:_rtMap[$full] }
        $slash = $full.LastIndexOf('/')
        $name = if($slash -ge 0){ $full.Substring($slash+1) } else { $full }
        if(-not $name){ $name=$full }
        $parent = if($slash -gt 0){ $full.Substring(0,$slash) } else { '' }
        $node = New-Object System.Windows.Forms.TreeNode($name)
        $node.Tag = 'DIR:'+$full
        $node.ForeColor = $dark
        if($parent -and ($parent -ne $full)){
            $pnode = _EnsureDir $parent $tv
            [void]$pnode.Nodes.Add($node)
        } else {
            [void]$tv.Nodes.Add($node)
        }
        $script:_rtMap[$full] = $node
        return $node
    }

    $count = 0
    foreach($raw in [System.IO.File]::ReadAllLines($listFile)){
        $line = $raw.Trim(); if(-not $line){ continue }
        $bar = $line.IndexOf('|'); if($bar -lt 0){ continue }
        $kind = $line.Substring(0,$bar)
        $full = $line.Substring($bar+1).TrimEnd('/')
        if(-not $full){ continue }
        if($kind -eq 'DIR'){
            [void](_EnsureDir $full $treeView)
        } elseif($kind -eq 'FILE'){
            if($script:_rtMap.ContainsKey($full)){ continue }
            $slash = $full.LastIndexOf('/')
            $name = if($slash -ge 0){ $full.Substring($slash+1) } else { $full }
            $parent = if($slash -gt 0){ $full.Substring(0,$slash) } else { '' }
            $node = New-Object System.Windows.Forms.TreeNode($name)
            $node.Tag = 'FILE:'+$full
            $node.ForeColor = $accent
            if($parent){ $pnode = _EnsureDir $parent $treeView; [void]$pnode.Nodes.Add($node) }
            else { [void]$treeView.Nodes.Add($node) }
            $script:_rtMap[$full] = $node
        } else { continue }
        $count++
    }

    # Expand /home -> booster and surface the SDK root so the tree isn't a single collapsed node.
    foreach($r in $treeView.Nodes){
        $r.Expand()
        foreach($c in $r.Nodes){ $c.Expand() }
    }
    $sdk='/home/booster/Workspace/booster_robotics_sdk'
    if($script:_rtMap.ContainsKey($sdk)){
        $script:_rtMap[$sdk].EnsureVisible()
        $treeView.SelectedNode=$script:_rtMap[$sdk]
    }
    $treeView.EndUpdate()
    return $count
}

# ============================================================================
#  Robot Files - fill key-paths ListView from k1_paths.json
# ============================================================================
function Load-KeyPaths {
    param([string]$jsonFile,$listView)
    if(-not (Test-Path $jsonFile)){ return 0 }
    $json = $null
    try { $json = (Get-Content -Raw -Path $jsonFile) | ConvertFrom-Json } catch { return 0 }
    if(-not $json -or -not $json.paths){ return 0 }

    $script:RobotPaths = @{}   # refresh the self-correct cache
    $listView.BeginUpdate(); $listView.Items.Clear()
    $n = 0
    foreach($prop in $json.paths.PSObject.Properties){
        $key  = $prop.Name
        $path = [string]$prop.Value
        $exists = $false
        if($json.exists -and ($json.exists.PSObject.Properties.Name -contains $key)){ $exists = [bool]$json.exists.$key }

        $isTopic = ($key -eq 'camera_topic_head_rgb')
        $isEmpty = (-not $path)

        # cache real filesystem paths only (skip empty + topic)
        if($path -and (-not $isTopic)){ $script:RobotPaths[$key]=$path }

        $item = New-Object System.Windows.Forms.ListViewItem($key)
        $item.UseItemStyleForSubItems = $true
        [void]$item.SubItems.Add($(if($isEmpty){'(not built)'}else{$path}))
        if($isEmpty){
            [void]$item.SubItems.Add('n/a'); $item.ForeColor=[System.Drawing.Color]::DimGray; $item.Tag=''
        } elseif($isTopic){
            [void]$item.SubItems.Add('topic'); $item.ForeColor=$amber; $item.Tag=''
        } elseif($exists){
            [void]$item.SubItems.Add('OK'); $item.ForeColor=$green; $item.Tag=$path
        } else {
            [void]$item.SubItems.Add('MISSING'); $item.ForeColor=$red; $item.Tag=$path
        }
        [void]$listView.Items.Add($item)
        $n++
    }
    $listView.EndUpdate()
    return $n
}

# ============================================================================
#  Robot Files - view a remote file (scp into $WORK, show first ~400 lines)
# ============================================================================
$viewWorker = {
    try{
        $local = Join-Path $work ('view_'+([System.IO.Path]::GetFileName($remote)))
        if(Test-Path $local){ Remove-Item $local -Force -ErrorAction SilentlyContinue }
        $opts = $SSH_OPTS + @(("{0}@{1}:{2}" -f $user,$ip,$remote), $local)
        $p = Start-Process scp.exe -ArgumentList $opts -NoNewWindow -PassThru
        $null = $p.WaitForExit(30000)
        if(-not $p.HasExited){ try{$p.Kill()}catch{}; throw 'scp timed out.' }
        if(-not (Test-Path $local)){ throw 'file did not transfer (check path / SSH).' }
        $viewSync.Local=$local
        $viewSync.Size=(Get-Item $local).Length
        $viewSync.Ok=$true
    } catch {
        $viewSync.Ok=$false; $viewSync.Msg=$_.Exception.Message
    } finally {
        $viewSync.Done=$true
    }
}

function View-RemoteFile {
    param([string]$remote)
    if($viewSync.Running){ return }
    if(-not $remote){ return }
    $ip=$ipFiles.Text.Trim(); if(-not $ip){ Add-LogFiles 'Enter the robot IP first.' $amber; return }
    $previewBox.Clear()
    Add-LogFiles ('Fetching '+$remote+' ...') $accent
    $grpPreview.Text='File preview: '+$remote
    $viewSync.Running=$true; $viewSync.Done=$false; $viewSync.Ok=$false; $viewSync.Path=$remote; $viewSync.Local=''; $viewSync.Size=0; $viewSync.Msg=''
    $form.Cursor='AppStarting'

    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('viewSync',$viewSync)
    $rs.SessionStateProxy.SetVariable('SSH_OPTS',$SSH_OPTS)
    $rs.SessionStateProxy.SetVariable('ip',$ip)
    $rs.SessionStateProxy.SetVariable('user',$script:SshUser)
    $rs.SessionStateProxy.SetVariable('remote',$remote)
    $rs.SessionStateProxy.SetVariable('work',$WORK)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($viewWorker)
    $viewSync.PS=$ps; $viewSync.RS=$rs; $viewSync.Handle=$ps.BeginInvoke()
}

# Called ON THE UI THREAD by $mediaTimer when the scp finishes.
function Complete-ViewRemoteFile {
    try{ if($viewSync.PS){ $viewSync.PS.EndInvoke($viewSync.Handle) } }catch{}
    try{ if($viewSync.RS){ $viewSync.RS.Close() } }catch{}
    try{ if($viewSync.PS){ $viewSync.PS.Dispose() } }catch{}
    $viewSync.PS=$null; $viewSync.RS=$null; $viewSync.Handle=$null
    $form.Cursor='Default'
    if(-not $viewSync.Ok){ Add-LogFiles ('View failed: '+$viewSync.Msg) $red; return }

    $local=$viewSync.Local; $size=$viewSync.Size
    $name=[System.IO.Path]::GetFileName($viewSync.Path)
    $ext=([System.IO.Path]::GetExtension($name)).ToLower()
    $binExt=@('.a','.so','.o','.png','.jpg','.jpeg','.gif','.bin','.bag','.pyc','.zip','.tar','.gz','.whl')
    $isBin=$false
    if($binExt -contains $ext){ $isBin=$true }
    else {
        try{
            $fs=[System.IO.File]::OpenRead($local); $probe=New-Object byte[] 4096
            $rd=$fs.Read($probe,0,4096); $fs.Close()
            for($i=0;$i -lt $rd;$i++){ if($probe[$i] -eq 0){ $isBin=$true; break } }
        }catch{}
    }

    $previewBox.Clear()
    Add-LogFiles ('# '+$viewSync.Path) $accent
    Add-LogFiles ('# size: {0:N0} bytes   type: {1}' -f $size,$(if($isBin){'binary'}else{'text'})) ([System.Drawing.Color]::DimGray)
    Add-LogFiles ('# local copy: '+$local) ([System.Drawing.Color]::DimGray)
    Add-LogFiles ('-----------------------------------------------------------') ([System.Drawing.Color]::DimGray)
    if($isBin){ Add-LogFiles '[binary file - not shown as text]' $amber; return }
    try{
        $lines = Get-Content -Path $local -TotalCount 400 -ErrorAction Stop
        $previewBox.SelectionStart=$previewBox.TextLength
        $previewBox.SelectionColor=[System.Drawing.Color]::Gainsboro
        $previewBox.AppendText(($lines -join "`r`n")+"`r`n")
        if(@($lines).Count -ge 400){ Add-LogFiles '... (truncated at 400 lines) ...' $amber }
        $previewBox.SelectionStart=0; $previewBox.ScrollToCaret()
    } catch {
        Add-LogFiles ('Could not read file as text: '+$_.Exception.Message) $red
    }
}

# ============================================================================
#  Wire events
# ============================================================================
$scanBtn.Add_Click({ Start-Scan }); $stopBtn.Add_Click({ Stop-Scan })
$verifyBtn.Add_Click({ Do-Verify ($ipBox.Text.Trim()) })
$connectBtn.Add_Click({ $ip=Get-SelectedIP; if(-not $ip){$ip=$ipBox.Text.Trim()}; Do-Verify $ip })
$useBtn.Add_Click({ $ip=Get-SelectedIP; if(-not $ip){$ip=$ipBox.Text.Trim()}; if($ip){ Set-RobotIP $ip; $statusLbl.Text="Applied $ip to all tabs." } })
$list.Add_DoubleClick({ Do-Verify (Get-SelectedIP) })
$list.Add_SelectedIndexChanged({ $ip=Get-SelectedIP; if($ip){ $ipBox.Text=$ip } })
$pullBtn.Add_Click({ $ip=Get-SelectedIP; if(-not $ip){$ip=$ipBox.Text.Trim()}; if($ip){ Set-RobotIP $ip } })
$testBtn.Add_Click({ Test-Ssh2 })
$sshTermBtn.Add_Click({ Open-SshTerminal }); $keyBtn.Add_Click({ Setup-SshKey })
$addFilesBtn.Add_Click({ Add-UploadFiles }); $addFolderBtn.Add_Click({ Add-UploadFolder }); $clearFilesBtn.Add_Click({ $fileList.Items.Clear() }); $uploadBtn.Add_Click({ Do-Upload })
$startLiveBtn.Add_Click({ Start-Live }); $stopLiveBtn.Add_Click({ Stop-Live }); $enableCamBtn.Add_Click({ Enable-Camera })
$connCtrlBtn.Add_Click({ Connect-Ctrl }); $discCtrlBtn.Add_Click({ Disconnect-Ctrl }); $gftBtn.Add_Click({ Send-Loco 'gft' })
# Follow toggle: ON -> Start-Follow (drive if the DRIVE box is ticked); OFF -> Stop-Follow
$followToggle.Add_CheckedChanged({
    if($script:FollowToggleGuard){ return }
    if($followToggle.Checked){
        $drive=[bool]$followDriveChk.Checked
        $ok=Start-Follow $drive
        if(-not $ok){ Close-OperatorSession }   # launch aborted -> release the Auto-Tune loop (shared contract)
        if($ok){
            if($drive){ $followToggle.Text='Follow: DRIVING - click to STOP'; $followToggle.BackColor=$red }
            else { $followToggle.Text='Follow: PREVIEW - click to STOP'; $followToggle.BackColor=$accent }
            $followToggle.ForeColor='White'
        } else {
            $script:FollowToggleGuard=$true; $followToggle.Checked=$false; $script:FollowToggleGuard=$false
        }
    } else {
        Stop-Follow $false
    }
})
$followDriveChk.Add_CheckedChanged({
    if($script:FollowOn){ return }
    if($followDriveChk.Checked){ $followStatus.Text='DRIVE selected: toggling ON will WALK the robot (requires ARM + confirm).'; $followStatus.ForeColor=$red }
    else { $followStatus.Text='PREVIEW selected: toggling ON detects + prints only (no motion).'; $followStatus.ForeColor=[System.Drawing.Color]::DimGray }
})
$armChk.Add_CheckedChanged({
    if($script:FollowOn){ return }   # ARM is locked out while a follow runs (Set-FollowLockout disabled it)
    if($armChk.Checked){
        $r=[System.Windows.Forms.MessageBox]::Show("ARM motion?`r`n`r`nThe K1 will physically move when you press Move/Head/Wave/Get-up buttons, or start Follow (DRIVE). Clear the area and keep the e-stop handy.",'Arm motion',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -eq 'OK'){ Set-MotionEnabled $true; $connStatus.Text='MOTION ARMED. Press DAMPING or STOP to halt.'; $connStatus.ForeColor=$red; Add-LogCtrl 'MOTION ARMED.' $red }
        else { $armChk.Checked=$false }
    } else { Set-MotionEnabled $false; $connStatus.Text='Motion disarmed (Damping/STOP still active).'; $connStatus.ForeColor=[System.Drawing.Color]::DimGray; Add-LogCtrl 'Motion disarmed.' $amber }
})
$ipLive.Add_Leave({ if($ipLive.Text.Trim()){ $script:RobotIP=$ipLive.Text.Trim() } })
# --- Robot Files tab ---
$refreshFilesBtn.Add_Click({ Refresh-RobotFiles })
$ipFiles.Add_Leave({ if($ipFiles.Text.Trim()){ $script:RobotIP=$ipFiles.Text.Trim() } })
$filesTree.Add_NodeMouseDoubleClick({
    param($s,$e)
    $tag=[string]$e.Node.Tag
    if($tag -and $tag.StartsWith('FILE:')){ View-RemoteFile ($tag.Substring(5)) }
})
$keysList.Add_DoubleClick({
    if($keysList.SelectedItems.Count -gt 0){
        $p=[string]$keysList.SelectedItems[0].Tag
        $ex=$keysList.SelectedItems[0].SubItems[2].Text
        if($p -and ($ex -eq 'OK')){ View-RemoteFile $p }
    }
})
$ipCtrl.Add_Leave({ if($ipCtrl.Text.Trim()){ $script:RobotIP=$ipCtrl.Text.Trim() } })

# --- Tracker tab ---
$distTrack.Add_ValueChanged({ $lblDistVal.Text=((Get-TrackStandoff)+' m') })
$spdTrack.Add_ValueChanged({ $lblSpdVal.Text=((Get-TrackVxMax)+' m/s') })
$trackToggle.Add_CheckedChanged({
    if($script:TrackToggleGuard){ return }
    if($trackToggle.Checked){
        $drive=[bool]$trackDriveChk.Checked
        $ok=Start-Tracker $drive   # no try here: it would turn the app's skip-and-continue errors into an abort after the ssh is live
        if(-not $ok){ Close-OperatorSession }   # launch aborted -> release the Auto-Tune loop (shared contract)
        if($ok){
            if($drive){ $trackToggle.Text='Follow: DRIVING - click to STOP'; $trackToggle.BackColor=$red }
            else { $trackToggle.Text='Follow: PREVIEW - click to STOP'; $trackToggle.BackColor=$accent }
            $trackToggle.ForeColor='White'
        } else {
            $script:TrackToggleGuard=$true; $trackToggle.Checked=$false; $script:TrackToggleGuard=$false
        }
    } else {
        Stop-Tracker $false
    }
})
$trackDriveChk.Add_CheckedChanged({
    if($script:TrackOn){ return }
    if($trackDriveChk.Checked){ Add-LogTrack 'DRIVE selected: toggling Follow ON will WALK the robot (requires ARM + confirm).' $red }
    else { Add-LogTrack 'PREVIEW selected: toggling Follow ON tracks only - no motion.' ([System.Drawing.Color]::DimGray) }
})
$trackArmChk.Add_CheckedChanged({
    if($script:TrackOn){ return }
    if($trackArmChk.Checked){
        $r=[System.Windows.Forms.MessageBox]::Show("ARM motion?`r`n`r`nThe K1 will PHYSICALLY WALK when you start Follow in DRIVE mode. Clear the area and keep the e-stop handy.",'Arm motion',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -eq 'OK'){ Add-LogTrack 'MOTION ARMED (Tracker). DRIVE follow now permitted.' $red }
        else { $trackArmChk.Checked=$false }
    } else { Add-LogTrack 'Motion disarmed (Tracker).' $amber }
})
$trackStopBtn.Add_Click({ Stop-Tracker $false })
$ipTrack.Add_Leave({ if($ipTrack.Text.Trim()){ $script:RobotIP=$ipTrack.Text.Trim() } })

if(Test-Path $LAST_TARGET_FILE){ $last=(Get-Content $LAST_TARGET_FILE -EA SilentlyContinue | Select-Object -First 1); if($last){ Set-RobotIP $last.Trim() } }

$timer.Start(); $mediaTimer.Start(); $fpsTimer.Start()

$form.Add_Shown({
    Add-Log 'K1 Finder ready. Scan or enter the K1 IP, then Verify.' $accent
    Add-Log2 'SSH & Files ready.' $accent
    Add-LogLive 'Live View: set IP, pick a camera topic, click Start. Frames appear when the camera is publishing.' $accent
    Add-LogCtrl 'Control: Connect (iface 127.0.0.1), Test link (gft), then ARM to enable motion. DAMPING/STOP always live.' $accent
    Add-LogCtrl 'Follow (QR): toggle ON = PREVIEW (detect-only, safe). Tick DRIVE + ARM to walk the robot. Toggle OFF stops + PREP.' $accent
    if($script:RobotIP){ $ipFiles.Text=$script:RobotIP }
    try{ $filesSplit.SplitterDistance=400 }catch{}
    Add-LogFiles 'Robot Files: set IP, click "Refresh from robot" to deploy + run tree_manifest.py and load the live SDK layout.' $accent
    Add-LogTrack 'Tracker: set IP, tune Distance + Speed, then toggle Follow. PREVIEW never moves; tick DRIVE + ARM (confirm) to WALK. Show the marker to lock onto a person; the robot then person-follows. STOP / toggle OFF safes the robot.' $accent
})
$form.Add_FormClosing({
    $sync.Cancel=$true; $timer.Stop(); $mediaTimer.Stop(); $fpsTimer.Stop()
    try{ if($filesSync.PS){ $filesSync.PS.Stop(); $filesSync.PS.Dispose() } }catch{}
    try{ if($viewSync.PS){ $viewSync.PS.Stop(); $viewSync.PS.Dispose() } }catch{}
    try{ Stop-Voice }catch{}
    try{ Stop-Tracker }catch{}; try{ Stop-Follow }catch{}; try{ Stop-Live }catch{}; try{ Disconnect-Ctrl }catch{}
    try{ if($sync.PS){ $sync.PS.Stop() } }catch{}
})

[void]$form.ShowDialog()
