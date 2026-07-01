# K1 Finder — UI Re-Redesign Plan

**Target:** rewrite the view layer of `K1Finder.ps1` in **PowerShell + WPF (XAML)**, keep all 6 tabs and all behavior, apply a **refined "current hybrid"** visual system (light body + dark header/log/video, one accent, consistent spacing, clear button hierarchy), and make the whole UI **DPI- and resize-safe by default**. Zero-install (WPF ships with .NET on Windows 11).

Locked decisions (2026-06-24):
- Scope: **layout + visual overhaul** — structure (6 tabs) and behavior unchanged.
- Stack: **PowerShell + WPF / XAML** view layer.
- Look: **refine the current hybrid** — make today's white-body / dark-chrome mix consistent and intentional.

---

## 1. Guiding principles

1. **Behavior is sacred.** The entire backend — ssh transport, the `K1F1` MAGIC+status+len+jpeg protocol, the runspaces, the safety/coordination logic, the 3-layer velocity clamps — is UI-agnostic and migrates **verbatim**. This redesign touches the view layer only.
2. **Zero absolute positioning.** Every `Location='x,y'` is replaced by WPF `Grid` / `DockPanel` / `StackPanel` / `WrapPanel` / `UniformGrid`. The window then resizes and DPI-scales for free.
3. **One visual system, applied centrally.** Colors, fonts, spacing, and every button/card/badge come from a single `ResourceDictionary`. No per-control ad-hoc styling.
4. **The four async surfaces share one pattern.** Discover, Live View, Tracker, and Robot Files each handle loading / empty / error / data in the *same* idiom (overlay text, warmup hint, console error line + a way forward, then data).
5. **Safety is visually privileged.** `STOP` / `DAMPING` are always Danger-styled and always enabled. `ARM` is a stateful toggle. Motion buttons are Secondary and disabled until armed. Exactly one Primary button per pane.

---

## 2. Architecture — what moves vs. what stays

### Stays (copy verbatim — UI-agnostic, do not edit)
- **Synchronized state:** `$sync`, `$liveSync`, `$trackSync`, `$ctrlSync`, `$followSync`, `$filesSync`, `$viewSync`.
- **Background runspaces:** `$scanScript`, `$liveReader`, `$trackReader`, the follow reader, the Robot-Files refresh/view workers.
- **Transport & protocol:** `Deploy-RobotFiles`, `Deploy-FollowFiles`, `Get-SshOptString`, `$SSH_OPTS`, the `K1F1` framing, the **NO `-tt`** rule for binary streams, `SSH_ASKPASS`.
- **Safety & coordination:** single-camera-consumer rule, single-loco-driver lockouts (`Set-TrackLockout`, `Set-FollowLockout`, Connect/Disconnect gating), ARM/DRIVE confirm dialogs, the ~125 s watchdogs, SIGHUP/EOF clean-stop → `ChangeMode(kPrepare)`, and the sacred 3-layer clamps (robot-side, untouched).
- **Pure logic:** `Get-TrackStandoff`, `Get-TrackVxMax`, `Score-Candidate`, `Get-ConnectRecipe`, `Test-K1Reachable`.

### Changes (view layer only)
| Today (WinForms) | Becomes (WPF) | Notes |
|---|---|---|
| `Form` + `TabControl` + `StatusStrip` | `Window` + styled `TabControl` + status bar | XAML shell |
| 3× `System.Windows.Forms.Timer` (`$timer` 250 ms, `$mediaTimer` 40 ms, `$fpsTimer` 1 s) | 3× `System.Windows.Threading.DispatcherTimer` | **Tick bodies port nearly verbatim** — they already run on the UI thread and only *read* the hashtables |
| Direct `$ctrlVar` references | `x:Name` in XAML → one `$UI` lookup built once via `FindName` | No ad-hoc element reaching |
| `PictureBox.Image` from `MemoryStream` | `Image.Source` from a **frozen `BitmapImage`** | The one genuinely new idiom; `.Freeze()` lets the JPEG decode move off the UI thread |
| `RichTextBox` + `SelectionColor` | WPF `RichTextBox` `FlowDocument` `Run`, behind one `Add-LogTo` | Colored console lines |
| `ListView` Details | WPF `ListView` + `GridView` | Discover results, Key-paths |
| WinForms `TreeView` | WPF `TreeView` + `HierarchicalDataTemplate` | Robot Files |
| `TrackBar` / `CheckBox Appearance=Button` / `ProgressBar` | `Slider` / `ToggleButton` / `ProgressBar` | |
| `Forms.MessageBox`, `OpenFileDialog` | `System.Windows.MessageBox`, `Microsoft.Win32.OpenFileDialog` | Folder picker: keep `Forms.FolderBrowserDialog` (still zero-install) |

### The three real adaptation points (everything else is mechanical)
1. **Timers → DispatcherTimer.** Same cadence, same handler logic. Hard rule (already true today, now enforced by WPF): **runspaces touch only hashtables; the DispatcherTimer (UI thread) does all UI.**
2. **Video painting → frozen BitmapImage.** Decode JPEG bytes to a `BitmapImage` with `CacheOption=OnLoad`, `.Freeze()`, assign to `Image.Source`; release the previous source each frame. Validate FPS parity against WinForms early (Phase 3).
3. **Element access → `$UI` map.** Build `$UI = @{}` once from named XAML elements; reference `$UI.trackToggle` etc. everywhere.

---

## 3. The design system (one ResourceDictionary)

**Brushes**
- `Accent` `#0078D7` · `Ok` `#107C10` · `Warn` `#CA7C00` · `Danger` `#C42B1C`
- `HeaderDark` `#202225` · `Console` `#202225` (text `Gainsboro`) · video `#15171A`
- `Surface` `#FFFFFF` · `SurfaceAlt` `#F3F4F6` · `Border` `#E1E3E6`
- `TextPrimary` `#202225` · `TextMuted` `#6B7075`

**Typography** — UI = Segoe UI (Body 13 / Section 14 Bold / Title 18 Bold); Mono = Consolas (telemetry, logs, IPs, paths).

**Spacing scale** 4 / 8 / 12 / 16 / 24 · control height 32 · card padding 12 · gutter 12 · radius 4.

**Named styles**
- `BtnPrimary` (accent fill, white) — the single main action per pane.
- `BtnSecondary` (surface + border) — ordinary actions.
- `BtnDanger` (red fill) — STOP, DAMPING; always enabled.
- `ToggleArm` / `ToggleFollow` — `IsChecked` DataTriggers drive color/label (Off → Preview → Driving).
- `Card` — GroupBox replacement: header label + bordered surface.
- `Console`, `Telemetry`, `Badge` (status pill colored by bound state byte 0/1/2/3 → gray/amber/green/red).

> The live app keeps `STOP` and `ARM MOTION` uppercased for emphasis even though the rest of the UI is sentence-case; that is a deliberate safety affordance.

---

## 4. Tab-by-tab (structure preserved, layout rebuilt)

1. **Discover** — action card (Scan / Stop / IP / Verify / Progress) docked top; results `ListView` fills; console docked bottom. Confidence shown as a colored pill. *Empty:* "No hosts yet — Scan, or type the IP and Verify."
2. **SSH & Files** — vertical stack of `Card`s (Robot / SSH / Upload) over a console; upload list + remote path on a 2-column grid.
3. **Live View** — control card top in a `WrapPanel` (reflows on narrow widths); video `Image` fills; MARKER pill; console bottom.
4. **Control** — connection card; command grid via `UniformGrid` sections (Modes / Move / Head / Gestures) replacing pixel coords; Follow-marker card; console. ARM prominent; STOP/DAMPING Danger + always enabled; motion buttons Secondary + disabled until armed.
5. **Robot Files** — toolbar card + horizontal `GridSplitter`: `TreeView` left; (Key-paths `ListView` over file-preview console) right.
6. **Tracker** — the cockpit (see mockup): control card (IP + Distance + Speed sliders + Follow toggle + DRIVE + ARM + STOP), large annotated video, large state badge, transition log. Biggest beneficiary of real layout.

---

## 5. Phasing (each phase independently runnable)

**Status (2026-06-25):** Phase 0 ✅, Phase 1 ✅, Phase 2 ✅ complete and verified (`K1Finder.wpf.ps1`, launched via `K1 Finder (WPF).bat`). Discover tab proven end-to-end: real scan → empty state; injected results → bound/colored/sorted rows. Phases 3–7 remain.

- **Phase 0 — Scaffold.** New `K1Finder.wpf.ps1`. Load `PresentationFramework`/`PresentationCore`/`WindowsBase`. XAML shell (Window + dark header + tab strip + status bar), theme `ResourceDictionary`, `$UI` FindName map, 3 `DispatcherTimer`s wired to empty bodies. Window opens and shows the shell.
- **Phase 1 — Port the backend verbatim.** Paste every UI-agnostic function, runspace, and hashtable unchanged. No behavior edits. Goal: it loads with the new shell, nothing wired yet.
- **Phase 2 — Discover end-to-end.** First fully-wired tab → proves the DispatcherTimer-drain + `ListView` + console + scan-runspace pattern. Becomes the reference for the rest.
- **Phase 3 — Live View + Tracker.** The frozen-`BitmapImage` video path + status badges. Highest-risk migration (binary stream → WPF paint); do them together and measure FPS parity.
- **Phase 4 — Control + Follow, and SSH & Files.** Command `UniformGrid`, ARM/STOP hierarchy, the lockout web; uploads/keygen/terminal.
- **Phase 5 — Robot Files.** `TreeView` + key-paths `ListView` + preview, including the `Complete-*` UI-thread completion handlers.
- **Phase 6 — Polish pass** against the self-critique checklist: keyboard + tab order, all four states per surface, resize from min size, 150 %/200 % DPI, contrast, no gratuitous motion, one Primary per pane. Screenshot + compare to WinForms.
- **Phase 8 — K1 Vitals window (queued).** A second WPF window auto-opened at launch, showing live robot vitals as a diagram (humanoid joint heatmap + battery + IMU/fall + cameras + compute). Read-only telemetry monitor (no loco commands; safe alongside follow/control). Full spec + data sources + open items in [VITALS.md](VITALS.md). Built **after** Phase 7.

- **Phase 7 — Cutover (in place).** Prefer **overwriting `K1Finder.ps1` in place** (save the old body as `K1Finder.winforms.bak`) so both launchers *and* the two `K1Finder.ps1` lint paths in `.claude/settings.local.json` keep resolving with zero edits. Develop and cut over **inside the live `K1Finder` folder** so the new script sits beside the continuously-updated helper scripts. If you instead rename to `K1Finder.wpf.ps1`, you must repoint **both** launchers in lockstep, update the README, *and* update the two `ParseFile` rules in `.claude/settings.local.json` (Phase 7's original file list omitted it).

---

## 6. Risks & mitigations

- **Binary JPEG → WPF paint.** Freeze the `BitmapImage` (decode off-thread, assign on UI), keep the 40 ms DispatcherTimer, release the prior source each frame. Prove FPS parity in Phase 3 before porting more.
- **WPF's stricter threading.** Enforce the existing rule: runspaces write only hashtables; only the DispatcherTimer touches UI.
- **Element-reference sprawl.** Build `$UI` once; never reach into XAML ad hoc.
- **No native folder picker in WPF.** Reuse `Forms.FolderBrowserDialog` (load the assembly) — still zero-install.
- **Scope creep into behavior.** Hard rule: Phase 1 backend is copy-only; any behavior change is out of scope for this redesign.

---

## 7. Acceptance criteria

- [ ] Zero absolute `Location` coordinates remain.
- [ ] Window resizes from minimum to maximized with no clipped/overlapping controls; crisp at 150 %/200 % DPI.
- [ ] Every button maps to a named style; exactly one Primary per pane; STOP/DAMPING always Danger + enabled.
- [ ] All six tabs reach feature parity with the WinForms app (verified against README behaviors).
- [ ] Safety verified intact: ARM gating, DRIVE confirm, single-driver / single-camera lockouts, watchdogs, clean stop on close.
- [ ] Self-critique checklist passes; before/after screenshots captured.

---

## 8. Porting-fidelity rules (verified by the 2026-06-24 breakage audit)

These are the ways a careless port silently *regresses* behavior. **None of them affect the running WinForms app** — they are guards for the rewrite itself. (Audit: 4 grounded readers + 3 adversarial skeptics; robot-side scripts need **zero** changes; WPF is already installed on this machine; both launchers already pass `-STA`, WPF's one hard requirement.)

1. **Port from the LIVE `K1Finder.ps1` by hash — never a backup.** The live file (111,045 bytes, 6/23 20:20) is newer than `_opt_backup_…\K1Finder.ps1` and `K1Finder.ps1.bak_20260623_201956` (both ~17:26). The ~11-line delta is *exactly* the single-camera-consumer / single-loco-driver cross-tab guards. Forking from a backup (or the README's stale "five tabs") yields a robot that can run **two camera consumers / two loco drivers at once** — and "all 6 tabs present" does **not** catch it (the backups already contain the Tracker tab). Diff the finished WPF backend against the live file to prove zero behavioral delta.
2. **Keep JPEG decode on the UI thread (inside the DispatcherTimer tick).** WPF throws on off-thread `Image.Source` assignment of an unfrozen bitmap (thread affinity, verified empirically). Mirror today's WinForms tick: the runspace ships raw bytes; the tick decodes + paints. Only move decode off-thread if you `.Freeze()` the bitmap on the worker first.
3. **Slider must snap to integer ticks.** `Slider.Value` is `Double`; `Get-TrackStandoff/VxMax` divide by 10/100 to build `--standoff-m/--vx-max`. Without `IsSnapToTickEnabled=true`, a drag to 12.7 sends `1.27 m` instead of `1.2`. Set Min/Max/TickFrequency to the TrackBar's (6..30 / 5..30), snap to ticks, and cast `[int]` before dividing.
4. **ComboBox items must be plain strings (or read `.Content`).** XAML `<ComboBoxItem>` makes `SelectedItem` an object; interpolating it into the `run_stream.sh` command yields `"System.Windows.Controls.ComboBoxItem: /boostercamera…"` and corrupts argv → no frames. Populate Topic/FPS/Q via `ItemsSource = @('…')` strings, or read `.Content`.
5. **Keep the loco command bytes exact.** Preserve `StandardInput.NewLine = "` + "`" + `n"` (LF, not CRLF — the robot's `getline` matches literal tokens like `gft`) and the per-button code (via `CommandParameter` or `Tag`), or commands silently no-op.
6. **`Window.Closing` must safe the robot.** Map `Add_FormClosing` → `Closing` and keep calling `Stop-Tracker/Stop-Follow/Stop-Live/Disconnect-Ctrl` in order (local `Kill` first, then remote `pkill`) so a running DRIVE session is safed on app close.
7. **Wrap XAML construction in try/catch (`-ErrorAction Stop`).** The script's `Set-StrictMode -Off` + `$ErrorActionPreference='SilentlyContinue'` will swallow a malformed-XAML exception → `$window` stays `$null`, the app exits 0, and via the no-console `.vbs` there's nothing to reveal the error. Surface failures with a MessageBox.
8. **Keep runspaces MTA, the Window STA.** Every reader runspace sets `ApartmentState='MTA'`; the launchers pass `-STA`. Keep both; add a top-of-script STA guard that fails loudly if launched MTA.

> **Audit corrected one wrong finding:** a first-pass reader claimed WPF's `MessageBox` enum return makes `if($r -eq 'OK')` silently false and inverts the ARM/DRIVE gates. PowerShell coerces the enum to its string for `-eq`, so `[MessageBoxResult]::OK -eq 'OK'` is `$true`. The confirm gates work unchanged — do **not** "fix" them.
