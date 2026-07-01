I now have the full picture. I've verified every anchor. Let me note key findings before writing the corrected blocks.

Verified facts against the file:
- Line 184 `$liveSync` definition â€” anchor exact. âœ“
- Lines 241-242 `$tabFiles`/AddRange â€” exact. âœ“
- Line 458 `$tabFiles.Controls.Add($filesLayout)` â€” exact. âœ“
- Line 472 `Set-RobotIP` â€” exact. âœ“
- Line 468 `Add-LogFiles` â€” exact. âœ“ **But note: `Add-LogFiles` writes to `$previewBox`, not a "files log" â€” fine, that's existing.**
- Line 561-562 end of `Enable-Camera` â€” the anchor `}` is there. âœ“
- Lines 646-648 `Set-FollowLockout` tail â€” exact. âœ“
- Line 599 `Connect-Ctrl` follow-disable line â€” exact. âœ“
- Line 614 `Disconnect-Ctrl` follow-enable line â€” exact. âœ“
- Lines 754-755 `# Live frame update` â€” exact. âœ“
- Lines 790-791 `$fpsTimer` `if($liveSync.Done...` then `}` â€” exact. âœ“
- Line 1232 `$ipCtrl.Add_Leave` â€” exact. âœ“ (it's the last line of the "wire events" before the last_target load)
- Line 1252 FormClosing `try{ Stop-Follow }...` â€” exact. âœ“
- Line 1246 Add_Shown `Add-LogFiles 'Robot Files...'` â€” exact. âœ“

Now the defects I found. The most serious:

**CRITICAL DEFECT 1 â€” DRIVE stop is NOT guaranteed safe with no PTY.** This is the single biggest safety problem and it directly contradicts the prompt's premise. I'll detail it in the review.

**DEFECT 2 â€” `$script:CtrlPrime` priming `Send-Loco 'gft'` fires while Tracker may own loco** â€” actually no, Send-Loco checks CtrlOn. Fine.

**DEFECT 3 â€” `Stop-Tracker` default param vs FormClosing call** â€” `Stop-Tracker` is called with no arg in FormClosing; default `$false` â†’ fine.

**DEFECT 4 â€” `Set-TrackLockout` references `$script:CtrlOn` but does not re-disable on Disconnect**. Minor.

Let me write up the full adversarial review.

---

# Adversarial Safety + Correctness Review â€” Tracker Tab

I read the entire `K1Finder.ps1` (1257 lines). All 12 blocks' anchors are exact and unique, PowerShell 5.1 syntax is valid, no duplicate identifiers, FormClosing stops it. The protocol reader, transport, threading, and disposal are faithful clones of the verified `$liveReader`/`Start-Live`/`Stop-Live`. **But there is one CRITICAL safety defect and several smaller ones.** Fixes below.

---

## CRITICAL DEFECT 1 â€” DRIVE stop is NOT proven safe: `Kill()` an ssh that has NO redirected stdin may not deliver SIGHUP/EOF reliably, and there is no confirmation/fallback

This is the one that can leave a humanoid walking.

The verified contract says: *kill ssh â†’ node traps SIGHUP â†’ clean stop; AND bridge safes on its own stdin-EOF.* The design relies entirely on that. But look at exactly how the Live transport (which this clones) starts ssh:

```powershell
$psi.UseShellExecute=$false; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$false; $psi.CreateNoWindow=$true
```

Note `RedirectStandardInput` is **never set** (correctly â€” a PTY would corrupt the stream, and we don't set stdin either). When you `Kill()` this local `ssh.exe`:

- Killing the **local** Windows `ssh.exe` tears down the TCP connection. The remote `sshd` then sends SIGHUP to the remote session leader and closes the remote command's stdin (EOF). So in the normal case both safety paths DO fire. Good.
- **BUT** there is a real-world failure mode the Live clone never had to care about (Live doesn't move the robot): if the Wi-Fi/TCP link is flaky, a half-open connection can persist for up to `ServerAliveInterval=5` Ã— (default ServerAliveCountMax=3) â‰ˆ **15 seconds** before `sshd` notices the client is gone and sends SIGHUP. During those up-to-15s the robot keeps executing its last `MoveCommand`. The python node self-limits velocity per-frame, but if the *camera* is still feeding "person ahead" frames the bridge keeps driving forward.

The Live View tab tolerates a 15s lag to "stream stops." A walking robot does not. **The design has no independent stop command and no confirmation that the robot actually stopped.** `Stop-Tracker` issues `Kill()` and immediately reports *"robot does stop + PREP via SIGHUP/EOF"* â€” it asserts safety it did not verify.

### Fix â€” add `ServerAliveCountMax=1` to the Tracker ssh so a dead link is detected in ~5s, AND send an explicit best-effort stop over a *second* short-lived ssh on Stop (belt-and-suspenders, independent of the dying stream socket).

Because `Get-SshOptString` is shared, override per-launch by appending options to the remote ssh arg string for the Tracker only. Corrected `Start-Tracker` ssh build + corrected `Stop-Tracker`:

**Corrected BLOCK 6 â€” `Start-Tracker` arg string (replace just the two arg/psi lines):**
```powershell
    # NO -tt: a PTY would corrupt the binary JPEG stream (see Start-Live; opposite of the Control follow).
    # Tighten liveness detection for the WALKING case: a dropped link must trigger remote SIGHUP fast.
    # ServerAliveCountMax=1 -> sshd notices a dead client in ~ServerAliveInterval(5s), not ~15s.
    $argStr=(Get-SshOptString)+' -o ServerAliveCountMax=1'+" $($script:SshUser)@$ip `"$remote`""
```

**Corrected BLOCK 6 â€” `Stop-Tracker` (adds an explicit independent remote stop before/after the kill):**
```powershell
function Stop-Tracker([bool]$procAlreadyDead=$false){
    if(-not $script:TrackOn){ return }
    $trackSync.Stop=$true
    $ip=$ipTrack.Text.Trim()
    # Independent SAFE command over a SECOND short-lived ssh: pkill the node (its SIGTERM handler
    # does stop + ChangeMode(kPrepare)) so the robot is safed even if the streaming socket is half-open
    # and the primary kill's SIGHUP is delayed. Fire-and-forget with a short bounded wait; never blocks UI long.
    if($ip){
        try{ $sp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -TERM -f follow_person_k1.py')) -NoNewWindow -PassThru; $null=$sp.WaitForExit(2500) }catch{}
    }
    # Kill the streaming ssh -> node traps SIGHUP -> stop + ChangeMode(kPrepare); bridge also safes on stdin-EOF.
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
    Add-LogTrack 'Follow stopped (sent pkill + killed ssh; robot does stop + PREP via SIGTERM/SIGHUP/EOF).' $amber
    $statusLbl.Text='Tracker stopped.'
}
```

Note the explicit `pkill` is the same mechanism `Start-Live` already trusts (`pkill -f stream_cam.py`), so it's a proven-on-this-robot operation, not a new assumption. It is gated to â‰¤2.5s so it cannot hang FormClosing.

---

## CRITICAL DEFECT 2 â€” the kill IS issued on all three paths, BUT the FormClosing path can fail to safe the robot because `Add-LogTrack`/UI calls inside `Stop-Tracker` run during form teardown

You asked: *is the kill actually issued on Stop, FormClosing, AND self-exit?*

- **Stop button / toggle OFF:** `Stop-Tracker $false` â†’ kill. âœ“
- **Self-exit (process dies):** `$mediaTimer` detects `HasExited` â†’ `Stop-Tracker $true` â†’ kill (already dead, harmless). âœ“
- **FormClosing (Block 11):** `try{ Stop-Tracker }catch{}`. âœ“ â€” but it runs **after** `$mediaTimer.Stop()` (line 1249), which is correct, and the whole call is wrapped in `try{}catch{}`, so even if a UI control is mid-dispose the `Kill()`/`pkill` still executes (they're early in the function). 

**However:** in `Stop-Tracker`, the new `$ip=$ipTrack.Text.Trim()` reads a WinForms control during FormClosing. If the control is already disposed this throws â€” but it throws *inside* the function before the `Kill()`. **Reorder so the local `Kill()` is unconditional and first, before any control access.** The corrected `Stop-Tracker` above already does the `$ip` read before the kill â€” fix that ordering:

**Corrected ordering (use this `Stop-Tracker` head instead):**
```powershell
function Stop-Tracker([bool]$procAlreadyDead=$false){
    if(-not $script:TrackOn){ return }
    $trackSync.Stop=$true
    # LOCAL kill FIRST and unconditionally â€” never gated behind a WinForms control read that
    # could throw during FormClosing. Tearing down the local ssh starts the remote SIGHUP path.
    try{ if($script:TrackProc -and -not $script:TrackProc.HasExited){ $script:TrackProc.Kill() } }catch{}
    # THEN the independent belt-and-suspenders pkill (control read guarded).
    $ip=''
    try{ $ip=$ipTrack.Text.Trim() }catch{}
    if($ip){
        try{ $sp=Start-Process ssh.exe -ArgumentList ($SSH_OPTS + @(("{0}@{1}" -f $script:SshUser,$ip),'pkill -TERM -f follow_person_k1.py')) -NoNewWindow -PassThru; $null=$sp.WaitForExit(2500) }catch{}
    }
    try{ if($script:TrackPS){ $script:TrackPS.Dispose() } }catch{}
    try{ if($script:TrackRS){ $script:TrackRS.Close() } }catch{}
    $script:TrackProc=$null; $script:TrackPS=$null; $script:TrackRS=$null
    $script:TrackOn=$false; $script:TrackDrive=$false
    # the rest (UI restore) is all wrapped so a disposed control can't abort the safety work above
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
```

---

## DEFECT 3 â€” SINGLE LOCO DRIVER has a real gap: `Disconnect-Ctrl` and `Stop-Follow` unconditionally RE-ENABLE the Tracker controls even though Block 9c guards only `Disconnect-Ctrl`

Block 9a (`Set-FollowLockout`) disables the Tracker toggle while the Control follow runs, and re-enables it when the follow stops (`$manual=$true`). Block 9c guards `Disconnect-Ctrl` with `-not $script:TrackOn`. **But `Set-FollowLockout $false`** (called from `Stop-Follow`) re-enables `$trackToggle`/`$trackDriveChk`/`$trackArmChk` with a bare `$manual` and **no `-not $script:TrackOn` guard.**

In normal flow the Tracker can't be on while a Control follow runs (they mutually refuse to *start*). So `Stop-Follow` re-enabling them is harmless *in practice*. But it's a latent footgun and contradicts the "airtight" claim. Tighten Block 9a to never enable Tracker controls while the Tracker is somehow active:

**Corrected BLOCK 9a:**
```powershell
    $armChk.Enabled = $manual
    $followDriveChk.Enabled = $manual    # can't switch mode mid-follow (toggle itself stays live to switch OFF)
    # SINGLE LOCO DRIVER RULE: no Tracker follow while the Control-tab follow runs.
    # Never ENABLE the Tracker controls if the Tracker itself is active (defensive; normal flow they can't co-exist).
    $trackIdle = $manual -and -not $script:TrackOn
    if($trackToggle){ $trackToggle.Enabled = $trackIdle }
    if($trackDriveChk){ $trackDriveChk.Enabled = $trackIdle }
    if($trackArmChk){ $trackArmChk.Enabled = $trackIdle }
}
```

The mutual *refuse-to-start* is otherwise airtight: `Start-Tracker` refuses if `$script:FollowOn` OR `$script:CtrlOn`; `Start-Follow` (line 652) refuses if `$script:CtrlOn` but **does NOT refuse if `$script:TrackOn`** â€” that's a hole. Add the reciprocal guard:

**Additional required edit â€” `Start-Follow` must refuse while the Tracker is running.** Anchor (lines 651-655):
```powershell
    if($script:FollowOn){ return $false }
    if($script:CtrlOn){
        [System.Windows.Forms.MessageBox]::Show("The manual controller is connected. Disconnect it first - only one process may drive locomotion at a time.",'Follow blocked','OK','Warning')|Out-Null
        return $false
    }
```
**Insert after the `$script:FollowOn` line:**
```powershell
    if($script:TrackOn){
        [System.Windows.Forms.MessageBox]::Show("The Tracker (tab 6) follow is running. Stop it first - only one process may drive locomotion at a time.",'Follow blocked','OK','Warning')|Out-Null
        return $false
    }
```
Without this, the Control follow's toggle is *disabled* by `Set-TrackLockout` while the Tracker runs (Block 6) â€” so the UI blocks it. But disabling is UI-only; the code-level refuse is the real guard. Add it. **(This is a NEW edit not in the original 12 blocks â€” it must be included.)**

---

## DEFECT 4 â€” `Set-TrackLockout` disables `$followToggle`/`$connCtrlBtn` but a later `Disconnect-Ctrl`/`Stop-Follow` can re-enable them mid-Tracker

Symmetric to Defect 3. `Set-TrackLockout $true` disables `$connCtrlBtn` and `$followToggle`. But those controls' enabled-state is also written by `Connect-Ctrl`/`Disconnect-Ctrl`/`Set-FollowLockout`. While the Tracker is running, none of those *run* (they're blocked), so no live conflict â€” but the fix in Defect 3 (guarding with `-not $script:TrackOn`) closes this from the other side too. With both guards in place the two subsystems are airtight. No further change to `Set-TrackLockout` needed. **OK as-is** given Defect 3's fix.

---

## DEFECT 5 â€” sliderâ†’arg values are NOT clamped if a TrackBar is somehow driven out of range; fine in practice, but verify

`distTrack.Minimum=6/Maximum=30`, `spdTrack.Minimum=5/Maximum=30`. WinForms TrackBar hard-clamps `.Value` to `[Minimum,Maximum]`, so `Get-TrackStandoff` âˆˆ [0.6,3.0] and `Get-TrackVxMax` âˆˆ [0.05,0.30] by construction â€” cannot exceed the stated ranges. `InvariantCulture` guarantees a `.` decimal for the remote bash. The contract says the bridge hard-clamps anyway. **OK as-is.**

---

## DEFECT 6 â€” binary protocol reader: correct, but confirm status-byte-before-length and bounded length

`$trackReader` is byte-identical to the verified `$liveReader`: reads status byte *before* the 4-byte big-endian length, resyncs on bad magic (`elseif($b -eq $magic[0]){ $m=1 }`), bounds length `if($len -le 0 -or $len -gt 8000000){ continue }`, and `ReadExact` returns `$null` on truncation to break cleanly. Writes ONLY `$trackSync`. **OK as-is.**

---

## DEFECT 7 â€” threading / GDI: correct

Background runspace writes only the synchronized `$trackSync`. All WinForms access (PictureBox, badge, log, sliders) is on the UI thread inside `$mediaTimer`/`$fpsTimer`/event handlers. PictureBox `Image` + backing `MemoryStream` are disposed on swap (Block 7) and on stop (Block 6), exactly mirroring Live View. No off-thread WinForms touch. **OK as-is.**

---

## DEFECT 8 â€” minor: `Start-Tracker` pre-launch `pkill` uses `-f follow_person_k1.py`, but the bridge process is separate

`run_follow.sh` launches both the python node and the compiled `loco_follow_bridge`. The pre-launch cleanup only `pkill`s `follow_person_k1.py`. The contract says the bridge safes on its *own* stdin-EOF, and killing the python node closes the pipe feeding the bridge â†’ bridge gets EOF â†’ `MoveCommand(0,0,0)+kPrepare`. So killing only the python node cascades correctly. The same applies to the Defect-1 `pkill`. **OK as-is** (matches the verified single-pkill recipe Live/Follow already rely on).

---

## Blocks that are correct as written

- **BLOCK 1** (TabPage + AddRange) â€” anchor exact, syntax OK. **OK as-is.**
- **BLOCK 2** ($trackSync + state) â€” clones $liveSync exactly; no dup identifiers. **OK as-is.**
- **BLOCK 3** (Tracker UI) â€” TableLayoutPanel idiom matches; control coords don't overlap; `$trackMuteChk` added is fine. **OK as-is.**
- **BLOCK 4** (Set-RobotIP `$ipTrack` sync) â€” guarded like `$ipFiles`. **OK as-is.**
- **BLOCK 5** (Add-LogTrack) â€” matches Add-LogTo idiom. **OK as-is.**
- **BLOCK 7** ($mediaTimer additions) â€” inserted before `# Live frame update`; watchdog + display + badge + chime + transition-log all correct; uses guarded disposal. **OK as-is.**
- **BLOCK 8** ($fpsTimer Tracker heartbeat) â€” inserted after the `liveSync.Done` line, before the Control-prime block; correct scope. **OK as-is.**
- **BLOCK 9b** (Connect-Ctrl disables Tracker) â€” exact. **OK as-is.**
- **BLOCK 9c** (Disconnect-Ctrl re-enables, guarded by `-not $script:TrackOn`) â€” correct. **OK as-is.**
- **BLOCK 10** (wire events) â€” anchor (line 1232) exact; toggle guard + slider labels + ARM confirm all correct. **OK as-is.**
- **BLOCK 11** (FormClosing) â€” `Stop-Tracker` first, exact anchor. **OK as-is** (works with the reordered Stop-Tracker above).
- **BLOCK 12** (Add_Shown hint) â€” exact anchor. **OK as-is.**

---

## Summary of REQUIRED changes (must apply, beyond the original 12 blocks)

1. **Block 6 / `Start-Tracker`**: append `-o ServerAliveCountMax=1` to the Tracker ssh arg string (fast dead-link detection for the walking case). *(Corrected line above.)*
2. **Block 6 / `Stop-Tracker`**: local `Kill()` FIRST/unconditional; add an independent bounded `pkill -TERM -f follow_person_k1.py` over a second ssh; wrap UI restore in `try/catch` so a disposed control can't abort the safety work. *(Full corrected function above.)*
3. **Block 9a / `Set-FollowLockout`**: gate Tracker-control re-enable with `-not $script:TrackOn`. *(Corrected above.)*
4. **NEW edit to `Start-Follow`** (line 651): add a `if($script:TrackOn){ ...refuse... }` guard so the code-level single-driver rule is symmetric. *(Snippet above.)*

Everything else is faithful to the verified Live/Follow patterns and safe.

---

## Ordered integration + on-robot test checklist

**Integration (apply in this order):**
1. Block 1 (tab) â†’ 2 (state) â†’ 3 (UI) â†’ 4 (Set-RobotIP) â†’ 5 (Add-LogTrack) â†’ 6 (engine, **with the two Block-6 corrections**) â†’ 7 ($mediaTimer) â†’ 8 ($fpsTimer) â†’ 9a/9b/9c (**9a corrected**) â†’ **NEW Start-Follow guard** â†’ 10 (events) â†’ 11 (FormClosing) â†’ 12 (hint).
2. Lint without running: `powershell -NoProfile -Command "[void][System.Management.Automation.Language.Parser]::ParseFile('C:\Users\toddm\OneDrive\Desktop\runtime\K1Finder\K1Finder.ps1',[ref]$null,[ref]$errs); $errs"` â€” expect zero parse errors.
3. Launch the app. Confirm tab "6. Tracker" appears and all controls render without overlap.

**On-robot â€” PREVIEW FIRST (no motion, mandatory before any drive):**
4. Set IP (or Verify on Discover; confirm it propagates to the Tracker IP box via `Set-RobotIP`).
5. Leave DRIVE + ARM **unchecked**. Toggle Follow ON â†’ confirm it launches in PREVIEW, annotated video appears after ~10s warmup, FPS heartbeat increments in the status strip.
6. Show the marker â†’ badge goes ACQUIRING MARKER (amber) â†’ LOCKED-FOLLOWING (green), Asterisk chime fires once, log shows the transitions. Hide marker / occlude person â†’ LOST-SHOW MARKER (red) + Hand chime. **Confirm the robot never moves in preview.**
7. Toggle OFF â†’ video stops, badge â†’ IDLE, log "Follow stopped". Confirm on the robot (ssh `pgrep -af follow_person_k1.py`) that no node lingers.
8. Single-driver: with Tracker running, confirm the Control-tab Follow toggle + Connect are disabled; stop Tracker, start Control follow, then confirm the Tracker toggle is disabled and `Start-Follow`/`Start-Tracker` both refuse with the message box.

**On-robot â€” CAUTIOUS DRIVE (only after preview is fully green and camera FPS is healthy):**
9. Clear a large area; spotter on the e-stop. Set Distance ~1.5 m, Speed to the **minimum 0.05 m/s**.
10. Tick ARM (confirm dialog) â†’ tick DRIVE â†’ toggle Follow ON (confirm DRIVE dialog). Show marker, let it lock, take **one step**; verify the robot follows and holds standoff.
11. **Stop tests (do all three):** (a) press STOP â†’ robot halts + PREP within ~1s; (b) re-arm, start, then toggle OFF â†’ halts; (c) re-arm, start, then close the app window â†’ FormClosing safes it. After each, ssh-verify the node is gone and the robot is in Prepare.
12. Only then raise Speed toward 0.18 m/s incrementally, re-testing STOP at each step. Confirm the ~125s watchdog auto-stops a long run.

**Files referenced (absolute):** `C:\Users\toddm\OneDrive\Desktop\runtime\K1Finder\K1Finder.ps1` (only file edited).
