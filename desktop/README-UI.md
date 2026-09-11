# K1 Finder UI (Tesla × SpaceX × Apple)

Near-black UI (`#000` / `#0A0A0A`), surfaces `#141414` / `#1C1C1E`, text `#F5F5F7`, muted `#8E8E93`, **one** electric cyan accent (`#32D4FF`), Tesla red STOP (`#E31937`). Bahnschrift / Segoe UI Variable for UI; Cascadia Mono for logs.

## Local Map = domains

Domains are **separate environments** (kitchen, warehouse bay, patio). Each follow/capture run **merges** occupancy into the **active** domain — accumulate over time, do not blindly replace.

### Persistence

| Path | Role |
|------|------|
| `desktop/localmap-data/domains.json` | Registry: `active` + list (name, updated, run_count, cell_count) |
| `desktop/localmap-data/domains/<id>/manifest.json` | Per-domain metadata |
| `desktop/localmap-data/domains/<id>/occupancy.json` | Accumulated cells + pose |
| `desktop/localmap-viewer/` | Three.js host (`index.html`, `viewer.js`, `three.min.js`, textures) |
| `desktop/localmap-viewer/feed.json` | Live poll mirror of the active domain (fallback when WS is down) |
| `desktop/telemetry-bridge/` | Realtime WebSocket bridge + static server (port **8742**) |

### Operator UX

- **Domain chips** in the 3D viewer (+ Domain combo on the WinForms toolbar): click to switch; scene reloads that domain’s map.
- **+ New domain**: create when bringing the robot to a new environment.
- **Import last run** / **Refresh**: pull robot dump (or sample) and **merge** into the active domain.
- Seed data: Kitchen, Warehouse Bay A, Outdoor Patio.
- **Live telemetry**: green HUD pill when WebSocket `/ws/telemetry` is connected; last pose + `vx/vy/wz` stay on screen across reconnects.

### Hosting

1. **WebView2** (preferred) embeds `localmap-viewer/index.html`.
2. **WebBrowser** fallback — prefer **Open in browser** for full Three.js.
3. HTML `+ New domain` / chip switches sync to disk via a document-title bridge polled by WinForms.
4. **Telemetry bridge** (dev / Linux preview): see below and [`docs/realtime-telemetry.md`](../docs/realtime-telemetry.md).

### Drive safety

Local Map is observe-only. Tracker DRIVE / ARM / STOP / Deadman HB wiring is unchanged.

## Realtime telemetry (recommended preview)

```bash
cd desktop/telemetry-bridge
npm install
npm start
# http://127.0.0.1:8742/localmap-viewer/index.html?live=1&domain=warehouse-bay-a
# ws://127.0.0.1:8742/ws/telemetry
# http://127.0.0.1:8742/api/status
```

Mock Booster-like odom walks the warehouse aisle at ~15 Hz. `feed.json` is still watched for occupancy dumps from K1 Finder.

## Static preview (no WebSocket)

```bash
cd desktop
python3 -m http.server 8765
# http://127.0.0.1:8765/k1finder-ui-preview.html?domain=warehouse-bay-a
# http://127.0.0.1:8765/localmap-viewer/index.html
```

Textures (Poly Haven + ambientCG CC0) live under `localmap-viewer/assets/` — see `ATTRIBUTION.md`.
