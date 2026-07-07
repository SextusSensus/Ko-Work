# ============================================================================
#  K1 Finder - Booster K1 discovery, SSH/files, live camera view & control
#  Native Windows desktop app (PowerShell + Windows Forms, zero dependencies)
#
#  Tabs:
#    1. Discover    - scan the LAN, find/rank the K1, verify reachability
#    2. SSH & Files - SSH terminal, passwordless key, file upload (scp)
#    3. Live View   - live MJPEG-over-SSH stream from the K1 head camera
#    4. Control     - clickable menu of all loco commands (drives the SDK CLI)
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
$K1_SSH_PASS      = '123456'
$K1_LOCO_IFACE    = '127.0.0.1'
$SCRIPT_DIR       = Split-Path -Parent $MyInvocation.MyCommand.Path
# P2.1 reorg: the app lives in desktop/; its deploy SOURCES moved to sibling dirs. The scp DEST
# stays flat /home/booster/... (the robot layout is unchanged). last_target.txt is per-machine
# runtime state and stays next to the app.
$REPO_ROOT        = Split-Path -Parent $SCRIPT_DIR
$ROBOT_DIR        = Join-Path $REPO_ROOT 'robot'
$MODELS_DIR       = Join-Path $REPO_ROOT 'models'
$LAST_TARGET_FILE = Join-Path $SCRIPT_DIR 'last_target.txt'
$WORK             = Join-Path $env:TEMP 'k1finder'
New-Item -ItemType Directory -Force -Path $WORK | Out-Null

$script:RobotIP  = $K1_DEFAULT_IP
$script:SshUser  = $K1_SSH_USER
$script:SshPass  = $K1_SSH_PASS

# ---- SSH password via askpass (non-interactive) -----------------------------
$ASKPASS = Join-Path $WORK 'askpass.cmd'
function Update-Askpass {
    Set-Content -Path $ASKPASS -Value "@echo off`r`necho $($script:SshPass)" -Encoding ASCII
}
Update-Askpass
$env:SSH_ASKPASS = $ASKPASS
$env:SSH_ASKPASS_REQUIRE = 'force'
$env:DISPLAY = 'localhost:0.0'

$SSH_OPTS = @('-o','StrictHostKeyChecking=accept-new','-o','PreferredAuthentications=password',
              '-o','PubkeyAuthentication=no','-o','ConnectTimeout=8','-o','ServerAliveInterval=5')

function Get-SshOptString {
    '-o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=8 -o ServerAliveInterval=5'
}

# ---- Deploy robot-side helper scripts (idempotent) --------------------------
$script:Deployed = $false
function Deploy-RobotFiles {
    param([string]$ip)
    $files = @('stream_cam.py','run_stream.sh','run_loco.sh')
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
# compiles loco_follow_bridge from source on the robot on first DRIVE (verified recipe).
# follow_person_k1.py = lock-and-handoff: marker is a one-time lock onto the person, then YOLO-follows that person.
function Deploy-FollowFiles {
    param([string]$ip)
    foreach ($f in @('follow_person_k1.py','common.py','bridge.py','tracking.py','loco_follow_bridge.cpp','run_follow.sh','run_follow_demo.sh','stage_pose.py')) {
        $src = Join-Path $ROBOT_DIR $f
        if (-not (Test-Path $src)) { return $false }
        $args = $SSH_OPTS + @($src, ("{0}@{1}:/home/booster/{2}" -f $script:SshUser, $ip, $f))
        $p = Start-Process scp.exe -ArgumentList $args -NoNewWindow -PassThru
        $null = $p.WaitForExit(20000)
    }
    # Config layer (P1.1): the node loads /home/booster/config/defaults.yaml and FAIL-CLOSES
    # without it, so defaults.yaml is hard-required like the files above; the profiles are
    # best-effort. mkdir the flat config dir first (mirrors the reid/ mkdir pattern).
    $cfgDir = Join-Path $ROBOT_DIR 'config'
    $defaults = Join-Path $cfgDir 'defaults.yaml'
    if (-not (Test-Path $defaults)) { return $false }
    $mk = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser, $ip), 'mkdir -p /home/booster/config')) -NoNewWindow -PassThru
    $null = $mk.WaitForExit(10000)
    $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($defaults, ("{0}@{1}:/home/booster/config/defaults.yaml" -f $script:SshUser, $ip))) -NoNewWindow -PassThru
    $null = $p.WaitForExit(20000)
    # defaults.yaml is hard-required (node fail-closes without it): a failed/timed-out push must
    # abort the launch, not leave a stale config in place and report success.
    if (($null -eq $p.ExitCode) -or ($p.ExitCode -ne 0)) { return $false }
    foreach ($prof in @('dev.yaml', 'demo.yaml', 'field.yaml')) {
        $ps = Join-Path $cfgDir $prof
        if (Test-Path $ps) {
            $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($ps, ("{0}@{1}:/home/booster/config/{2}" -f $script:SshUser, $ip, $prof))) -NoNewWindow -PassThru
            $null = $p.WaitForExit(20000)
        }
    }
    # k1_rerun.py is BEST-EFFORT (review fix): Rerun is never a launch dependency -- the node
    # degrades to a no-op sink when the module is absent (follow_person_k1.py _NullRR), so a
    # missing local copy must not block the follow like the hard-required files above do.
    $rr = Join-Path $ROBOT_DIR 'k1_rerun.py'
    if (Test-Path $rr) {
        $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($rr, ("{0}@{1}:/home/booster/k1_rerun.py" -f $script:SshUser, $ip))) -NoNewWindow -PassThru
        $null = $p.WaitForExit(20000)
    }
    return $true
}

# ---- Gesture pose-model staging (auto, idempotent) --------------------------
# True iff the robot has $path. Uses a single-quoted remote test (no embedded double quotes -> safe
# through Start-Process arg quoting). Output captured to a temp file.
function Test-RobotFile([string]$ip,[string]$path){
    $tmp = Join-Path $env:TEMP 'k1_rf_chk.txt'
    try{
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("test -f '{0}' && echo PRESENT || echo MISSING" -f $path))) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
        $null = $p.WaitForExit(10000)
        return ((Get-Content $tmp -Raw -ErrorAction SilentlyContinue) -match 'PRESENT')
    }catch{ return $false }
}

# Make the YOLO11n-pose model exist on the robot before a gesture / A-B follow. Priority:
#   1) already present;  2) a local copy (matching basename) next to the app -> scp it (OFFLINE-safe);
#   3) export it on the robot via the deployed stage_pose.py (downloads the .pt ONCE -> needs internet).
# Returns $true if present afterward. If $false, the node still launches and SAFELY falls back to the
# ArUco marker. Never throws.
function Ensure-GestureModel([string]$ip){
    $remote = $script:GestureModel
    if(Test-RobotFile $ip $remote){ Add-LogTrack ('Gesture model present: {0}' -f $remote) $green; return $true }
    $local = Join-Path $MODELS_DIR (Split-Path $remote -Leaf)
    if(Test-Path $local){
        Add-LogTrack ('Staging gesture model ({0}) to robot...' -f (Split-Path $local -Leaf)) $accent
        try{ $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($local, ("{0}@{1}:{2}" -f $script:SshUser,$ip,$remote))) -NoNewWindow -PassThru; $null = $p.WaitForExit(120000) }catch{ Add-LogTrack ('scp failed: {0}' -f $_) $red }
        if(Test-RobotFile $ip $remote){ Add-LogTrack 'Gesture model staged (scp).' $green; return $true }
    }
    Add-LogTrack 'No local model -> exporting on the robot (ultralytics, needs internet once)...' $amber
    $tmp = Join-Path $env:TEMP 'k1_stage.txt'
    try{
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), 'python3 /home/booster/stage_pose.py')) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
        $null = $p.WaitForExit(240000)
        $r = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
        if($r){ Add-LogTrack ('stage_pose: {0}' -f ($r.Trim())) $accent }
    }catch{ Add-LogTrack ('Robot export failed: {0}' -f $_) $red }
    if(Test-RobotFile $ip $remote){ Add-LogTrack 'Gesture model exported on robot.' $green; return $true }
    Add-LogTrack 'Gesture model unavailable -> follow uses the ArUco marker (safe). Stage yolo11n-pose.onnx on the robot to enable gesture.' $amber
    return $false
}

# Make the OSNet ReID ONNX exist on the robot before an --appearance osnet follow. Priority:
#   1) already present;  2) a local copy (matching basename) next to the app -> scp it (OFFLINE-safe).
# NO robot-side export branch (OSNet has no ultralytics one-liner -> pre-stage the .onnx). Returns $true
# if present afterward. On $false the node still launches and SAFELY falls back to a colour histogram
# (the REID badge shows HIST red and the node's arm-gate auto-refuses armed re-lock). Never throws.
function Ensure-ReidModel([string]$ip){
    $remote = $script:ReidEngine
    if(Test-RobotFile $ip $remote){ Add-LogTrack ('ReID engine present: {0}' -f $remote) $green; return $true }
    $local = Join-Path $MODELS_DIR (Split-Path $remote -Leaf)
    if(Test-Path $local){
        Add-LogTrack ('Staging ReID engine ({0}) to robot...' -f (Split-Path $local -Leaf)) $accent
        try{
            $rdir = (Split-Path $remote -Parent) -replace '\\','/'
            $p = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ("mkdir -p '{0}'" -f $rdir))) -NoNewWindow -PassThru; $null = $p.WaitForExit(10000)
            $p = Start-Process scp.exe -ArgumentList ($SSH_OPTS + @($local, ("{0}@{1}:{2}" -f $script:SshUser,$ip,$remote))) -NoNewWindow -PassThru
            # APP-4: a timed-out scp keeps writing in the background and a bare `test -f` passes on
            # the truncated in-progress file -> KILL on timeout, then verify the REMOTE SIZE below.
            if(-not $p.WaitForExit(120000)){ try{ $p.Kill() }catch{}; $null=$p.WaitForExit(2000); Add-LogTrack 'scp timed out -> staging treated as FAILED' $red }
        }catch{ Add-LogTrack ('scp failed: {0}' -f $_) $red }
        # Size-verified presence (not just existence): catches truncated/aborted transfers. Remote
        # command uses NO double quotes (safe through Start-Process arg quoting, like Test-RobotFile).
        $llen = (Get-Item $local).Length
        $tmp = Join-Path $env:TEMP 'k1_reid_sz.txt'
        try{
            Remove-Item $tmp -ErrorAction SilentlyContinue
            $q = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), ('test -f ''{0}'' && [ $(stat -c%s ''{0}'') -eq {1} ] && echo SIZEOK || echo BAD' -f $remote,$llen))) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
            $null = $q.WaitForExit(10000)
            if((Get-Content $tmp -Raw -ErrorAction SilentlyContinue) -match 'SIZEOK'){ Add-LogTrack 'ReID engine staged (scp, size verified).' $green; return $true }
        }catch{}
    }
    Add-LogTrack ('ReID engine unavailable ({0}) -> deep re-ID falls back to histogram; armed re-lock auto-refused. Pre-stage {1} on the robot (or next to the app) to enable OSNet.' -f $remote, (Split-Path $remote -Leaf)) $amber
    return $false
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
        $q = Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip), 'ls -1t /home/booster/rerun/*.rrd 2>/dev/null | head -1')) -NoNewWindow -PassThru -RedirectStandardOutput $tmp
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
        try{ $q=Start-Process $py -ArgumentList '-c "import rerun"' -WindowStyle Hidden -PassThru; if($q.WaitForExit(15000) -and $q.ExitCode -eq 0){ $imp=$true } }catch{}
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
$script:TrackMaxSec=125          # UI-side hard session watchdog (python also self-limits at 120s)
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
$script:FollowMaxSec=125          # UI-side hard session watchdog (python also self-limits at 120s)
$script:FollowToggleGuard=$false  # prevents the toggle's CheckedChanged from re-entering during programmatic resets

# ============================================================================
#  Fonts / colors
# ============================================================================
$font=New-Object System.Drawing.Font('Segoe UI',9)
$fontBold=New-Object System.Drawing.Font('Segoe UI',10,[System.Drawing.FontStyle]::Bold)
$mono=New-Object System.Drawing.Font('Consolas',9)
$accent=[System.Drawing.Color]::FromArgb(0,120,215)
$green=[System.Drawing.Color]::FromArgb(16,124,16)
$red=[System.Drawing.Color]::FromArgb(196,43,28)
$amber=[System.Drawing.Color]::FromArgb(202,124,0)
$dark=[System.Drawing.Color]::FromArgb(32,34,37)

# ============================================================================
#  Form + header + tabs + status
# ============================================================================
$form=New-Object System.Windows.Forms.Form
$form.Text='K1 Finder - Booster K1 Discovery, SSH, Live View & Control'
$form.Size=New-Object System.Drawing.Size(900,720)
$form.MinimumSize=New-Object System.Drawing.Size(760,600)
$form.StartPosition='CenterScreen'; $form.Font=$font; $form.BackColor=[System.Drawing.Color]::White

$header=New-Object System.Windows.Forms.Panel
$header.Dock='Top'; $header.Height=50; $header.BackColor=$dark
$title=New-Object System.Windows.Forms.Label
$title.Text='Booster K1  -  Discover - SSH - Live View - Control'
$title.ForeColor=[System.Drawing.Color]::White
$title.Font=New-Object System.Drawing.Font('Segoe UI',12,[System.Drawing.FontStyle]::Bold)
$title.AutoSize=$true; $title.Location=New-Object System.Drawing.Point(16,12)
$header.Controls.Add($title)

$status=New-Object System.Windows.Forms.StatusStrip
$statusLbl=New-Object System.Windows.Forms.ToolStripStatusLabel
$statusLbl.Text='Ready.'
[void]$status.Items.Add($statusLbl)

$tabs=New-Object System.Windows.Forms.TabControl
$tabs.Dock='Fill'; $tabs.Font=$font
$tabDiscover=New-Object System.Windows.Forms.TabPage; $tabDiscover.Text='  1. Discover  '; $tabDiscover.BackColor=[System.Drawing.Color]::White
$tabSsh=New-Object System.Windows.Forms.TabPage; $tabSsh.Text='  2. SSH & Files  '; $tabSsh.BackColor=[System.Drawing.Color]::White
$tabLive=New-Object System.Windows.Forms.TabPage; $tabLive.Text='  3. Live View  '; $tabLive.BackColor=[System.Drawing.Color]::White
$tabCtrl=New-Object System.Windows.Forms.TabPage; $tabCtrl.Text='  4. Control  '; $tabCtrl.BackColor=[System.Drawing.Color]::White
$tabFiles=New-Object System.Windows.Forms.TabPage; $tabFiles.Text='  5. Robot Files  '; $tabFiles.BackColor=[System.Drawing.Color]::White
$tabTrack=New-Object System.Windows.Forms.TabPage; $tabTrack.Text='  6. Tracker  '; $tabTrack.BackColor=[System.Drawing.Color]::White
[void]$tabs.TabPages.AddRange(@($tabDiscover,$tabSsh,$tabLive,$tabCtrl,$tabFiles,$tabTrack))

$form.Controls.Add($header); $form.Controls.Add($status); $form.Controls.Add($tabs); $tabs.BringToFront()

# ============================================================================
#  TAB 1 - DISCOVER
# ============================================================================
$ctrlPanel=New-Object System.Windows.Forms.Panel; $ctrlPanel.Dock='Top'; $ctrlPanel.Height=96; $ctrlPanel.Padding='12,8,12,8'
$subnetLbl=New-Object System.Windows.Forms.Label; $subnetLbl.Text='Subnets: (detected at scan time)'; $subnetLbl.AutoSize=$true; $subnetLbl.ForeColor=[System.Drawing.Color]::DimGray; $subnetLbl.Location=New-Object System.Drawing.Point(14,8); $ctrlPanel.Controls.Add($subnetLbl)
$scanBtn=New-Object System.Windows.Forms.Button; $scanBtn.Text='Scan for K1'; $scanBtn.Size='120,32'; $scanBtn.Location='14,30'; $scanBtn.BackColor=$accent; $scanBtn.ForeColor='White'; $scanBtn.FlatStyle='Flat'; $scanBtn.Font=$fontBold; $ctrlPanel.Controls.Add($scanBtn)
$stopBtn=New-Object System.Windows.Forms.Button; $stopBtn.Text='Stop'; $stopBtn.Size='70,32'; $stopBtn.Location='140,30'; $stopBtn.FlatStyle='Flat'; $stopBtn.Enabled=$false; $ctrlPanel.Controls.Add($stopBtn)
$manualLbl=New-Object System.Windows.Forms.Label; $manualLbl.Text='Or enter K1 IP:'; $manualLbl.AutoSize=$true; $manualLbl.Location='230,38'; $ctrlPanel.Controls.Add($manualLbl)
$ipBox=New-Object System.Windows.Forms.TextBox; $ipBox.Size='130,26'; $ipBox.Location='322,35'; $ipBox.Font=$mono; $ipBox.Text=$K1_DEFAULT_IP; $ctrlPanel.Controls.Add($ipBox)
$verifyBtn=New-Object System.Windows.Forms.Button; $verifyBtn.Text='Verify'; $verifyBtn.Size='80,32'; $verifyBtn.Location='460,30'; $verifyBtn.FlatStyle='Flat'; $ctrlPanel.Controls.Add($verifyBtn)
$progress=New-Object System.Windows.Forms.ProgressBar; $progress.Size='180,18'; $progress.Location='560,38'; $progress.Style='Continuous'; $ctrlPanel.Controls.Add($progress)

$list=New-Object System.Windows.Forms.ListView; $list.View='Details'; $list.FullRowSelect=$true; $list.GridLines=$true; $list.MultiSelect=$false; $list.HideSelection=$false; $list.Dock='Fill'; $list.Font=$font
[void]$list.Columns.Add('Confidence',90);[void]$list.Columns.Add('IP Address',130);[void]$list.Columns.Add('Hostname',150);[void]$list.Columns.Add('SSH Banner',200);[void]$list.Columns.Add('Why',230)
$listPanel=New-Object System.Windows.Forms.Panel; $listPanel.Dock='Fill'; $listPanel.Padding='12,4,12,4'; $listPanel.Controls.Add($list)

$bottom=New-Object System.Windows.Forms.Panel; $bottom.Dock='Bottom'; $bottom.Height=210; $bottom.Padding='12,4,12,8'
$connectBtn=New-Object System.Windows.Forms.Button; $connectBtn.Text='Verify Selected'; $connectBtn.Size='130,30'; $connectBtn.Location='12,4'; $connectBtn.BackColor=$green; $connectBtn.ForeColor='White'; $connectBtn.FlatStyle='Flat'; $connectBtn.Font=$fontBold; $bottom.Controls.Add($connectBtn)
$useBtn=New-Object System.Windows.Forms.Button; $useBtn.Text='Use this IP everywhere'; $useBtn.Size='170,30'; $useBtn.Location='150,4'; $useBtn.FlatStyle='Flat'; $bottom.Controls.Add($useBtn)
$logBox=New-Object System.Windows.Forms.RichTextBox; $logBox.ReadOnly=$true; $logBox.Dock='Bottom'; $logBox.Height=160; $logBox.BackColor=$dark; $logBox.ForeColor=[System.Drawing.Color]::Gainsboro; $logBox.Font=$mono; $bottom.Controls.Add($logBox)
$tabDiscover.Controls.Add($ctrlPanel); $tabDiscover.Controls.Add($bottom); $tabDiscover.Controls.Add($listPanel); $listPanel.BringToFront()

# ============================================================================
#  TAB 2 - SSH & FILES
# ============================================================================
$sshLayout=New-Object System.Windows.Forms.TableLayoutPanel; $sshLayout.Dock='Fill'; $sshLayout.ColumnCount=1; $sshLayout.RowCount=4; $sshLayout.Padding='12,8,12,8'
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,74)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,92)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,188)))
[void]$sshLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
$grpRobot=New-Object System.Windows.Forms.GroupBox; $grpRobot.Text='Robot'; $grpRobot.Dock='Fill'
$lblIp2=New-Object System.Windows.Forms.Label; $lblIp2.Text='Robot IP:'; $lblIp2.AutoSize=$true; $lblIp2.Location='12,28'; $grpRobot.Controls.Add($lblIp2)
$ip2Box=New-Object System.Windows.Forms.TextBox; $ip2Box.Size='150,26'; $ip2Box.Location='78,25'; $ip2Box.Font=$mono; $ip2Box.Text=$K1_DEFAULT_IP; $grpRobot.Controls.Add($ip2Box)
$pullBtn=New-Object System.Windows.Forms.Button; $pullBtn.Text='Pull from Discover'; $pullBtn.Size='140,26'; $pullBtn.Location='240,25'; $pullBtn.FlatStyle='Flat'; $grpRobot.Controls.Add($pullBtn)
$testBtn=New-Object System.Windows.Forms.Button; $testBtn.Text='Test SSH'; $testBtn.Size='100,26'; $testBtn.Location='392,25'; $testBtn.FlatStyle='Flat'; $grpRobot.Controls.Add($testBtn)
$userLbl=New-Object System.Windows.Forms.Label; $userLbl.Text=("login: {0} / pw: {1}" -f $K1_SSH_USER,$K1_SSH_PASS); $userLbl.AutoSize=$true; $userLbl.ForeColor=[System.Drawing.Color]::DimGray; $userLbl.Location='512,30'; $grpRobot.Controls.Add($userLbl)
$grpSsh=New-Object System.Windows.Forms.GroupBox; $grpSsh.Text='SSH'; $grpSsh.Dock='Fill'
$sshDesc=New-Object System.Windows.Forms.Label; $sshDesc.Text='Open a shell on the robot, or install an SSH key so uploads need no password.'; $sshDesc.AutoSize=$true; $sshDesc.ForeColor=[System.Drawing.Color]::DimGray; $sshDesc.Location='12,22'; $grpSsh.Controls.Add($sshDesc)
$sshTermBtn=New-Object System.Windows.Forms.Button; $sshTermBtn.Text='Open SSH Terminal'; $sshTermBtn.Size='160,30'; $sshTermBtn.Location='12,46'; $sshTermBtn.BackColor=$accent; $sshTermBtn.ForeColor='White'; $sshTermBtn.FlatStyle='Flat'; $sshTermBtn.Font=$fontBold; $grpSsh.Controls.Add($sshTermBtn)
$keyBtn=New-Object System.Windows.Forms.Button; $keyBtn.Text='Enable passwordless login (install SSH key)'; $keyBtn.Size='300,30'; $keyBtn.Location='184,46'; $keyBtn.FlatStyle='Flat'; $grpSsh.Controls.Add($keyBtn)
$grpUp=New-Object System.Windows.Forms.GroupBox; $grpUp.Text='Upload files / folders to the K1'; $grpUp.Dock='Fill'
$addFilesBtn=New-Object System.Windows.Forms.Button; $addFilesBtn.Text='Add files...'; $addFilesBtn.Size='100,28'; $addFilesBtn.Location='12,24'; $addFilesBtn.FlatStyle='Flat'; $grpUp.Controls.Add($addFilesBtn)
$addFolderBtn=New-Object System.Windows.Forms.Button; $addFolderBtn.Text='Add folder...'; $addFolderBtn.Size='100,28'; $addFolderBtn.Location='118,24'; $addFolderBtn.FlatStyle='Flat'; $grpUp.Controls.Add($addFolderBtn)
$clearFilesBtn=New-Object System.Windows.Forms.Button; $clearFilesBtn.Text='Clear'; $clearFilesBtn.Size='70,28'; $clearFilesBtn.Location='224,24'; $clearFilesBtn.FlatStyle='Flat'; $grpUp.Controls.Add($clearFilesBtn)
$fileList=New-Object System.Windows.Forms.ListBox; $fileList.Size='400,92'; $fileList.Location='12,58'; $fileList.Font=$mono; $fileList.HorizontalScrollbar=$true; $grpUp.Controls.Add($fileList)
$remoteLbl=New-Object System.Windows.Forms.Label; $remoteLbl.Text='Remote path:'; $remoteLbl.AutoSize=$true; $remoteLbl.Location='428,60'; $grpUp.Controls.Add($remoteLbl)
$remoteBox=New-Object System.Windows.Forms.TextBox; $remoteBox.Size='280,26'; $remoteBox.Location='428,80'; $remoteBox.Font=$mono; $remoteBox.Text='/home/booster/'; $grpUp.Controls.Add($remoteBox)
$pwlessChk=New-Object System.Windows.Forms.CheckBox; $pwlessChk.Text='Passwordless (SSH key installed) - show result in app'; $pwlessChk.AutoSize=$true; $pwlessChk.Location='428,112'; $grpUp.Controls.Add($pwlessChk)
$uploadBtn=New-Object System.Windows.Forms.Button; $uploadBtn.Text='Upload to K1'; $uploadBtn.Size='160,34'; $uploadBtn.Location='548,138'; $uploadBtn.BackColor=$green; $uploadBtn.ForeColor='White'; $uploadBtn.FlatStyle='Flat'; $uploadBtn.Font=$fontBold; $grpUp.Controls.Add($uploadBtn)
$logBox2=New-Object System.Windows.Forms.RichTextBox; $logBox2.ReadOnly=$true; $logBox2.Dock='Fill'; $logBox2.BackColor=$dark; $logBox2.ForeColor=[System.Drawing.Color]::Gainsboro; $logBox2.Font=$mono
$sshLayout.Controls.Add($grpRobot,0,0); $sshLayout.Controls.Add($grpSsh,0,1); $sshLayout.Controls.Add($grpUp,0,2); $sshLayout.Controls.Add($logBox2,0,3)
$tabSsh.Controls.Add($sshLayout)

# ============================================================================
#  TAB 3 - LIVE VIEW
# ============================================================================
$liveLayout=New-Object System.Windows.Forms.TableLayoutPanel; $liveLayout.Dock='Fill'; $liveLayout.ColumnCount=1; $liveLayout.RowCount=3; $liveLayout.Padding='10,8,10,8'
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,72)))
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
[void]$liveLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,96)))
$grpLiveCtl=New-Object System.Windows.Forms.GroupBox; $grpLiveCtl.Text='Camera'; $grpLiveCtl.Dock='Fill'
$lblIpL=New-Object System.Windows.Forms.Label; $lblIpL.Text='IP:'; $lblIpL.AutoSize=$true; $lblIpL.Location='10,26'; $grpLiveCtl.Controls.Add($lblIpL)
$ipLive=New-Object System.Windows.Forms.TextBox; $ipLive.Size='120,24'; $ipLive.Location='34,23'; $ipLive.Font=$mono; $ipLive.Text=$K1_DEFAULT_IP; $grpLiveCtl.Controls.Add($ipLive)
$lblTopic=New-Object System.Windows.Forms.Label; $lblTopic.Text='Topic:'; $lblTopic.AutoSize=$true; $lblTopic.Location='164,26'; $grpLiveCtl.Controls.Add($lblTopic)
$topicCombo=New-Object System.Windows.Forms.ComboBox; $topicCombo.Size='220,24'; $topicCombo.Location='208,23'; $topicCombo.DropDownStyle='DropDownList'
# raw/rgb is the topic that actually delivers frames (the processed head/rgb stays at 0Hz); default to it.
[void]$topicCombo.Items.AddRange(@('/boostercamera/head/raw/rgb','/boostercamera/head/rgb','/boostercamera/head/right/rgb','/boostercamera/head/raw/combine/rgb','/boostercamera/head/depth')); $topicCombo.SelectedIndex=0; $grpLiveCtl.Controls.Add($topicCombo)
$lblFps=New-Object System.Windows.Forms.Label; $lblFps.Text='FPS:'; $lblFps.AutoSize=$true; $lblFps.Location='440,26'; $grpLiveCtl.Controls.Add($lblFps)
$fpsCombo=New-Object System.Windows.Forms.ComboBox; $fpsCombo.Size='55,24'; $fpsCombo.Location='474,23'; $fpsCombo.DropDownStyle='DropDownList'; [void]$fpsCombo.Items.AddRange(@('5','10','12','15','20')); $fpsCombo.SelectedIndex=2; $grpLiveCtl.Controls.Add($fpsCombo)
$lblQ=New-Object System.Windows.Forms.Label; $lblQ.Text='Q:'; $lblQ.AutoSize=$true; $lblQ.Location='536,26'; $grpLiveCtl.Controls.Add($lblQ)
$qCombo=New-Object System.Windows.Forms.ComboBox; $qCombo.Size='55,24'; $qCombo.Location='556,23'; $qCombo.DropDownStyle='DropDownList'; [void]$qCombo.Items.AddRange(@('40','55','70','85')); $qCombo.SelectedIndex=2; $grpLiveCtl.Controls.Add($qCombo)
$startLiveBtn=New-Object System.Windows.Forms.Button; $startLiveBtn.Text='Start'; $startLiveBtn.Size='70,30'; $startLiveBtn.Location='624,21'; $startLiveBtn.BackColor=$green; $startLiveBtn.ForeColor='White'; $startLiveBtn.FlatStyle='Flat'; $startLiveBtn.Font=$fontBold; $grpLiveCtl.Controls.Add($startLiveBtn)
$stopLiveBtn=New-Object System.Windows.Forms.Button; $stopLiveBtn.Text='Stop'; $stopLiveBtn.Size='60,30'; $stopLiveBtn.Location='698,21'; $stopLiveBtn.FlatStyle='Flat'; $stopLiveBtn.Enabled=$false; $grpLiveCtl.Controls.Add($stopLiveBtn)
$enableCamBtn=New-Object System.Windows.Forms.Button; $enableCamBtn.Text='Enable cam (beta)'; $enableCamBtn.Size='130,30'; $enableCamBtn.Location='762,21'; $enableCamBtn.FlatStyle='Flat'; $grpLiveCtl.Controls.Add($enableCamBtn)
$liveStatus=New-Object System.Windows.Forms.Label; $liveStatus.Text='Idle.'; $liveStatus.AutoSize=$true; $liveStatus.ForeColor=[System.Drawing.Color]::DimGray; $liveStatus.Location='12,50'; $grpLiveCtl.Controls.Add($liveStatus)
# marker lock-on badge (driven by the per-frame status byte from the streamer)
$lockBadge=New-Object System.Windows.Forms.Label; $lockBadge.Text='  MARKER: --  '; $lockBadge.AutoSize=$false; $lockBadge.Size='250,26'; $lockBadge.TextAlign='MiddleCenter'; $lockBadge.Font=$fontBold; $lockBadge.ForeColor='White'; $lockBadge.BackColor=[System.Drawing.Color]::Gray; $lockBadge.Location='430,48'; $grpLiveCtl.Controls.Add($lockBadge)
$muteChk=New-Object System.Windows.Forms.CheckBox; $muteChk.Text='mute lock sound'; $muteChk.AutoSize=$true; $muteChk.Location='690,50'; $muteChk.ForeColor=[System.Drawing.Color]::DimGray; $grpLiveCtl.Controls.Add($muteChk)
$livePic=New-Object System.Windows.Forms.PictureBox; $livePic.Dock='Fill'; $livePic.BackColor=[System.Drawing.Color]::Black; $livePic.SizeMode='Zoom'
$liveLog=New-Object System.Windows.Forms.RichTextBox; $liveLog.ReadOnly=$true; $liveLog.Dock='Fill'; $liveLog.BackColor=$dark; $liveLog.ForeColor=[System.Drawing.Color]::Gainsboro; $liveLog.Font=$mono
$liveLayout.Controls.Add($grpLiveCtl,0,0); $liveLayout.Controls.Add($livePic,0,1); $liveLayout.Controls.Add($liveLog,0,2)
$tabLive.Controls.Add($liveLayout)

# ============================================================================
#  TAB 4 - CONTROL
# ============================================================================
$ctrlLayout=New-Object System.Windows.Forms.TableLayoutPanel; $ctrlLayout.Dock='Fill'; $ctrlLayout.ColumnCount=1; $ctrlLayout.RowCount=4; $ctrlLayout.Padding='10,8,10,8'
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,76)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,250)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,86)))
[void]$ctrlLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))

$grpConn=New-Object System.Windows.Forms.GroupBox; $grpConn.Text='Controller connection'; $grpConn.Dock='Fill'
$lblIpC=New-Object System.Windows.Forms.Label; $lblIpC.Text='IP:'; $lblIpC.AutoSize=$true; $lblIpC.Location='10,28'; $grpConn.Controls.Add($lblIpC)
$ipCtrl=New-Object System.Windows.Forms.TextBox; $ipCtrl.Size='120,24'; $ipCtrl.Location='34,25'; $ipCtrl.Font=$mono; $ipCtrl.Text=$K1_DEFAULT_IP; $grpConn.Controls.Add($ipCtrl)
$lblIf=New-Object System.Windows.Forms.Label; $lblIf.Text='SDK iface:'; $lblIf.AutoSize=$true; $lblIf.Location='164,28'; $grpConn.Controls.Add($lblIf)
$ifaceBox=New-Object System.Windows.Forms.TextBox; $ifaceBox.Size='100,24'; $ifaceBox.Location='234,25'; $ifaceBox.Font=$mono; $ifaceBox.Text=$K1_LOCO_IFACE; $grpConn.Controls.Add($ifaceBox)
$connCtrlBtn=New-Object System.Windows.Forms.Button; $connCtrlBtn.Text='Connect'; $connCtrlBtn.Size='90,30'; $connCtrlBtn.Location='344,22'; $connCtrlBtn.BackColor=$accent; $connCtrlBtn.ForeColor='White'; $connCtrlBtn.FlatStyle='Flat'; $connCtrlBtn.Font=$fontBold; $grpConn.Controls.Add($connCtrlBtn)
$discCtrlBtn=New-Object System.Windows.Forms.Button; $discCtrlBtn.Text='Disconnect'; $discCtrlBtn.Size='90,30'; $discCtrlBtn.Location='438,22'; $discCtrlBtn.FlatStyle='Flat'; $discCtrlBtn.Enabled=$false; $grpConn.Controls.Add($discCtrlBtn)
$gftBtn=New-Object System.Windows.Forms.Button; $gftBtn.Text='Test link (gft)'; $gftBtn.Size='110,30'; $gftBtn.Location='532,22'; $gftBtn.FlatStyle='Flat'; $gftBtn.Enabled=$false; $grpConn.Controls.Add($gftBtn)
$armChk=New-Object System.Windows.Forms.CheckBox; $armChk.Text='ARM MOTION'; $armChk.AutoSize=$true; $armChk.Location='656,28'; $armChk.ForeColor=$red; $armChk.Font=$fontBold; $grpConn.Controls.Add($armChk)
$connStatus=New-Object System.Windows.Forms.Label; $connStatus.Text='Not connected. Connect, then ARM to enable motion. STOP/Damping stay live.'; $connStatus.AutoSize=$true; $connStatus.ForeColor=[System.Drawing.Color]::DimGray; $connStatus.Location='12,52'; $grpConn.Controls.Add($connStatus)

$grpCmd=New-Object System.Windows.Forms.GroupBox; $grpCmd.Text='Commands'; $grpCmd.Dock='Fill'

# helper to add a command button
function Add-Cmd {
    param($parent,$text,$code,$x,$y,$w,[bool]$motion=$true,$color=$null)
    $b=New-Object System.Windows.Forms.Button
    $b.Text=$text; $b.Location=New-Object System.Drawing.Point($x,$y); $b.Size=New-Object System.Drawing.Size($w,34); $b.FlatStyle='Flat'; $b.Tag=$code; $b.Enabled=$false
    if($color){ $b.BackColor=$color; $b.ForeColor='White'; $b.Font=$fontBold }
    $b.Add_Click({ Send-Loco ($this.Tag) })
    [void]$parent.Controls.Add($b)
    if($motion){ $script:MotionButtons += $b }
    return $b
}
# Section labels
function Add-SecLabel($parent,$text,$x,$y){ $l=New-Object System.Windows.Forms.Label; $l.Text=$text; $l.AutoSize=$true; $l.ForeColor=$accent; $l.Font=$fontBold; $l.Location=New-Object System.Drawing.Point($x,$y); $parent.Controls.Add($l) }

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
$grpFollow=New-Object System.Windows.Forms.GroupBox; $grpFollow.Text='Follow marker (QR / ArUco)'; $grpFollow.Dock='Fill'
$followToggle=New-Object System.Windows.Forms.CheckBox; $followToggle.Appearance='Button'; $followToggle.Text='Follow Marker (QR): OFF'; $followToggle.TextAlign='MiddleCenter'; $followToggle.Size='220,34'; $followToggle.Location='14,22'; $followToggle.FlatStyle='Flat'; $followToggle.Font=$fontBold; $grpFollow.Controls.Add($followToggle)
$followDriveChk=New-Object System.Windows.Forms.CheckBox; $followDriveChk.Text='DRIVE (walk the robot)'; $followDriveChk.AutoSize=$true; $followDriveChk.Location='246,30'; $followDriveChk.ForeColor=$red; $followDriveChk.Font=$fontBold; $grpFollow.Controls.Add($followDriveChk)
$followStatus=New-Object System.Windows.Forms.Label; $followStatus.Text='marker = one-time lock onto the person, then follows that person (re-show marker to re-seed). Toggle ON for PREVIEW (no motion); tick DRIVE + ARM to walk.'; $followStatus.AutoSize=$false; $followStatus.Size='660,28'; $followStatus.Location='14,60'; $followStatus.ForeColor=[System.Drawing.Color]::DimGray; $grpFollow.Controls.Add($followStatus)

$ctrlLog=New-Object System.Windows.Forms.RichTextBox; $ctrlLog.ReadOnly=$true; $ctrlLog.Dock='Fill'; $ctrlLog.BackColor=$dark; $ctrlLog.ForeColor=[System.Drawing.Color]::Gainsboro; $ctrlLog.Font=$mono
$ctrlLayout.Controls.Add($grpConn,0,0); $ctrlLayout.Controls.Add($grpCmd,0,1); $ctrlLayout.Controls.Add($grpFollow,0,2); $ctrlLayout.Controls.Add($ctrlLog,0,3)
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
$grpFilesTop=New-Object System.Windows.Forms.GroupBox; $grpFilesTop.Text='Robot file manifest'; $grpFilesTop.Dock='Fill'
$lblIpF=New-Object System.Windows.Forms.Label; $lblIpF.Text='IP:'; $lblIpF.AutoSize=$true; $lblIpF.Location='10,26'; $grpFilesTop.Controls.Add($lblIpF)
$ipFiles=New-Object System.Windows.Forms.TextBox; $ipFiles.Size='120,24'; $ipFiles.Location='34,23'; $ipFiles.Font=$mono; $ipFiles.Text=$(if($script:RobotIP){$script:RobotIP}else{$K1_DEFAULT_IP}); $grpFilesTop.Controls.Add($ipFiles)
$refreshFilesBtn=New-Object System.Windows.Forms.Button; $refreshFilesBtn.Text='Refresh from robot'; $refreshFilesBtn.Size='150,30'; $refreshFilesBtn.Location='164,20'; $refreshFilesBtn.BackColor=$accent; $refreshFilesBtn.ForeColor='White'; $refreshFilesBtn.FlatStyle='Flat'; $refreshFilesBtn.Font=$fontBold; $grpFilesTop.Controls.Add($refreshFilesBtn)
$filesStatus=New-Object System.Windows.Forms.Label; $filesStatus.Text='Click "Refresh from robot" to deploy + run tree_manifest.py and load the SDK layout.'; $filesStatus.AutoSize=$true; $filesStatus.MaximumSize='520,40'; $filesStatus.ForeColor=[System.Drawing.Color]::DimGray; $filesStatus.Location='326,24'; $grpFilesTop.Controls.Add($filesStatus)

# --- body: left TreeView | right (key-paths list over file preview) --------
$filesSplit=New-Object System.Windows.Forms.SplitContainer; $filesSplit.Dock='Fill'; $filesSplit.Orientation='Vertical'; $filesSplit.SplitterWidth=6; $filesSplit.Panel1MinSize=180; $filesSplit.Panel2MinSize=220

# left: SDK + /home/booster tree
$grpTree=New-Object System.Windows.Forms.GroupBox; $grpTree.Text='SDK + /home/booster'; $grpTree.Dock='Fill'
$filesTree=New-Object System.Windows.Forms.TreeView; $filesTree.Dock='Fill'; $filesTree.Font=$mono; $filesTree.HideSelection=$false; $filesTree.ShowLines=$true; $filesTree.PathSeparator='/'
$grpTree.Controls.Add($filesTree)
$filesSplit.Panel1.Controls.Add($grpTree)

# right: key-paths list (top) over file preview (bottom)
$rightLayout=New-Object System.Windows.Forms.TableLayoutPanel; $rightLayout.Dock='Fill'; $rightLayout.ColumnCount=1; $rightLayout.RowCount=2
[void]$rightLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,46)))
[void]$rightLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,54)))

$grpKeys=New-Object System.Windows.Forms.GroupBox; $grpKeys.Text='Key paths (from k1_paths.json)'; $grpKeys.Dock='Fill'
$keysList=New-Object System.Windows.Forms.ListView; $keysList.View='Details'; $keysList.FullRowSelect=$true; $keysList.GridLines=$true; $keysList.MultiSelect=$false; $keysList.HideSelection=$false; $keysList.Dock='Fill'; $keysList.Font=$font
[void]$keysList.Columns.Add('Key',150); [void]$keysList.Columns.Add('Path',360); [void]$keysList.Columns.Add('Exists',70)
$grpKeys.Controls.Add($keysList)

$grpPreview=New-Object System.Windows.Forms.GroupBox; $grpPreview.Text='File preview (double-click a FILE in the tree)'; $grpPreview.Dock='Fill'
$previewBox=New-Object System.Windows.Forms.RichTextBox; $previewBox.ReadOnly=$true; $previewBox.Dock='Fill'; $previewBox.BackColor=$dark; $previewBox.ForeColor=[System.Drawing.Color]::Gainsboro; $previewBox.Font=$mono; $previewBox.WordWrap=$false; $previewBox.DetectUrls=$false
$grpPreview.Controls.Add($previewBox)

$rightLayout.Controls.Add($grpKeys,0,0); $rightLayout.Controls.Add($grpPreview,0,1)
$filesSplit.Panel2.Controls.Add($rightLayout)

$filesLayout.Controls.Add($grpFilesTop,0,0); $filesLayout.Controls.Add($filesSplit,0,1)
$tabFiles.Controls.Add($filesLayout)

# ============================================================================
#  TAB 6 - TRACKER  (cockpit for the marker-seeded markerless PERSON-follow)
# ============================================================================
$trackLayout=New-Object System.Windows.Forms.TableLayoutPanel; $trackLayout.Dock='Fill'; $trackLayout.ColumnCount=1; $trackLayout.RowCount=4; $trackLayout.Padding='10,8,10,8'
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,226)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent,100)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,54)))
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,118)))

# --- top control row -------------------------------------------------------
$grpTrackCtl=New-Object System.Windows.Forms.GroupBox; $grpTrackCtl.Text='Follow control (marker locks onto a person, then person-follows)'; $grpTrackCtl.Dock='Fill'
$lblIpT=New-Object System.Windows.Forms.Label; $lblIpT.Text='IP:'; $lblIpT.AutoSize=$true; $lblIpT.Location='10,28'; $grpTrackCtl.Controls.Add($lblIpT)
$ipTrack=New-Object System.Windows.Forms.TextBox; $ipTrack.Size='120,24'; $ipTrack.Location='34,25'; $ipTrack.Font=$mono; $ipTrack.Text=$(if($script:RobotIP){$script:RobotIP}else{$K1_DEFAULT_IP}); $grpTrackCtl.Controls.Add($ipTrack)

# Distance / standoff slider (TrackBar ticks in 0.1 m: 6..30 -> 0.6..3.0 m, default 12 -> 1.2 m)
$lblDistCap=New-Object System.Windows.Forms.Label; $lblDistCap.Text='Distance:'; $lblDistCap.AutoSize=$true; $lblDistCap.Location='168,28'; $grpTrackCtl.Controls.Add($lblDistCap)
$distTrack=New-Object System.Windows.Forms.TrackBar; $distTrack.Minimum=6; $distTrack.Maximum=30; $distTrack.TickFrequency=2; $distTrack.SmallChange=1; $distTrack.LargeChange=2; $distTrack.Value=12; $distTrack.Size='150,40'; $distTrack.Location='232,20'; $grpTrackCtl.Controls.Add($distTrack)
$lblDistVal=New-Object System.Windows.Forms.Label; $lblDistVal.Text='1.2 m'; $lblDistVal.AutoSize=$false; $lblDistVal.Size='52,20'; $lblDistVal.TextAlign='MiddleLeft'; $lblDistVal.Font=$fontBold; $lblDistVal.Location='386,28'; $grpTrackCtl.Controls.Add($lblDistVal)

# Speed / vx-max slider (TrackBar ticks in 0.01 m/s: 5..30 -> 0.05..0.30 m/s, default 18 -> 0.18 m/s)
$lblSpdCap=New-Object System.Windows.Forms.Label; $lblSpdCap.Text='Speed:'; $lblSpdCap.AutoSize=$true; $lblSpdCap.Location='448,28'; $grpTrackCtl.Controls.Add($lblSpdCap)
$spdTrack=New-Object System.Windows.Forms.TrackBar; $spdTrack.Minimum=5; $spdTrack.Maximum=30; $spdTrack.TickFrequency=5; $spdTrack.SmallChange=1; $spdTrack.LargeChange=5; $spdTrack.Value=18; $spdTrack.Size='150,40'; $spdTrack.Location='498,20'; $grpTrackCtl.Controls.Add($spdTrack)
$lblSpdVal=New-Object System.Windows.Forms.Label; $lblSpdVal.Text='0.18 m/s'; $lblSpdVal.AutoSize=$false; $lblSpdVal.Size='66,20'; $lblSpdVal.TextAlign='MiddleLeft'; $lblSpdVal.Font=$fontBold; $lblSpdVal.Location='652,28'; $grpTrackCtl.Controls.Add($lblSpdVal)

# Follow toggle (CheckBox styled as a button) + DRIVE + ARM + Stop
$trackToggle=New-Object System.Windows.Forms.CheckBox; $trackToggle.Appearance='Button'; $trackToggle.Text='Follow: OFF'; $trackToggle.TextAlign='MiddleCenter'; $trackToggle.Size='150,34'; $trackToggle.Location='34,64'; $trackToggle.FlatStyle='Flat'; $trackToggle.Font=$fontBold; $grpTrackCtl.Controls.Add($trackToggle)
$trackDriveChk=New-Object System.Windows.Forms.CheckBox; $trackDriveChk.Text='DRIVE (walk)'; $trackDriveChk.AutoSize=$true; $trackDriveChk.Location='196,72'; $trackDriveChk.ForeColor=$red; $trackDriveChk.Font=$fontBold; $grpTrackCtl.Controls.Add($trackDriveChk)
$trackArmChk=New-Object System.Windows.Forms.CheckBox; $trackArmChk.Text='ARM MOTION'; $trackArmChk.AutoSize=$true; $trackArmChk.Location='320,72'; $trackArmChk.ForeColor=$red; $trackArmChk.Font=$fontBold; $grpTrackCtl.Controls.Add($trackArmChk)
$trackStopBtn=New-Object System.Windows.Forms.Button; $trackStopBtn.Text='STOP'; $trackStopBtn.Size='100,34'; $trackStopBtn.Location='452,64'; $trackStopBtn.BackColor=$red; $trackStopBtn.ForeColor='White'; $trackStopBtn.FlatStyle='Flat'; $trackStopBtn.Font=$fontBold; $grpTrackCtl.Controls.Add($trackStopBtn)
$trackMuteChk=New-Object System.Windows.Forms.CheckBox; $trackMuteChk.Text='mute lock sound'; $trackMuteChk.AutoSize=$true; $trackMuteChk.Location='572,72'; $trackMuteChk.ForeColor=[System.Drawing.Color]::DimGray; $grpTrackCtl.Controls.Add($trackMuteChk)

# --- 3rd row: perception backend + per-session feature toggles (the new follow-mode flags) ---
$lblPercep=New-Object System.Windows.Forms.Label; $lblPercep.Text='Perception:'; $lblPercep.AutoSize=$true; $lblPercep.Location='10,112'; $grpTrackCtl.Controls.Add($lblPercep)
# 'striped' removed from the operator-facing list: it is the documented lock-loser (degraded then lost the lock).
# The node's --appearance striped path is left intact for a dev who passes it via the CLI (Get-TrackExtraArgs still
# emits '--appearance striped' when $app -eq 'striped'). Items are now global,osnet -> osnet is index 1.
$trackApp=New-Object System.Windows.Forms.ComboBox; $trackApp.DropDownStyle='DropDownList'; $trackApp.Size='92,24'; $trackApp.Location='88,108'; [void]$trackApp.Items.AddRange(@('global','osnet')); $trackApp.SelectedIndex=1; $grpTrackCtl.Controls.Add($trackApp)   # default OSNet (deep ReID; armed re-lock needs it)
$trackCoast=New-Object System.Windows.Forms.CheckBox; $trackCoast.Text='Coast occlusions'; $trackCoast.AutoSize=$true; $trackCoast.Location='192,110'; $grpTrackCtl.Controls.Add($trackCoast)
$trackReacq=New-Object System.Windows.Forms.CheckBox; $trackReacq.Text='Auto re-acq'; $trackReacq.AutoSize=$true; $trackReacq.Location='322,110'; $trackReacq.Checked=$true; $grpTrackCtl.Controls.Add($trackReacq)
$trackFence=New-Object System.Windows.Forms.CheckBox; $trackFence.Text='Range fence'; $trackFence.AutoSize=$true; $trackFence.Location='416,110'; $grpTrackCtl.Controls.Add($trackFence)
# DANGER: armed markerless re-lock (--arm-reacquire). OSNet only -- the node refuses it on the weak
# backends. Default OFF; preview-verify it re-locks onto YOU before driving with it on.
$trackArmReloc=New-Object System.Windows.Forms.CheckBox; $trackArmReloc.Text='Arm re-lock'; $trackArmReloc.AutoSize=$true; $trackArmReloc.Location='510,110'; $trackArmReloc.ForeColor=$red; $trackArmReloc.Font=$fontBold; $grpTrackCtl.Controls.Add($trackArmReloc)
# Deadman HB (P2 #12): passes --require-heartbeat to the node (which also env-arms the bridge's own
# K1_REQUIRE_HB watchdog) and starts the app-side heartbeat relay. The remote loop touches the hb
# file ONLY on bytes RECEIVED from this app, so mtime freshness == end-to-end connectivity: WiFi
# drop / app freeze / laptop death -> touches stop -> node zeroes (400ms) + bridge kPrepares (1.5s).
# Default OFF (tethered byte-identical). One pillar of the untethered gate (UNTETHERED_FOLLOW.md).
$trackHbChk=New-Object System.Windows.Forms.CheckBox; $trackHbChk.Text='Deadman HB'; $trackHbChk.AutoSize=$true; $trackHbChk.Location='610,110'; $trackHbChk.ForeColor=$red; $trackHbChk.Font=$fontBold; $grpTrackCtl.Controls.Add($trackHbChk)

# --- Acquisition + command-surface toggles (LAUNCH-time for gesture/A-B; runtime for voice) ---
# Gesture lock = --lock-trigger gesture (raised hand seeds instead of the marker). A/B = --lock-trigger
# both (ArUco still drives, gesture audits -> GBIND data to retire ArUco). Voice = System.Speech keyword
# recognizer that maps spoken words to the SAME command enum the Cmd buttons send.
$chkGesture=New-Object System.Windows.Forms.CheckBox; $chkGesture.Text='Gesture lock'; $chkGesture.AutoSize=$true; $chkGesture.Location='10,154'; $chkGesture.ForeColor=$accent; $chkGesture.Font=$fontBold; $chkGesture.Checked=$true; $grpTrackCtl.Controls.Add($chkGesture)   # DEFAULT ON (2026-07-05, user request): gesture is the default lock trigger; UNTICK for the (more reliable) ArUco marker. Raise a hand DURING SEARCH to seed.
$chkAB=New-Object System.Windows.Forms.CheckBox; $chkAB.Text='A/B (compare)'; $chkAB.AutoSize=$true; $chkAB.Location='120,154'; $grpTrackCtl.Controls.Add($chkAB)
$chkVoice=New-Object System.Windows.Forms.CheckBox; $chkVoice.Text='Voice cmds'; $chkVoice.AutoSize=$true; $chkVoice.Location='240,154'; $chkVoice.ForeColor=$accent; $chkVoice.Font=$fontBold; $chkVoice.Enabled=$false; $grpTrackCtl.Controls.Add($chkVoice)
$voiceStatus=New-Object System.Windows.Forms.Label; $voiceStatus.Text='Voice: off'; $voiceStatus.AutoSize=$true; $voiceStatus.Location='340,156'; $voiceStatus.ForeColor=[System.Drawing.Color]::DimGray; $grpTrackCtl.Controls.Add($voiceStatus)
# --- persistent ReID-health badge (parsed from the node's REID-ENGINE ok/FAILED + REID-DEGRADED stderr) ---
# OSNet(TRT)/OSNet(CUDA)=green, CPU-EP=amber, HIST fallback / DEGRADED=red, '--'=idle. Set by Update-ReidBadge.
$reidBadge=New-Object System.Windows.Forms.Label; $reidBadge.Text='REID: --'; $reidBadge.AutoSize=$false; $reidBadge.Size='168,22'; $reidBadge.TextAlign='MiddleCenter'; $reidBadge.Location='470,155'; $reidBadge.ForeColor='White'; $reidBadge.BackColor=[System.Drawing.Color]::Gray; $reidBadge.Font=$fontBold; $reidBadge.BorderStyle='FixedSingle'; $grpTrackCtl.Controls.Add($reidBadge)
# Rerun (rerun.io) recording toggle -> --rerun (Phase 3). Default OFF and byte-identical to today
# when off. Records a scrubbable .rrd on the robot; pull+open it with the 'Open .rrd' button below.
$trackRerun=New-Object System.Windows.Forms.CheckBox; $trackRerun.Text='Rerun'; $trackRerun.AutoSize=$true; $trackRerun.Location='645,156'; $trackRerun.ForeColor=$accent; $trackRerun.Font=$fontBold; $grpTrackCtl.Controls.Add($trackRerun)
$chkGesture.Add_CheckedChanged({ if($chkGesture.Checked -and $chkAB.Checked){ $chkAB.Checked=$false } })
$chkAB.Add_CheckedChanged({ if($chkAB.Checked -and $chkGesture.Checked){ $chkGesture.Checked=$false } })
$chkVoice.Add_CheckedChanged({ if($chkVoice.Checked){ Start-Voice } else { Stop-Voice } })

# --- Tier-1 command row (watched-file channel /tmp/k1_cmd; enabled ONLY while a follow session runs).
# De-escalating WAIT/PARK/STATUS go immediately; RESUME/FOLLOW carry the per-command ARM credential
# under DRIVE (ARM MOTION + a typed confirm). The Ctrl-C STOP button stays the supreme deadman.
$cmdLbl=New-Object System.Windows.Forms.Label; $cmdLbl.Text='Cmd:'; $cmdLbl.AutoSize=$true; $cmdLbl.Location='10,192'; $grpTrackCtl.Controls.Add($cmdLbl)
$btnWait=New-Object System.Windows.Forms.Button; $btnWait.Text='WAIT'; $btnWait.Size='58,28'; $btnWait.Location='48,186'; $btnWait.FlatStyle='Flat'; $btnWait.Enabled=$false; $grpTrackCtl.Controls.Add($btnWait)
$btnResume=New-Object System.Windows.Forms.Button; $btnResume.Text='RESUME'; $btnResume.Size='70,28'; $btnResume.Location='112,186'; $btnResume.FlatStyle='Flat'; $btnResume.Enabled=$false; $grpTrackCtl.Controls.Add($btnResume)
$btnPark=New-Object System.Windows.Forms.Button; $btnPark.Text='PARK'; $btnPark.Size='58,28'; $btnPark.Location='188,186'; $btnPark.FlatStyle='Flat'; $btnPark.Enabled=$false; $grpTrackCtl.Controls.Add($btnPark)
$btnStatus=New-Object System.Windows.Forms.Button; $btnStatus.Text='STATUS'; $btnStatus.Size='66,28'; $btnStatus.Location='252,186'; $btnStatus.FlatStyle='Flat'; $btnStatus.Enabled=$false; $grpTrackCtl.Controls.Add($btnStatus)
$btnFollowCmd=New-Object System.Windows.Forms.Button; $btnFollowCmd.Text='FOLLOW'; $btnFollowCmd.Size='66,28'; $btnFollowCmd.Location='324,186'; $btnFollowCmd.FlatStyle='Flat'; $btnFollowCmd.Enabled=$false; $grpTrackCtl.Controls.Add($btnFollowCmd)
$btnWait.Add_Click({ Send-FollowCmd 'HOLD' })
$btnResume.Add_Click({ Send-FollowCmd 'RESUME' })
$btnPark.Add_Click({ Send-FollowCmd 'PARK' })
$btnStatus.Add_Click({ Send-FollowCmd 'STATUS' })
$btnFollowCmd.Add_Click({ Send-FollowCmd 'FOLLOW' })
# Rerun (Phase 3): pull the newest .rrd off the robot and open it in the laptop viewer. Always
# enabled (unlike the session-only Cmd buttons) -- you scrub AFTER a run. No-op-safe if none exists.
$btnRrd=New-Object System.Windows.Forms.Button; $btnRrd.Text='Open .rrd'; $btnRrd.Size='110,28'; $btnRrd.Location='598,186'; $btnRrd.FlatStyle='Flat'; $btnRrd.ForeColor=$accent; $grpTrackCtl.Controls.Add($btnRrd)
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
$trackPic=New-Object System.Windows.Forms.PictureBox; $trackPic.Dock='Fill'; $trackPic.BackColor=[System.Drawing.Color]::Black; $trackPic.SizeMode='Zoom'

# --- big state badge ------------------------------------------------------
$trackBadge=New-Object System.Windows.Forms.Label; $trackBadge.Text='IDLE'; $trackBadge.Dock='Fill'; $trackBadge.TextAlign='MiddleCenter'; $trackBadge.ForeColor='White'; $trackBadge.BackColor=[System.Drawing.Color]::Gray; $trackBadge.Font=(New-Object System.Drawing.Font('Segoe UI',16,[System.Drawing.FontStyle]::Bold))

# --- small status-transition log ------------------------------------------
$trackLog=New-Object System.Windows.Forms.RichTextBox; $trackLog.ReadOnly=$true; $trackLog.Dock='Fill'; $trackLog.BackColor=$dark; $trackLog.ForeColor=[System.Drawing.Color]::Gainsboro; $trackLog.Font=$mono

$trackLayout.Controls.Add($grpTrackCtl,0,0); $trackLayout.Controls.Add($trackPic,0,1); $trackLayout.Controls.Add($trackBadge,0,2); $trackLayout.Controls.Add($trackLog,0,3)
$tabTrack.Controls.Add($trackLayout)

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
function Set-RobotIP([string]$ip){ if(-not $ip){return}; $script:RobotIP=$ip; $ipBox.Text=$ip; $ip2Box.Text=$ip; $ipLive.Text=$ip; $ipCtrl.Text=$ip; if($ipFiles){ $ipFiles.Text=$ip }; if($ipTrack){ $ipTrack.Text=$ip } }

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
    Add-LogLive ("Deploying camera streamer to {0} ..." -f $ip) $accent
    if(-not (Deploy-RobotFiles $ip)){ Add-LogLive 'Deploy failed (helper scripts missing).' $red; return }
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
    if(-not $proc.Start()){ Add-LogLive 'Failed to start ssh.' $red; return }
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
        $a += '--arm-reacquire'
    }
    if($trackFence -and $trackFence.Checked){ $a += '--max-follow-range 4.0' }
    # Rerun observability -> record a scrubbable .rrd on the robot. Default OFF; byte-identical when off.
    # The node auto-disables Rerun (RERUN-DISABLED-SLOW) if the loop goes over budget with it on, so the
    # gait is never held hostage to logging -- but the loop-cost gate (RERUN_PLAN.md) is still the
    # operator's call before ticking this WITH DRIVE.
    if($trackRerun -and $trackRerun.Checked){ $a += ('--rerun --rerun-mode save --rerun-dir {0}' -f $script:RerunDir) }
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
    try{
        $cmd="echo $line > /tmp/k1_cmd"
        $sp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),$cmd)) -NoNewWindow -PassThru
        $null=$sp.WaitForExit(4000)
        Add-LogTrack ("CMD sent: $line") $accent
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
        $r=[System.Windows.Forms.MessageBox]::Show("Start FOLLOW in DRIVE mode?`r`n`r`nMarker = one-time lock onto the person, then the K1 will PHYSICALLY WALK to follow THAT PERSON (re-show the marker to re-seed). Standoff $((Get-TrackStandoff)) m, max speed $((Get-TrackVxMax)) m/s. Auto-stops after ~$($script:TrackMaxSec)s, when the person is lost, or if the camera stalls. Clear the area and keep the e-stop handy.",'Confirm DRIVE follow',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -ne 'OK'){ return $false }
    }
    $ip=$ipTrack.Text.Trim(); if(-not $ip){ Add-LogTrack 'Enter the robot IP first.' $amber; return $false }
    $script:RobotIP=$ip
    # SINGLE CAMERA CONSUMER: stop the Live View stream (it also decodes the head
    # camera) so the K1 only ever streams the camera once.
    if($script:LiveOn){ Add-LogTrack 'Stopping Live View (single camera consumer)...' $amber; Stop-Live }
    Add-LogTrack ("Deploying follow helpers to {0} ..." -f $ip) $accent
    if(-not (Deploy-FollowFiles $ip)){ Add-LogTrack 'Deploy failed (follow helper files missing next to the app).' $red; return $false }
    # Gesture / A-B selected -> make sure the pose model is on the robot first (auto-stage). On failure
    # the node still runs and falls back to the ArUco marker, so offer to continue rather than block.
    if(($chkGesture -and $chkGesture.Checked) -or ($chkAB -and $chkAB.Checked)){
        if(-not (Ensure-GestureModel $ip)){
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
        if(-not (Ensure-ReidModel $ip)){
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
    try{ $kp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -f follow_person_k1.py')) -NoNewWindow -PassThru; $null=$kp.WaitForExit(6000) }catch{}
    $trackSync.Stop=$false; $trackSync.Done=$false; $trackSync.Jpeg=$null; $trackSync.Seq=0; $trackSync.Frames=0; $trackSync.Err=''
    $script:trackLastSeq=-1; $script:trackFpsFrames=0; $script:trackLastLock=-1; $trackSync.Lock=0
    $mode = if($drive){'drive'}else{'preview'}
    $standoff=Get-TrackStandoff; $vxmax=Get-TrackVxMax; $extra=Get-TrackExtraArgs
    $remote="bash /home/booster/run_follow.sh $mode /boostercamera/head/raw/rgb --stream --standoff-m $standoff --vx-max $vxmax $extra"
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
        if($d -and ($d -match '^(GDBG|GESTURE|LOCK-TRIGGER|SEED|LOCKED|AUTO-RELOCK|CMD|GBIND|DRIVE-|BRIDGE|ARM|HELD|RANGE-GATE|HB-|SLOW-LOOP|LOOP-MS|WATCHDOG|FRAME-ERR|RESUME|EXIT|MODE |REID|RELOC|DEPTH|NO-FRAME stall=|RGB|RERUN)')){
            $Event.MessageData.Enqueue($d)
        }
    }
    try{ $script:TrackErrSub = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errAction -MessageData $trackSync.ErrLines; $proc.BeginErrorReadLine() }catch{ Add-LogTrack ("stderr capture failed: $_") $amber }
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('trackSync',$trackSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($trackReader); [void]$ps.BeginInvoke()
    $script:TrackPS=$ps; $script:TrackRS=$rs
    $script:TrackOn=$true; $script:TrackDrive=$drive; $script:TrackStart=[datetime]::Now
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

function Stop-Tracker([bool]$procAlreadyDead=$false){
    if(-not $script:TrackOn){ return }
    $trackSync.Stop=$true
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
    if($ip){
        try{ $sp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -TERM -f follow_person_k1.py')) -NoNewWindow -PassThru; $null=$sp.WaitForExit(2500) }catch{}
    }
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
        Add-LogTrack 'Follow stopped (killed ssh + pkill; robot does stop + PREP via SIGHUP/SIGTERM/EOF).' $amber
        $statusLbl.Text='Tracker stopped.'
    }catch{}
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
    if(-not $proc.Start()){ Add-LogCtrl 'Failed to start ssh.' $red; return }
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
        $r=[System.Windows.Forms.MessageBox]::Show("Start FOLLOW in DRIVE mode?`r`n`r`nMarker = one-time lock onto the person, then the K1 will PHYSICALLY WALK to follow THAT PERSON (re-show the marker to re-seed). It auto-stops after ~$($script:FollowMaxSec)s, when the person is lost, or if the camera stalls. Clear the area and keep the e-stop handy.",'Confirm DRIVE follow',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -ne 'OK'){ return $false }
    }
    $ip=$ipCtrl.Text.Trim(); if(-not $ip){ Add-LogCtrl 'Enter the robot IP (connection box) first.' $amber; return $false }
    $script:RobotIP=$ip
    # SINGLE CAMERA CONSUMER: stop the Live View stream (it also decodes the head
    # camera) so the camera is only ever streamed once.
    if($script:LiveOn){ Add-LogCtrl 'Stopping Live View (single camera consumer)...' $amber; Stop-Live }
    Add-LogCtrl ("Deploying follow helpers to {0} ..." -f $ip) $accent
    if(-not (Deploy-FollowFiles $ip)){ Add-LogCtrl 'Deploy failed (follow helper files missing next to the app).' $red; return $false }
    $followSync.Stop=$false; $followSync.Log.Clear()
    $mode = if($drive){'drive'}else{'preview'}
    $remote="bash /home/booster/run_follow.sh $mode /boostercamera/head/raw/rgb"
    $argStr='-tt '+(Get-SshOptString)+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardInput=$true; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogCtrl 'Failed to start ssh.' $red; return $false }
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
        $followStatus.Text="FOLLOW: DRIVE - marker = one-time lock onto the person, then follows that person (re-show marker to re-seed). ~10s camera warmup. Auto-stop in ~$($script:FollowMaxSec)s / person lost / camera stall."; $followStatus.ForeColor=$red
        Add-LogCtrl 'FOLLOW DRIVE started. Show the marker to lock onto the person, then the robot WALKS to follow THAT PERSON (re-show marker to re-seed). Toggle OFF halts + returns to PREP.' $red
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
    $followStatus.Text='Follow stopped. Robot sent stop + PREP on exit.'; $followStatus.ForeColor=[System.Drawing.Color]::DimGray
    Add-LogCtrl 'Follow stopped (Ctrl-C sent; robot does stop + PREP).' $amber
    $statusLbl.Text='Follow stopped.'
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
            Add-LogCtrl 'Follow process exited; cleaning up.' $amber
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
                # (DRIVE-ABORT, deliberate); anything else = crash.
                $tailFile = '/home/booster/k1_follow.err'
                if($ec -eq 3){ $tailFile='/home/booster/k1_compile.err'; Add-LogTrack 'Follow process exited 3 = COMPILE FAILED (run_follow.sh). See k1_compile.err tail below.' $red }
                elseif($ec -eq 4){ Add-LogTrack 'Follow process exited 4 = DRIVE-ABORT (node refused to walk -- see the amber DRIVE-ABORT line above and the tail below).' $amber }
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
1) SSH:  ssh $K1_SSH_USER@$ip   (password: $K1_SSH_PASS)
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
function Open-SshTerminal { $ip=$ip2Box.Text.Trim(); if(-not $ip){return}; Add-Log2 ("Opening SSH terminal to {0}@{1} (pw {2})" -f $K1_SSH_USER,$ip,$K1_SSH_PASS) $accent; Start-Process cmd.exe -ArgumentList '/k',"ssh -o StrictHostKeyChecking=accept-new $K1_SSH_USER@$ip" }
function Setup-SshKey {
    $ip=$ip2Box.Text.Trim(); if(-not $ip){return}
    Add-Log2 ("Installing SSH key on {0} (type pw {1} once)..." -f $ip,$K1_SSH_PASS) $accent
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
        Add-Log2 ("Launching scp in a console (type pw {0})..." -f $K1_SSH_PASS) $accent; Start-Process cmd.exe -ArgumentList '/k',$cmd
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
        $ok=Start-Tracker $drive
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
