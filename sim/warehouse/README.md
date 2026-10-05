# K1 warehouse simulator

A MuJoCo + gymnasium simulator for training and testing Booster K1 warehouse behaviors. It uses
the official K1 URDF (`desktop/localmap-viewer/assets/library/robot/k1/`). The tool has four parts:

- a randomized warehouse environment,
- a browser control UI,
- importers and exporters that connect it to the robot's own tooling,
- PPO training.

```
pip install -r requirements-sim.txt
python sim/warehouse/web.py                  # control UI on http://127.0.0.1:8765
python sim/warehouse/train.py --task follow --steps 2000000
```

Every module has a self-test: `python sim/warehouse/<k1_warehouse|sim_io|record|web>.py selftest`.

## Web control UI

`web.py` serves one page with live video. It runs fully offline: no CDN and no web fonts.

| Area | Controls |
|---|---|
| Episode | Task (pick/follow), seed, Reset, Next, Replay, Pause, Step (one 0.1 s tick), speed 0.5×–8× |
| Driver | Autopilot (the scripted baseline), Manual teleop (D-pad or WASD/arrow keys), Policy (needs `--model runs/x/model.zip`) |
| Camera | Chase, Top, Head (robot point of view) |
| Inject fault | Link stall 0.5 s (watchdog zero tier), link stall 1.5 s (prepare tier), swap target ID (follow) |
| Import | Upload a scenario JSON, or load a Local Map domain as the map |
| Export | Scenario JSON, scene MJCF zip, episode JSONL, Rerun `.rrd`, LeRobot zip |
| HUD | Safety counters, link/loco flags, applied command, depth-scan fan, outcome banner |

Manual driving goes through `env.step` like any policy, so the bridge clamps, link latency and
watchdog all apply. A server-side deadman zeroes the action 300 ms after the last drive message,
which covers a closed tab or a dropped connection. Manual driving never runs faster than real time.

The server binds 127.0.0.1. `--host 0.0.0.0` exposes it to the LAN. That is an explicit opt-in:
anyone on the network can then drive the sim. They cannot drive the robot.

## Import and export

| Direction | Format | Where |
|---|---|---|
| ↔ | Scenario JSON (`k1sim.scenario` v1). The exact world and episode; replays bit for bit | UI, `sim_io.py`, `env.reset(options={"scenario": ...})` |
| ← | Local Map domain. A robot-mapped site becomes a sim world | UI, `sim_io.py import-localmap`, `train.py --localmap` |
| → | Local Map domain. A sim world opens in the Local Map viewer | `sim_io.py export-localmap` |
| → | MJCF bundle (`scene.xml` + meshes). Opens in MuJoCo `simulate`, Isaac Lab, etc. | UI, `sim_io.py export-mjcf` |
| → | Episode JSONL. Scenario header plus one line per step; header + actions replay | UI, `record.py dataset --format jsonl` |
| → | Rerun `.rrd`. Uses the robot's entity paths and timelines | UI, `record.py dataset --format rrd` |
| → | LeRobot RAW. TRAIN_CONTRACT `observation.state[7]` / `action[2]` + head-camera mp4 | UI, `record.py dataset --format lerobot` |

```
python sim/warehouse/sim_io.py list-domains
python sim/warehouse/sim_io.py import-localmap <domain> --out site.json --task follow
python sim/warehouse/sim_io.py export-mjcf site.json out/mjcf
python sim/warehouse/record.py dataset --task follow --episodes 50 --format lerobot --out data/
python sim/warehouse/train.py --task follow --world site.json     # train on one fixed map
```

**Sim data is always labelled as sim.** Every export is marked SIMULATED:

- LeRobot exports use `robot_type: booster_k1_sim` and include `meta/sim_provenance.json`.
- `.rrd` exports carry a static `/sim/provenance` entity.

The repo's shared `.rrd` reader (`eval/rrd_to_lerobot.read_rrd` / `read_rrd_frames`) refuses a sim
recording unless the caller passes `allow_sim=True`. This keeps sim data out of camera
calibration, Local Map ingest, recon and training datasets unless someone asks for it. To convert
one on purpose, run `rrd_to_lerobot.py --allow-sim`; it labels the output `booster_k1_sim`.

`observation.state` in sim exports is synthetic. The sim has no YOLO or ReID, and
`record.contract_state()` documents the mapping it uses. Use these exports for pipeline plumbing
and pretraining only. They are not measured robot signals.

## What is simulated

The robot takes commands the way the real one does: a 10 Hz `(vx, vyaw)` stream into Booster's
onboard loco controller. The stream goes through the same hard clamps (`vx` -0.10..0.30 m/s,
`vyaw` ±0.40 rad/s) and the same staleness watchdog (zero velocity at 400 ms, prepare at
1000 ms) as `robot/loco_follow_bridge.cpp`. The clamps are duplicated here on purpose.

The gait is **not** simulated, and the real stack does not control it either. The K1 is a rigid
stand-pose body on a planar x/y/yaw base. Its loco model has:

- velocity lag (0.15–0.40 s)
- an acceleration limit
- a gait-start delay from standstill (0.3–0.7 s)
- gait sway while walking

The body still gets blocked by racks, shoves loose boxes, and slows under a payload. Whole-body
gait RL needs a different sim (Booster's `booster_gym`).

A global planner sees only the static map: it runs Dijkstra over a 0.2 m occupancy grid and
picks the farthest point it can see straight to. Boxes, pallets and people are left to the
policy. The same planner runs on generated and imported maps.

| Task | Goal | Success |
|---|---|---|
| `pick` | Visit N pick stations, then go back to the dock | All goals reached, no human contact, no fall |
| `follow` | Follow a picker (orange) | ≥60% of time at 1.0–2.0 m, no human contact, no fall |

### Failure modes taken from the robot's incident history

| Sim failure | Real-robot source |
|---|---|
| **Fall** on foot-level contact at speed, on a velocity lunge, or on fast yaw while walking | `docs/HARDENING_2026-07.md` |
| **Forklift contact** is always a fall | Warehouse hazard; forklifts do not yield |
| **kPrepare lockout**: 1.5–3 s before walking again after the 1000 ms stale tier | `STALE_PREP_MS` in `robot/loco_follow_bridge.cpp`; `_graceful_stop` in `robot/follow_person_k1.py` |
| **Reacquire**: the follow target is only known through the detector; behind a rack it is gone | `S_REACQUIRE` in `robot/follow_person_k1.py` |
| **Identity swap**: a worker crossing within 0.6 m of the target can steal the lock | `docs/CROWD_2FA_LOCK.md` |
| **Depth range glitch**: 2% of person detections read 1.5–3× long | `docs/HARDENING_2026-07.md` |
| **Blind band**: pallets 0.15 m tall reach into aisles; only the 0.10 m scan sees them | `docs/PLAN_A_CLASS_AWARE_BRAKE.md` |

Every reset re-randomizes these:

- **Layout:** generated maps only.
- **Actors:** workers (about half do not yield) and loose boxes.
- **Forklift:** on half the shifts, in generated maps.
- **Robot:** payload, actuator force and gain, acceleration limit, loco lag, gait-start delay, sway, trip sensitivity.
- **Command link:** latency, jitter, drops, 0.3–1.5 s burst stalls.
- **Sensing:** a 2×16-ray depth scan whose long-reading error grows with range (c ≈ 0.1 1/m), with dropouts that read as free space. A person detector with FOV, occlusion and misses. Noisy odometry.

## Scripted baseline (fixed seeds 1000–1029)

| Task | Success | 95% CI |
|---|---|---|
| pick (2 stations) | 0.80 | 0.63–0.91 |
| follow | 0.57 | 0.39–0.73 |

A trained policy has to beat these on the same seeds (`train.py` reports both). The next step
after that is shadow mode on the robot, not driving.

## Not included (yet)

- Arm pick-and-place: the arms are fixed in the stand pose.
- Camera-image policies: the policy sees a ray scan. The head camera is rendered for exports and the UI only.
- Odometry drift.
- Head pan/tilt.
- Import has only been tested on a synthetic Local Map fixture. The repo's one domain (`exact-lab`) is empty.
