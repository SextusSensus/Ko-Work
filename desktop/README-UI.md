# K1 Finder UI (Tesla × SpaceX × Apple)

## Local Map 3D viewer (Windows)

The **Local Map** tab hosts an interactive Three.js scene of short-horizon occupancy (robot-local depth + odometry cells), not the old Aurora `.stcm` static render.

### Files

| Path | Role |
|------|------|
| `desktop/localmap-viewer/index.html` | Scene shell (black / cyan) |
| `desktop/localmap-viewer/viewer.js` | Orbit / pan / zoom + `window.k1LocalMap` API |
| `desktop/localmap-viewer/three.min.js` | Vendored Three.js (works offline / `file://`) |
| `desktop/localmap-viewer/sample.json` | Demo occupancy cells + pose |
| `desktop/localmap-viewer/feed.json` | Live feed the page polls (~1.5 s) |

### Hosting in K1 Finder

1. **WebView2** (preferred): if `Microsoft.Web.WebView2.WinForms.dll` is next to `K1Finder.ps1` or under `%USERPROFILE%\.nuget\packages\microsoft.web.webview2\`, the tab embeds Chromium and loads `index.html`.
2. **WebBrowser** fallback: IE-based control; Three.js may be limited — use **Open in browser**.
3. **Open in browser**: always available; opens `index.html` in the default browser (full orbit/pan/zoom).

### Controls

- **IP / Refresh** — try `scp` of `/tmp/k1_localmap.json` (or `~/localmap/latest.json`); on miss, reload sample
- **Load sample** — copy `sample.json` → `feed.json` and call `k1LocalMap.loadSample()`
- **Show robot pose / Follow pose / Clear / Reset view** — mapped to `window.k1LocalMap.*`
- **Import .stcm** — advanced legacy Aurora PNG path (hidden from primary UX)

### Drive safety

Local Map is observe-only. Tracker DRIVE / ARM / STOP / Deadman HB wiring is unchanged.

### Linux preview

Open `desktop/k1finder-ui-preview.html` for Discover + Tracker + Local Map mock screenshots without WinForms.
