# K1 Finder UI (Tesla × SpaceX × Apple)

Near-black UI (`#000` / `#0A0A0A`), surfaces `#141414` / `#1C1C1E`, text `#F5F5F7`, muted `#8E8E93`, **one** electric cyan accent (`#32D4FF`), Tesla red STOP (`#E31937`). Bahnschrift / Segoe UI Variable for UI; Cascadia Mono for logs.

## Local Map 3D viewer (Windows)

The **Local Map** tab hosts an interactive Three.js scene of short-horizon occupancy (robot-local depth + odometry), not the old Aurora `.stcm` static render as primary UX.

### Files

| Path | Role |
|------|------|
| `desktop/localmap-viewer/index.html` | Scene shell (black / cyan) |
| `desktop/localmap-viewer/viewer.js` | Orbit / pan / zoom + `window.k1LocalMap` API + domains |
| `desktop/localmap-viewer/three.min.js` | Vendored Three.js (offline / `file://`) |
| `desktop/localmap-viewer/sample.json` | Demo occupancy |
| `desktop/localmap-viewer/feed.json` | Live feed the page polls |
| `desktop/localmap-data/domains/` | Persisted domains (kitchen / warehouse / patio) |

### Hosting in K1 Finder

1. **WebView2** (preferred): embed Chromium if WinForms WebView2 DLL is available next to the app or under NuGet cache.
2. **WebBrowser** fallback: IE engine — prefer **Open in browser** for full Three.js.
3. **Open in browser**: launches `index.html` in the default browser.

### Controls

- **IP / Refresh** — pull occupancy dump when present; else sample
- **Load sample / Clear / Reset view / Show robot pose / Follow pose**
- **Domain** combo — switch persisted short-horizon maps
- **Import .stcm** — advanced legacy Aurora PNG path (not primary)

### Drive safety

Local Map is observe-only. Tracker DRIVE / ARM / STOP / Deadman HB wiring is unchanged.

## Preview on Linux

```bash
cd desktop
python3 -m http.server 8765
# http://127.0.0.1:8765/k1finder-ui-preview.html#discover
# http://127.0.0.1:8765/k1finder-ui-preview.html#tracker
# http://127.0.0.1:8765/k1finder-ui-preview.html#map
# http://127.0.0.1:8765/localmap-viewer/index.html
```
