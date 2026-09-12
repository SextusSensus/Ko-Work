# Sky Connect

A zero-dependency Windows desktop app that scans your local network to discover a
**Booster K1** humanoid robot and hands you the exact connection details.

Built with PowerShell + Windows Forms, so it runs natively on Windows 11 with
**nothing to install** (no Python, no Node).

The app has six tabs.

### Tab 1 - Discover

1. **Detects** your local IPv4 subnet(s).
2. **Scans** every address on the subnet for an open SSH port (22) and reads the
   SSH banner.
3. **Ranks** each host by how K1-like it is:
   - Default wired IP `192.168.10.102` (strongest signal)
   - On the K1 default subnet `192.168.10.x`
   - Runs Ubuntu/Debian (the K1's OS)
   - Hostname contains `booster` / `k1`
4. **Verifies & connects** — pings + checks SSH on the chosen IP, then gives you
   the SSH command and the Booster SDK connect recipe, and copies `ssh booster@<ip>`
   to your clipboard.
5. **Use in SSH & Files ->** sends the selected IP straight to Tab 2.

### Tab 2 - SSH & Files

Uses Windows' built-in OpenSSH client (`ssh.exe` / `scp.exe` / `ssh-keygen.exe`) -
still zero installs.

- **Robot IP** — type it, or **Pull from Discover** to reuse the discovered IP.
  **Test SSH** checks reachability in-app.
- **Open SSH Terminal** — launches a live shell: `ssh booster@<ip>` (type the
  password `123456` when prompted).
- **Enable passwordless login (install SSH key)** — generates an ed25519 key (if
  you don't have one) and appends it to the robot's `~/.ssh/authorized_keys`. You
  type the password **once**; after that, uploads need no password.
- **Upload files / folders** — add files or a folder, set the remote path
  (default `/home/booster/`), and **Upload to K1** (`scp -r`):
  - Default: opens a console window so you can type the password and watch progress.
  - Tick **Passwordless** (after installing the key) to run the transfer silently
    and see the result inside the app.

### Tab 3 - Live View

A live video feed from the K1's head camera, streamed over SSH (no extra installs).

- Pick a **camera topic** (default `/boostercamera/head/rgb`), **FPS**, and JPEG
  **quality**, then **Start**.
- Under the hood: a small ROS2->JPEG pump (`stream_cam.py`) runs on the robot,
  subscribes to the image topic (best-effort QoS), converts NV12->color, and writes
  length-prefixed JPEG frames to stdout; the app reads them over SSH and paints a
  PictureBox. Live FPS is shown in the status line.
- **Marker lock‑on indicator:** the streamer detects the DICT_4X4_50 marker and
  draws an overlay on the live frame — a center reticle, a green outline + center
  dot on the marker, a `range`/`bearing` readout, and a top banner that reads
  **NO MARKER** (gray) → **ACQUIRING…** (blue) → **LOCKED — READY TO FOLLOW**
  (green) once the marker is seen on 3 consecutive frames. The app shows a matching
  **MARKER LOCKED — READY** badge and plays a chime the moment it locks (and a
  different sound if the lock is lost). Tick **mute lock sound** to silence it.
- **If it says "waiting for camera frames"**, the camera isn't publishing in the
  robot's current state. **Enable cam (beta)** tries the SDK's
  `X5CameraClient.ChangeMode(NormalEnable)`; if it logs `ChangeMode -> 100`, that
  RPC service isn't answering right now (the feed works whenever the robot's vision
  stack is actively streaming - confirmed working when the camera was live).

### Tab 4 - Control

A clickable menu of **every** loco command, driving the robot's own
`b1_loco_example_client` over an SSH stdin pipe (SDK interface `127.0.0.1`).

1. **Connect** (launches the loco client; wait a few seconds for DDS discovery).
2. **Test link (gft)** - sends the read-only GetFrameTransform; a `pos:`/`ori:`
   reply confirms the link **without moving the robot**.
3. Tick **ARM MOTION** (confirms a warning) to enable the motion buttons.

Command groups: **Modes** (Prepare/Walking/Custom/Damping), **Move**
(fwd/back/left/right/turn/STOP), **Head** (up/down/left/right/center),
**Gestures/posture** (Wave, Get-up, Lie-down, Rock/Paper/Scissor/OK/Grasp).

Safety: **DAMPING** and **STOP** are always enabled (even un-armed); everything
that moves the robot is disabled until you ARM. To wave: `Prepare` -> `Walking` ->
`WAVE`.

#### Follow marker (QR / ArUco) - a single toggle

The Control tab has a **Follow Marker (QR)** toggle that makes the K1 track a
printed DICT_4X4_50 ArUco marker (the one-time lock trigger; a raised-hand gesture
is the default trigger).

- **Toggle ON (PREVIEW, default):** runs `follow_person_k1.py` on the robot -
  subscribes to the live head camera, detects the marker (the one-time lock
  trigger), and prints status to the log. **Never moves the robot.**
- Tick **DRIVE (walk the robot)** + **ARM MOTION**, then toggle ON: the K1
  physically walks to hold a standoff behind the marker, driven by a compiled
  `loco_follow_bridge` (`MoveCommand(vx,vy,vyaw)`).
- **Toggle OFF:** sends Ctrl-C -> the robot stops and returns to PREP.

Safety (built + adversarially reviewed): DRIVE is off by default and requires
ARM + a confirm dialog; it refuses if the manual controller is connected (only
one driver of locomotion at a time). Hard velocity clamps live in **both** the
Python and the C++ bridge. The robot stops on lost marker, camera stall (>1s),
a session watchdog (~120s), toggle-off, app close, or SSH drop - every exit path
ends in `MoveCommand(0,0,0) + ChangeMode(kPrepare)`. The compiled bridge is the
backstop: it safes the robot on its own EOF/SIGINT/SIGTERM.

Robot-side files (deployed automatically): `follow_person_k1.py`,
`loco_follow_bridge.cpp` (compiled on first DRIVE), `run_follow.sh`. The camera is
bursty/subscriber-gated (~10s warmup).

### Tab 5 - Robot Files (SDK browser)

Reflects the robot's **actual** Booster SDK file layout so paths are never guessed.

- **Refresh from robot** deploys `tree_manifest.py` to the K1, runs it (one
  Python pass that walks the SDK + `/home/booster`), and pulls back three files
  into the app's temp dir: `k1_tree.txt`, `k1_paths.json`, `k1_paths_list.txt`.
- **Left TreeView**: the live SDK tree (`include/booster/{idl,robot/...}`,
  `example`, `python`, `lib`, `build` binaries) plus `/home/booster`. Folders are
  black, files blue; the SDK root is auto-expanded and selected.
- **Right - Key paths grid**: every canonical path from `k1_paths.json` with an
  **OK** (green) / **MISSING** (red) / **topic** / **n/a** status. A red row is a
  drift signal - the SDK moved and the manifest already has the new location.
- **File preview**: double-click a file in the tree (or an **OK** key row) to scp
  it back and show the first ~400 lines; binaries show size/type only.

The robot-side generator is `tree_manifest.py` (lives next to the app, deployed on
each Refresh). All the app's robot paths were verified against this manifest, so
the loco client, headers, static lib, and helper-script locations are real, not
assumed.

### Tab 6 - Tracker (person-follow)

The person lock-and-handoff follow (`follow_person_k1.py`, deployed on each start
together with `loco_follow_bridge.cpp` + `run_follow.sh`). Lock onto a person via
gesture or ArUco marker, then the robot follows THAT person markerlessly
(YOLO + OSNet deep re-ID over TensorRT on the Orin). PREVIEW never moves;
walking requires **DRIVE (walk)** + **ARM MOTION** + a typed confirm.

- **Perception row**: appearance backend (`global` histogram / `osnet` deep re-ID —
  default osnet; the ReID ONNX is auto-staged and size-verified), `Auto re-acq`
  (passive, audit-only re-lock vote), `Range fence`, and the red **`Arm re-lock`**
  (default OFF — gated on the arm-validation runbook in
  `_follow_autonomy/ARM_VALIDATION_RUNBOOK.md`).
- **REID badge**: live engine health — green `OSNet(TRT/CUDA)`, amber `CPU-EP`,
  red `HIST fallback` / `DEGRADED`. The node refuses armed re-lock on anything
  but a healthy GPU engine.
- **Safety spine** (2026-07 hardening): velocity slew limits, depth-validated
  close-range floor + geofence, depth-fps floor → turn-only, anti-lunge relock
  range gates, DRIVE precondition (refuses to walk on unhealthy sensors), and a
  colored health log (whitelisted node telemetry; faults red/amber). Current
  state + flag reference: `_follow_autonomy/HARDENING_2026-07.md`.

## How to run

- Double-click **`Launch Sky Connect (no console).vbs`** for the clean, window-only experience.
- Or double-click **`Sky Connect.bat`** if you want to see console/debug output too.

Either one opens the app window. Click **Scan for K1**, or type the robot's IP and
click **Verify / Connect**.

## Booster K1 connection facts (what this app is built on)

| Item | Value |
|---|---|
| Default **wired** IP | `192.168.10.102` |
| SSH login | `ssh booster@192.168.10.102` (default password `123456`) |
| SDK transport | **Fast-DDS** (ROS2-compatible); the SDK client connects by passing the robot **IP** |
| Wi-Fi | K1 gets a **dynamic IP** — use the scanned IP, not `.102` |

Example SDK connect (after you have the IP):

```bash
# C++
./b1_loco_example_client 192.168.10.102
# Python
python3 sdk_pybind_b1_example.py 192.168.10.102
```

## Notes / honest caveats

- Discovery keys on **SSH (port 22)** because the K1 runs Linux with SSH enabled.
  A host that has 22 open and is on `192.168.10.x` running Ubuntu ranks "High".
  Other Linux boxes on your LAN may also show up as lower-confidence rows — pick
  the one whose IP/hostname matches your robot.
- Your control PC must be on the **same subnet** as the K1. Wired is recommended.
- If the scan finds nothing: confirm the K1 is powered on, you're on the same
  network, and (on wired) you're in the `192.168.10.x` range.
- This app does **not** drive the robot — it finds it and verifies the link. Actual
  motion control is done with the Booster SDK (Fast-DDS) using the IP it gives you.
