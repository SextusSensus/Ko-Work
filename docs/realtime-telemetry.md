# Realtime telemetry → Local Map

Practical first slice for feeding **odom / pose / occupancy / status** into the
Three.js Local Map viewer without jank.

## Research summary (what we evaluated)

| Approach | Fit | Why / why not for this repo |
|----------|-----|------------------------------|
| **Raw WebSocket JSON** | **Chosen** | Tiny bridge, no broker, works in WebView2 + browser, easy mock + `feed.json` tail. |
| MQTT over WebSocket (Mosquitto / EMQX / HiveMQ) | Later | Great for multi-client fleets; heavy for a single Local Map HUD. |
| Socket.IO | Skip | Extra framing; native WS is enough. |
| Server-Sent Events | Fallback only | One-way; fine for status, weaker for bidirectional later. |
| rosbridge + roslibjs | Later (on-robot) | Production ROS path; needs rosbridge on the K1 LAN. |
| Foxglove WS / SDK + MCAP | Parallel tool | Best for debugging multimodal logs; not our embedded map HUD. |
| Rerun | Already in stack | Capture / offline; not the desktop Local Map surface. |

**Booster K1 (public):** platform agents stream joint / motor / `vx,vy,wz` /
battery over WebSocket (~8 Hz). ROS2 conventions expose `/odometer_state`,
`/k1/joint_states`, `/cmd_vel`. This cloud VM cannot reach the robot LAN, so
the bridge **simulates Booster-like odom** and can **tail** `feed.json` when
Sky Connect writes occupancy dumps.

**Assets (CC0):** Poly Haven (floor / metal / plaster), ambientCG (concrete).
See `desktop/localmap-viewer/assets/ATTRIBUTION.md`.

**Three.js live updates:** buffer latest odom on the WS message handler; apply
pose + trail in `requestAnimationFrame`; throttle trail geometry rebuilds;
keep occupancy merges on a slower cadence.

## Chosen stack

1. **`desktop/telemetry-bridge/`** — Node (`ws`) on port **8742**
   - Static file server for `desktop/` (viewer + domains + preview)
   - `WS /ws/telemetry` broadcasts JSON messages
   - Optional REST: `/api/domains`, `/api/occupancy/:id`, `/api/status`
   - Modes: **mock odom** (default) and **feed tail** of `localmap-viewer/feed.json`
2. **Viewer** — reconnecting WebSocket client; last-pose HUD; `window.k1LocalMap`
   API extended (`connectTelemetry`, `getLastOdom`, `getTelemetryState`)

### Message shapes

```json
{ "type": "odom", "t": 1710000000.12, "x": 0.4, "y": 0.1, "z": 0, "yaw": 0.2, "vx": 0.3, "vy": 0, "wz": 0.05 }
{ "type": "occupancy", "domain_id": "exact-lab", "res_m": 0.08, "range_m": 4.5, "pose": { "x": 0.4, "y": 0.1, "yaw": 0.2 }, "cells": [ { "x": 1.2, "y": -1.6, "hits": 5 } ] }
{ "type": "status", "t": 1710000000.12, "mode": "mock", "connected": true, "hz": 15, "domain_id": "exact-lab", "battery": 87 }
```

## How to run

```bash
cd desktop/telemetry-bridge
npm install          # once
npm start            # http://127.0.0.1:8742  ·  ws://127.0.0.1:8742/ws/telemetry
```

Open:

- Live map: http://127.0.0.1:8742/localmap-viewer/index.html?live=1&domain=exact-lab
- UI preview: http://127.0.0.1:8742/k1finder-ui-preview.html?domain=exact-lab
- Status JSON: http://127.0.0.1:8742/api/status

Env / flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `PORT` | `8742` | HTTP + WS port |
| `--mock` | on | Simulate walking odom |
| `--no-mock` | | Only broadcast when feed/occupancy changes |
| `--hz 15` | 15 | Mock odom rate |
| `--feed <path>` | `../localmap-viewer/feed.json` | File to watch / merge |
| `--domain <id>` | `exact-lab` | Domain id for status/occupancy messages |

## Path to real robot

On the K1 LAN (not this cloud VM):

1. Prefer publishing planar pose from `/odometer_state` (or Aurora `/aurora_odom`
   when map-assist is armed) into the bridge via a small ROS→WS shim, **or**
2. Point rosbridge at the robot and add a thin adapter that maps
   `nav_msgs/Odometry` → `{type:"odom",...}` for the existing viewer client.

Foxglove / Rerun remain complementary for deep debugging; Local Map stays the
operator HUD inside Sky Connect.
