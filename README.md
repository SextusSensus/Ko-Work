# Ko-Work

Tooling for discovering, connecting to, and running follow-behaviours on a
**Booster K1** humanoid robot.

The repo is two layers that work together:

1. **K1 Finder** — a zero-install Windows desktop app (PowerShell + Windows Forms)
   that finds the robot on your LAN, opens an SSH shell, streams the head camera,
   drives loco commands, and toggles the follow behaviours.
2. **Robot-side autonomy stack** — the Python nodes and C++ bridges that actually
   run *on* the K1 (markerless person-follow, camera streamer, loco bridge), plus
   the design/safety docs behind them.

> ⚠️ **This drives a humanoid robot.** Every motion path is off by default and
> gated behind an explicit ARM step. Read [Safety](#safety) before running
> anything in `--drive`.

---

## Repo map

Organized by **deploy target** (P2.1):

| Path | What it is |
|---|---|
| `desktop/K1Finder.ps1` · `K1 Finder.bat` · `Launch K1 Finder (no console).vbs` | The Windows K1 Finder app (main entry point) + launchers. |
| `desktop/README.md` | **Full app documentation** (all six tabs, in detail). |
| `robot/follow_person_k1.py` | Robot-side markerless person-follow (lock-and-handoff: ArUco/gesture is a one-time trigger, then YOLO tracks the person). |
| `robot/loco_follow_bridge.cpp` | Compiled-on-robot bridge that turns follow velocities into Booster SDK `MoveCommand`s; the hard-clamp backstop. |
| `robot/config/` | YAML config (`defaults.yaml` + `dev`/`demo`/`field` profiles); the node loads it fail-closed. |
| `robot/stream_cam.py` · `enable_camera.cpp` · `tree_manifest.py` · `run_*.sh` | Camera pump, SDK camera-enable helper, file-tree manifest, and the launch scripts. |
| `eval/` | The `replay_eval` regression harness, `config_selftest`, and the offline `rrd_*` labeling tools. |
| `models/` | Model blobs (fetched/exported, gitignored) + the offline `wheels/` closure. |
| `docs/` | Design docs, hardening plans, `SCOPE_*.md`, `UNTETHERED_FOLLOW.md`, adversarial-safety gate docs. |

---

## Quick start (K1 Finder app)

Requires only **Windows 11** — no Python or Node install needed; the app uses
built-in PowerShell, Windows Forms, and the OpenSSH client.

1. Power on the K1 and put your PC on the **same subnet** (wired is recommended).
2. In `desktop/`, double-click **`Launch K1 Finder (no console).vbs`** (clean
   window) or **`K1 Finder.bat`** (with console/debug output).
3. Click **Scan for K1** — or type the robot IP and click **Verify / Connect**.

From there the app's six tabs cover discovery, SSH/file upload, live camera view,
loco control, an SDK file browser, and the Tracker (person-follow). See
**[`desktop/README.md`](desktop/README.md)** for the complete tab-by-tab walkthrough.

---

## Connection facts

| Item | Value |
|---|---|
| Default **wired** IP | `192.168.10.102` |
| SSH login | `ssh booster@192.168.10.102` (default password `123456`) |
| SDK transport | **Fast-DDS** (ROS2-compatible); the SDK client connects by IP |
| Wi-Fi | K1 gets a **dynamic IP** — use the scanned IP, not `.102` |

Robot-side environment (sourced before the follow nodes run):

```bash
source /opt/ros/humble/setup.bash
source /opt/booster/BoosterRos2/install/setup.bash
```

---

## Follow behaviours

One follow node runs on the robot; it defaults to **PREVIEW** (detect + print,
never move) and only drives when explicitly armed.

- **Person follow** (`follow_person_k1.py`) — *lock-and-handoff*: an ArUco marker
  (or a raised-hand gesture) is a one-time trigger to lock onto the human standing
  at it, after which the robot follows **that person** markerlessly (YOLO person
  detection + colour signature), re-seeding only to recover from a hard loss. See
  the module docstring for the full state machine (`SEARCH_MARKER → SEEDED → TRACK
  → REACQUIRE`).

It is toggled from the app's **Control** tab.

---

## Safety

The motion paths were built and adversarially reviewed. In `--drive` mode:

- **Off by default.** Preview mode never spawns the bridge and never moves the robot.
- **ARM required.** Driving needs ARM + a confirm dialog, and refuses if the manual
  controller is connected (only one driver of locomotion at a time).
- **Hard velocity clamps in both layers** — the Python node *and* the compiled C++
  bridge.
- **Stops on:** lost target, camera stall (>1s), session watchdog, toggle-off,
  app close, or SSH drop. Every exit path ends in
  `MoveCommand(0,0,0) + ChangeMode(kPrepare)`.
- **Bridge is the backstop** — it safes the robot on its own EOF / SIGINT / SIGTERM.

**Untethered operation is currently FORBIDDEN** — going wireless removes the SSH
link that today acts as the deadman. See
[`docs/UNTETHERED_FOLLOW.md`](docs/UNTETHERED_FOLLOW.md) for the pre-flight
gate that must be cleared first.

---

## License

See [`LICENSE`](LICENSE).
