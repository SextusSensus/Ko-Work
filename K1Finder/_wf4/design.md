I have everything needed. Here is the complete, paste-ready design for the Tracker tab.

---

# Tracker Tab â€” paste-ready edits to `C:\Users\toddm\OneDrive\Desktop\runtime\K1Finder\K1Finder.ps1`

All blocks are PowerShell 5.1 (no ternary/`?.`/`??`), matching the app's existing idioms. Each block has an EXACT quoted anchor describing where it goes.

---

## BLOCK 1 â€” New TabPage + AddRange edit

**ANCHOR** â€” find these two existing lines (241â€“242):
```powershell
$tabFiles=New-Object System.Windows.Forms.TabPage; $tabFiles.Text='  5. Robot Files  '; $tabFiles.BackColor=[System.Drawing.Color]::White
[void]$tabs.TabPages.AddRange(@($tabDiscover,$tabSsh,$tabLive,$tabCtrl,$tabFiles))
```

**REPLACE WITH** (adds `$tabTrack` line, extends the AddRange):
```powershell
$tabFiles=New-Object System.Windows.Forms.TabPage; $tabFiles.Text='  5. Robot Files  '; $tabFiles.BackColor=[System.Drawing.Color]::White
$tabTrack=New-Object System.Windows.Forms.TabPage; $tabTrack.Text='  6. Tracker  '; $tabTrack.BackColor=[System.Drawing.Color]::White
[void]$tabs.TabPages.AddRange(@($tabDiscover,$tabSsh,$tabLive,$tabCtrl,$tabFiles,$tabTrack))
```

---

## BLOCK 2 â€” `$trackSync` + cloned `$trackReader` runspace state

**ANCHOR** â€” find this existing line (184, the `$liveSync` definition):
```powershell
$liveSync = [hashtable]::Synchronized(@{ Jpeg=$null; Seq=0; Frames=0; Stop=$false; Done=$false; Err=''; Lock=0 })
```

**INSERT IMMEDIATELY AFTER IT**:
```powershell
# ---- Tracker (marker-seeded markerless PERSON-follow): annotated stream + status byte ----
# Identical MAGIC+status(1)+len(4)+jpeg protocol as Live View, cloned into its own sync table.
# status byte: 0=SEARCHING, 1=ACQUIRING MARKER, 2=TRACKING/FOLLOWING (LOCKED), 3=LOST/REACQUIRE.
$trackSync = [hashtable]::Synchronized(@{ Jpeg=$null; Seq=0; Frames=0; Stop=$false; Done=$false; Err=''; Lock=0 })
$script:TrackProc=$null; $script:TrackPS=$null; $script:TrackRS=$null; $script:TrackOn=$false; $script:TrackDrive=$false
$script:trackMs=$null; $script:trackLastSeq=-1; $script:trackFpsFrames=0; $script:trackLastLock=-1
$script:TrackStart=[datetime]::MinValue
$script:TrackMaxSec=125          # UI-side hard session watchdog (python also self-limits at 120s)
$script:TrackToggleGuard=$false  # prevents the toggle's CheckedChanged from re-entering during programmatic resets
```

---

## BLOCK 3 â€” Tracker tab controls (TableLayoutPanel: control row + PictureBox + badge/log)

**ANCHOR** â€” find this existing line (458, end of Tab 5 / Robot Files):
```powershell
$tabFiles.Controls.Add($filesLayout)
```

**INSERT IMMEDIATELY AFTER IT** (entire Tracker tab UI):
```powershell

# ============================================================================
#  TAB 6 - TRACKER  (cockpit for the marker-seeded markerless PERSON-follow)
# ============================================================================
$trackLayout=New-Object System.Windows.Forms.TableLayoutPanel; $trackLayout.Dock='Fill'; $trackLayout.ColumnCount=1; $trackLayout.RowCount=4; $trackLayout.Padding='10,8,10,8'
[void]$trackLayout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute,108)))
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

# --- big annotated video --------------------------------------------------
$trackPic=New-Object System.Windows.Forms.PictureBox; $trackPic.Dock='Fill'; $trackPic.BackColor=[System.Drawing.Color]::Black; $trackPic.SizeMode='Zoom'

# --- big state badge ------------------------------------------------------
$trackBadge=New-Object System.Windows.Forms.Label; $trackBadge.Text='IDLE'; $trackBadge.Dock='Fill'; $trackBadge.TextAlign='MiddleCenter'; $trackBadge.ForeColor='White'; $trackBadge.BackColor=[System.Drawing.Color]::Gray; $trackBadge.Font=(New-Object System.Drawing.Font('Segoe UI',16,[System.Drawing.FontStyle]::Bold))

# --- small status-transition log ------------------------------------------
$trackLog=New-Object System.Windows.Forms.RichTextBox; $trackLog.ReadOnly=$true; $trackLog.Dock='Fill'; $trackLog.BackColor=$dark; $trackLog.ForeColor=[System.Drawing.Color]::Gainsboro; $trackLog.Font=$mono

$trackLayout.Controls.Add($grpTrackCtl,0,0); $trackLayout.Controls.Add($trackPic,0,1); $trackLayout.Controls.Add($trackBadge,0,2); $trackLayout.Controls.Add($trackLog,0,3)
$tabTrack.Controls.Add($trackLayout)
```

---

## BLOCK 4 â€” `Set-RobotIP` keeps the Tracker IP box synced

**ANCHOR** â€” find this existing line (472):
```powershell
function Set-RobotIP([string]$ip){ if(-not $ip){return}; $script:RobotIP=$ip; $ipBox.Text=$ip; $ip2Box.Text=$ip; $ipLive.Text=$ip; $ipCtrl.Text=$ip; if($ipFiles){ $ipFiles.Text=$ip } }
```

**REPLACE WITH** (append the `$ipTrack` sync; guarded like `$ipFiles` since both are created after this function is defined but only ever called at runtime):
```powershell
function Set-RobotIP([string]$ip){ if(-not $ip){return}; $script:RobotIP=$ip; $ipBox.Text=$ip; $ip2Box.Text=$ip; $ipLive.Text=$ip; $ipCtrl.Text=$ip; if($ipFiles){ $ipFiles.Text=$ip }; if($ipTrack){ $ipTrack.Text=$ip } }
```

---

## BLOCK 5 â€” Add-LogTrack helper

**ANCHOR** â€” find this existing line (468):
```powershell
function Add-LogFiles([string]$m,$c){ Add-LogTo $previewBox $m $c }
```

**INSERT IMMEDIATELY AFTER IT**:
```powershell
function Add-LogTrack([string]$m,$c){ Add-LogTo $trackLog $m $c }
```

---

## BLOCK 6 â€” `$trackReader` runspace + `Start-Tracker` / `Stop-Tracker` + sliderâ†’arg conversion + badge helper

**ANCHOR** â€” find this existing line (562, the end of `Enable-Camera`, the closing brace right before the Control section header):
```powershell
    Add-LogLive 'If it shows ChangeMode -> 100, the camera RPC service is not answering in this robot state.' $amber
}
```

**INSERT IMMEDIATELY AFTER THAT CLOSING BRACE** (the whole Tracker engine â€” cloned from `$liveReader`/`Start-Live`/`Stop-Live`, with the Follow safety gating cloned from `Start-Follow`):
```powershell

# ============================================================================
#  Tracker - marker-seeded markerless person-follow (annotated stream cockpit)
#  Transport cloned from Start-Live/Stop-Live: ssh WITHOUT -tt (a PTY corrupts
#  the binary JPEG stream), RedirectStandardOutput, kill-to-stop. The node traps
#  SIGHUP on the killed ssh -> clean stop + ChangeMode(kPrepare); the bridge also
#  safes on its own stdin-EOF. So a kill safes the robot two ways - no Ctrl-C.
# ============================================================================

# Reader: identical MAGIC+status(1)+len(4)+jpeg protocol as $liveReader, into $trackSync.
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
# InvariantCulture so the remote bash always sees a '.' decimal point regardless of the PC's locale.
function Get-TrackStandoff { return ([math]::Round($distTrack.Value/10.0,2)).ToString([System.Globalization.CultureInfo]::InvariantCulture) }
function Get-TrackVxMax    { return ([math]::Round($spdTrack.Value/100.0,2)).ToString([System.Globalization.CultureInfo]::InvariantCulture) }

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
    Add-LogTrack ("Deploying follow helpers to {0} ..." -f $ip) $accent
    if(-not (Deploy-FollowFiles $ip)){ Add-LogTrack 'Deploy failed (follow helper files missing next to the app).' $red; return $false }
    # clear any orphaned follow node from a previous session (a killed ssh can leave the remote python running)
    try{ $kp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -f follow_person_k1.py')) -NoNewWindow -PassThru; $null=$kp.WaitForExit(6000) }catch{}
    $trackSync.Stop=$false; $trackSync.Done=$false; $trackSync.Jpeg=$null; $trackSync.Seq=0; $trackSync.Frames=0; $trackSync.Err=''
    $script:trackLastSeq=-1; $script:trackFpsFrames=0; $script:trackLastLock=-1; $trackSync.Lock=0
    $mode = if($drive){'drive'}else{'preview'}
    $standoff=Get-TrackStandoff; $vxmax=Get-TrackVxMax
    # --stream emits the annotated MAGIC+status+len+jpeg protocol on STDOUT; status text -> STDERR (ignored).
    $remote="bash /home/booster/run_follow.sh $mode /boostercamera/head/raw/rgb --stream --standoff-m $standoff --vx-max $vxmax"
    # NO -tt: a PTY would corrupt the binary JPEG stream (see Start-Live; opposite of the Control follow).
    $argStr=(Get-SshOptString)+" $($script:SshUser)@$ip `"$remote`""
    $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName='ssh.exe'; $psi.Arguments=$argStr
    $psi.UseShellExecute=$false; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
    $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
    if(-not $proc.Start()){ Add-LogTrack 'Failed to start ssh.' $red; return $false }
    $script:TrackProc=$proc
    $rs=[runspacefactory]::CreateRunspace(); $rs.ApartmentState='MTA'; $rs.Open()
    $rs.SessionStateProxy.SetVariable('proc',$proc); $rs.SessionStateProxy.SetVariable('trackSync',$trackSync)
    $ps=[powershell]::Create(); $ps.Runspace=$rs; [void]$ps.AddScript($trackReader); [void]$ps.BeginInvoke()
    $script:TrackPS=$ps; $script:TrackRS=$rs
    $script:TrackOn=$true; $script:TrackDrive=$drive; $script:TrackStart=[datetime]::Now
    Set-TrackLockout $true
    Set-TrackBadge 0
    if($drive){
        Add-LogTrack ("FOLLOW DRIVE started ($ip): standoff $standoff m, max speed $vxmax m/s. Show the marker to lock onto the person, then the robot WALKS to follow THAT PERSON. ~10s camera warmup. Toggle OFF / STOP halts + returns to PREP.") $red
        $statusLbl.Text="Tracker DRIVE active: $ip"
    } else {
        Add-LogTrack ("FOLLOW PREVIEW started ($ip): standoff $standoff m, max speed $vxmax m/s (preview never moves). Show the marker to lock onto the person, then person-track. ~10s camera warmup.") $green
        $statusLbl.Text="Tracker preview active: $ip"
    }
    return $true
}

function Stop-Tracker([bool]$procAlreadyDead=$false){
    if(-not $script:TrackOn){ return }
    $trackSync.Stop=$true
    # Kill the ssh process -> node traps SIGHUP -> stop + ChangeMode(kPrepare); bridge also safes on stdin-EOF.
    try{ if($script:TrackProc -and -not $script:TrackProc.HasExited){ $script:TrackProc.Kill() } }catch{}
    try{ if($script:TrackPS){ $script:TrackPS.Dispose() } }catch{}
    try{ if($script:TrackRS){ $script:TrackRS.Close() } }catch{}
    $script:TrackProc=$null; $script:TrackPS=$null; $script:TrackRS=$null
    $script:TrackOn=$false; $script:TrackDrive=$false
    Set-TrackLockout $false                                  # restore manual controls / Control follow
    if($trackArmChk.Checked){ $trackArmChk.Checked=$false }  # force re-ARM before any future DRIVE
    # reset the toggle UI without re-entering its handler
    $script:TrackToggleGuard=$true; $trackToggle.Checked=$false; $script:TrackToggleGuard=$false
    $trackToggle.Text='Follow: OFF'; $trackToggle.UseVisualStyleBackColor=$true; $trackToggle.ForeColor=[System.Drawing.SystemColors]::ControlText
    $script:trackLastLock=-1; Set-TrackBadge -1
    try{ if($trackPic.Image){ $img=$trackPic.Image; $trackPic.Image=$null; $img.Dispose() } }catch{}
    try{ if($script:trackMs){ $script:trackMs.Dispose(); $script:trackMs=$null } }catch{}
    Add-LogTrack 'Follow stopped (ssh killed; robot does stop + PREP via SIGHUP/EOF).' $amber
    $statusLbl.Text='Tracker stopped.'
}

# Single-driver coordination: while the Tracker runs, lock out the Control-tab follow,
# the manual controller connect, and this tab's own DRIVE/ARM mode switches; restore on stop.
function Set-TrackLockout([bool]$tracking){
    $idle = -not $tracking
    # Disable the Control-tab follow + manual connect while the Tracker owns loco.
    if($followToggle){ $followToggle.Enabled = ($idle -and -not $script:CtrlOn) }
    if($followDriveChk){ $followDriveChk.Enabled = $idle }
    if($connCtrlBtn){ $connCtrlBtn.Enabled = ($idle -and -not $script:CtrlOn) }
    # Can't switch mode/arm mid-follow (the toggle itself stays live to switch OFF).
    $trackDriveChk.Enabled = $idle
    $trackArmChk.Enabled = $idle
    $ipTrack.Enabled = $idle
}
```

---

## BLOCK 7 â€” `$mediaTimer` additions: display the track frame + update the state badge + one-shot chime + status-transition log + process-exit & watchdog cleanup

**ANCHOR** â€” find this existing block at the end of `$mediaTimer.Add_Tick` (lines 755â€“781), which displays the Live frame and lock badge. We insert the Tracker equivalent right before it. Find this line (754â€“755):
```powershell
    # Live frame update
    if($script:LiveOn -and $liveSync.Seq -ne $script:lastSeq){
```

**INSERT IMMEDIATELY BEFORE the `# Live frame update` comment line**:
```powershell
    # --- Tracker: process-exit + DRIVE watchdog (UI thread) -----------------
    if($script:TrackOn){
        if($script:TrackProc -and $script:TrackProc.HasExited){
            Add-LogTrack 'Follow process exited; cleaning up.' $amber
            Stop-Tracker $true   # proc already dead
        }
        elseif($script:TrackDrive -and (([datetime]::Now - $script:TrackStart).TotalSeconds -gt $script:TrackMaxSec)){
            Add-LogTrack ("Follow session watchdog ({0}s) - stopping." -f $script:TrackMaxSec) $red
            Stop-Tracker $false
        }
    }
    # --- Tracker: annotated frame + state badge + chime + status-transition log ---
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
        # state badge + audio cue + log only on status-byte transitions
        $tk=[int]$trackSync.Lock
        if($tk -ne $script:trackLastLock){
            Set-TrackBadge $tk
            # one-shot chime on ENTERING FOLLOWING (status 2)
            if($tk -eq 2 -and $script:trackLastLock -ne 2 -and -not $trackMuteChk.Checked){
                try{ [System.Media.SystemSounds]::Asterisk.Play() }catch{}
            }
            if($tk -ne 2 -and $script:trackLastLock -eq 2 -and -not $trackMuteChk.Checked){
                try{ [System.Media.SystemSounds]::Hand.Play() }catch{}   # lost the person
            }
            # log line on each transition (skip the very first paint from -1)
            if($script:trackLastLock -ge 0){
                $msg='SEARCHING'; $c=[System.Drawing.Color]::Gainsboro
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
```

---

## BLOCK 8 â€” `$fpsTimer` addition: Tracker FPS / stalled-stream status line

**ANCHOR** â€” find this existing block inside `$fpsTimer.Add_Tick` (lines 786â€“791):
```powershell
        if($liveSync.Done -and $f -eq 0){ $liveStatus.Text="Stream ended with no frames (camera idle / topic empty)." }
    }
```

**INSERT IMMEDIATELY AFTER that closing `}`** (still inside the `Add_Tick`, before the Control-prime block):
```powershell
    # Tracker FPS heartbeat in the status strip
    if($script:TrackOn){
        $tf=$trackSync.Frames; $td=$tf-$script:trackFpsFrames; $script:trackFpsFrames=$tf
        if($tf -gt 0){ $statusLbl.Text=("Tracker {0}  -  {1} fps  ({2} frames)" -f $ipTrack.Text.Trim(),$td,$tf) }
        elseif($trackSync.Done){ $statusLbl.Text='Tracker stream ended with no frames (camera idle / topic empty).' }
        else { $statusLbl.Text='Tracker: waiting for first annotated frame (~10s camera warmup)...' }
    }
```

---

## BLOCK 9 â€” Single-driver coordination FROM the other side (Control follow & manual connect disable the Tracker toggle)

The Tracker disables the Control follow via `Set-TrackLockout`. We also make the Control follow and the manual controller disable the Tracker toggle while THEY run, so neither side can start the other.

### 9a â€” `Set-FollowLockout` also disables the Tracker toggle

**ANCHOR** â€” find these existing lines (646â€“648, the tail of `Set-FollowLockout`):
```powershell
    $armChk.Enabled = $manual
    $followDriveChk.Enabled = $manual    # can't switch mode mid-follow (toggle itself stays live to switch OFF)
}
```

**REPLACE WITH** (adds Tracker-toggle lockout; guard with `if($trackToggle)` since `Set-FollowLockout` is defined before the Tracker controls are built but only invoked at runtime):
```powershell
    $armChk.Enabled = $manual
    $followDriveChk.Enabled = $manual    # can't switch mode mid-follow (toggle itself stays live to switch OFF)
    # SINGLE LOCO DRIVER RULE: no Tracker follow while the Control-tab follow runs.
    if($trackToggle){ $trackToggle.Enabled = $manual }
    if($trackDriveChk){ $trackDriveChk.Enabled = $manual }
    if($trackArmChk){ $trackArmChk.Enabled = $manual }
}
```

### 9b â€” `Connect-Ctrl` disables the Tracker toggle (manual controller owns loco)

**ANCHOR** â€” find this existing line (599, inside `Connect-Ctrl`):
```powershell
    $followToggle.Enabled=$false; $followDriveChk.Enabled=$false   # manual controller owns loco; no follow while connected
```

**REPLACE WITH**:
```powershell
    $followToggle.Enabled=$false; $followDriveChk.Enabled=$false   # manual controller owns loco; no follow while connected
    if($trackToggle){ $trackToggle.Enabled=$false; $trackDriveChk.Enabled=$false; $trackArmChk.Enabled=$false }   # ...and no Tracker follow either
```

### 9c â€” `Disconnect-Ctrl` re-enables the Tracker toggle

**ANCHOR** â€” find this existing line (614, inside `Disconnect-Ctrl`):
```powershell
    $followToggle.Enabled=$true; $followDriveChk.Enabled=$true   # re-enable follow (DRIVE still needs a fresh ARM)
```

**REPLACE WITH** (only re-enable the Tracker if no Tracker is somehow active â€” defensive; in normal flow it is off):
```powershell
    $followToggle.Enabled=$true; $followDriveChk.Enabled=$true   # re-enable follow (DRIVE still needs a fresh ARM)
    if($trackToggle -and -not $script:TrackOn){ $trackToggle.Enabled=$true; $trackDriveChk.Enabled=$true; $trackArmChk.Enabled=$true }   # re-enable Tracker too
```

---

## BLOCK 10 â€” Wire Tracker events (toggle, DRIVE checkbox, ARM checkbox, Stop, sliders, IP-leave sync)

**ANCHOR** â€” find this existing line (1232, near the end of the "Wire events" section):
```powershell
$ipCtrl.Add_Leave({ if($ipCtrl.Text.Trim()){ $script:RobotIP=$ipCtrl.Text.Trim() } })
```

**INSERT IMMEDIATELY AFTER IT**:
```powershell

# --- Tracker tab ---
# Live slider value labels (integer TrackBars -> float args): Distance 0.1m, Speed 0.01 m/s.
$distTrack.Add_ValueChanged({ $lblDistVal.Text=((Get-TrackStandoff)+' m') })
$spdTrack.Add_ValueChanged({ $lblSpdVal.Text=((Get-TrackVxMax)+' m/s') })
# Follow toggle: ON -> Start-Tracker (drive if DRIVE box ticked); OFF -> Stop-Tracker.
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
    if($script:TrackOn){ return }   # ARM is locked out while a follow runs
    if($trackArmChk.Checked){
        $r=[System.Windows.Forms.MessageBox]::Show("ARM motion?`r`n`r`nThe K1 will PHYSICALLY WALK when you start Follow in DRIVE mode. Clear the area and keep the e-stop handy.",'Arm motion',[System.Windows.Forms.MessageBoxButtons]::OKCancel,[System.Windows.Forms.MessageBoxIcon]::Warning)
        if($r -eq 'OK'){ Add-LogTrack 'MOTION ARMED (Tracker). DRIVE follow now permitted.' $red }
        else { $trackArmChk.Checked=$false }
    } else { Add-LogTrack 'Motion disarmed (Tracker).' $amber }
})
$trackStopBtn.Add_Click({ Stop-Tracker $false })
$ipTrack.Add_Leave({ if($ipTrack.Text.Trim()){ $script:RobotIP=$ipTrack.Text.Trim() } })
```

---

## BLOCK 11 â€” FormClosing stops the Tracker

**ANCHOR** â€” find this existing line (1252, inside `$form.Add_FormClosing`):
```powershell
    try{ Stop-Follow }catch{}; try{ Stop-Live }catch{}; try{ Disconnect-Ctrl }catch{}
```

**REPLACE WITH** (stop the Tracker first, before the others):
```powershell
    try{ Stop-Tracker }catch{}; try{ Stop-Follow }catch{}; try{ Stop-Live }catch{}; try{ Disconnect-Ctrl }catch{}
```

---

## BLOCK 12 â€” Startup hint in the Tracker log (optional but matches the app's idiom)

**ANCHOR** â€” find this existing line inside `$form.Add_Shown` (1246):
```powershell
    Add-LogFiles 'Robot Files: set IP, click "Refresh from robot" to deploy + run tree_manifest.py and load the live SDK layout.' $accent
```

**INSERT IMMEDIATELY AFTER IT**:
```powershell
    Add-LogTrack 'Tracker: set IP, tune Distance + Speed, then toggle Follow. PREVIEW never moves; tick DRIVE + ARM (confirm) to WALK. Show the marker to lock onto a person; the robot then person-follows. STOP / toggle OFF safes the robot.' $accent
```

---

# Design notes / verification summary

- **Protocol reuse (Block 2 + 6):** `$trackReader` is a byte-for-byte clone of `$liveReader` reading `MAGIC=K1F1` + status(1) + len(4 big-endian) + jpeg into a cloned `$trackSync` hashtable. Background runspace writes ONLY `$trackSync`; the UI thread drains it in `$mediaTimer` â€” never touching WinForms off-thread, matching the app's rule.
- **Transport (Block 6):** `Start-Tracker` clones `Start-Live`'s `ProcessStartInfo` exactly â€” `ssh.exe`, `(Get-SshOptString)+" $user@$ip \"<remote>\""`, `UseShellExecute=$false`, `RedirectStandardOutput=$true`, `RedirectStandardError=$false`, `CreateNoWindow=$true`, and crucially **NO `-tt`** (a PTY corrupts the binary JPEG stream). `Stop-Tracker` kills the ssh process; per the verified contract the node traps SIGHUP â†’ stop + `ChangeMode(kPrepare)` and the bridge also safes on stdin-EOF, so no `[char]3`/Ctrl-C is sent (unlike `Stop-Follow`). A pre-launch `pkill -f follow_person_k1.py` clears any orphaned node, mirroring `Start-Live`'s `pkill -f stream_cam.py`.
- **Remote arg build (Blocks 6 + 8):** `run_follow.sh <mode> /boostercamera/head/raw/rgb --stream --standoff-m <D> --vx-max <S>`. Sliderâ†’float: `Get-TrackStandoff` = `distTrack.Value/10.0` (0.6â€“3.0 m, default 1.2), `Get-TrackVxMax` = `spdTrack.Value/100.0` (0.05â€“0.30 m/s, default 0.18), both formatted with `InvariantCulture` so the remote bash always gets a `.` decimal.
- **Safety / single-driver guard:** `Start-Tracker` refuses if `$script:FollowOn` OR `$script:CtrlOn`; DRIVE requires `$trackArmChk.Checked` + an OK/Cancel confirm (cloned from `Start-Follow`). `Set-TrackLockout $true` disables the Control follow toggle + manual Connect while tracking; reciprocally, `Set-FollowLockout` (9a), `Connect-Ctrl` (9b), and `Disconnect-Ctrl` (9c) disable/restore the Tracker toggle. So the two follows can NEVER run at once.
- **Badge + chime + log (Block 7):** big `$trackBadge` colored by status byte (0 gray/SEARCHING, 1 amber/ACQUIRING MARKER, 2 green/LOCKED-FOLLOWING, 3 red/LOST-SHOW MARKER); one-shot `SystemSounds.Asterisk` on entering status 2 (and `.Hand` on leaving it), gated by `$trackMuteChk`; one log line per status-byte transition. Process-exit and a 125 s DRIVE watchdog both call `Stop-Tracker`, mirroring the existing follow watchdog.
- **Lifecycle:** `FormClosing` (Block 11) calls `Stop-Tracker` first; `Stop-Tracker` disposes the PictureBox image + MemoryStream to avoid leaks, mirroring `Stop-Live`.

**Files referenced (all absolute):** `C:\Users\toddm\OneDrive\Desktop\runtime\K1Finder\K1Finder.ps1` (the only file edited). No robot-side file changes are needed â€” `Deploy-FollowFiles` already ships `follow_person_k1.py`, the bridge, and `run_follow.sh`, and the `--stream`/`--standoff-m`/`--vx-max` args pass straight through the launcher per the verified contract.
