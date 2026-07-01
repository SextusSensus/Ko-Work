# K1 Vitals window — spec (queued)

**Decision (2026-06-25):** build **after** the WPF Discover→Live→Tracker→Control→Robot-Files tabs.
**v1 scope = everything** (full diagram). A second WPF `Window`, auto-opened when K1 Finder launches
(with a toggle to suppress), showing live robot vitals as a diagram. Mockup: the
`k1_vitals_window_mockup` rendered in chat 2026-06-25.

This is a **read-only telemetry monitor** — SDK *subscribers* only, never a command. It therefore
does NOT engage the single-loco-driver rule and is safe to stream continuously alongside
follow/control. No `MoveCommand` / `ChangeMode` ever.

## Data sources (from the SDK manifest `k1_tree.txt`)

| Vitals | SDK source | Prebuilt reader on robot |
|---|---|---|
| Joints — pos/vel/torque/**temp**/mode·error | `idl/b1/LowState.h`, `MotorState.h` | `b1_low_level_subscriber` (exe) |
| IMU — rpy/gyro/accel | `idl/b1/ImuState.h` (inside LowState) | (LowState) |
| Fall state | `idl/b1/FallDownState.h` | — |
| Battery — SoC/V/A/temp | `idl/b1/BatteryState.h` + `BoosterBatteryDaemon` | `battery_state_subscriber` (exe) |
| Odometry — x/y/yaw/vel | `idl/b1/Odometer.h` | `odometer_example` (exe) |
| Mode / process state | `b1_loco_client` GetMode/GetStatus, `RobotProcessState.h` | `b1_loco_example_client` (used) |
| Cameras — publishing + fps | `/boostercamera/*` ROS2 topics | `ros2 topic hz` |
| Compute — CPU/GPU temp, mem | Jetson `/sys/class/thermal`, psutil | shell |

## Architecture (reuses the proven streaming pattern)

- **Robot side — `k1_vitals` gatherer:** merge LowState (joints+IMU), BatteryState, Odometer,
  FallDownState + `/sys` thermals + camera `ros2 topic hz` into **one JSON line at ~5 Hz on stdout**.
  Deployed + run over SSH exactly like `stream_cam.py`. Two build options, decide after reading the
  exe output formats live: (a) a small purpose-built C++ subscriber compiled on the robot like
  `loco_follow_bridge`, emitting JSON; or (b) a Python wrapper merging the prebuilt exes
  (`b1_low_level_subscriber`, `battery_state_subscriber`, `odometer_example`).
- **PC side — 2nd WPF Window:** an SSH-reader runspace writes a synchronized hashtable; a
  `DispatcherTimer` drains it on the UI thread (same pattern as the Phase 2 Discover tab) → a
  humanoid joint heatmap on a `Canvas` + battery/IMU/fall/camera/compute panels. Auto-open at launch
  with a toggle in the main window; refined-hybrid theme (shared `ResourceDictionary`).

## Open items to verify on the robot (it was OFFLINE on .81 and .102 on 2026-06-25)

- Exact `LowState`/`MotorState` fields, the serial+parallel motor array layout, joint **count**, and
  the per-joint temperature/error fields → drives the humanoid node count + color mapping.
- stdout formats of `b1_low_level_subscriber` / `battery_state_subscriber` / `odometer_example`
  (parse them, or decide to write the custom JSON node instead).
- That a 2nd SDK `ChannelFactory::Init` on the same iface coexists with the loco client (DDS
  subscribers normally do — confirm).
- Cost of sampling camera `ros2 topic hz` at the vitals cadence.

## Acceptance

- [ ] 2nd window opens at launch (suppressible), streams ~5 Hz, renders joints/battery/IMU/fall/cams/compute.
- [ ] Loading / empty / error / data states all handled — survives the robot going offline↔online.
- [ ] Zero loco commands issued; verified read-only end to end.
