# K1 Finder UI (Tesla × SpaceX × Apple)

Near-black UI (`#000` / `#0A0A0A`), surfaces `#141414` / `#1C1C1E`, text `#F5F5F7`, muted `#8E8E93`, **one** electric cyan accent (`#32D4FF`), Tesla red STOP (`#E31937`). Bahnschrift / Segoe UI Variable for UI; Cascadia Mono for logs.

## Local Map = domains

Domains are **separate environments** (kitchen, warehouse bay, distribution hub). Each follow/capture run **merges** occupancy into the **active** domain — accumulate over time, do not blindly replace.

Parallel Local Map workers: see [`localmap-viewer/COORDINATION.md`](localmap-viewer/COORDINATION.md).

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
- Seed data: Kitchen, Warehouse Bay A, Distribution Hub.
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

## CGTrader asset strategy (premium hub / kitchen props)

CGTrader is the **discovery + purchase list** for premium distribution-hub and kitchen props. Paid models stay **link-only** until licensed. Free CGTrader listings are almost always **Royalty Free**, which forbids redistributing source files into this repo — do not commit those binaries.

| Path | Role |
|------|------|
| [`localmap-viewer/assets/CGTRADER.md`](localmap-viewer/assets/CGTRADER.md) | Curated shortlist (URLs, tags, prices, formats, status) |
| [`localmap-viewer/assets/catalog.json`](localmap-viewer/assets/catalog.json) | Machine-readable catalog |
| [`localmap-data/asset-ontology.json`](localmap-data/asset-ontology.json) | Label classes → `cgtrader_preferred` id + CC0/procedural runtime fallback |

Runtime keeps using Kenney / Poly Haven / procedural proxies. Ontology entries hold the preferred CGTrader URL so Todd can license later without remapping classes.

### Purchased CGTrader → local GLB drop-in

1. Buy / download the listing on [cgtrader.com](https://www.cgtrader.com) under your own account (accept their Royalty Free terms).
2. Export or pick a **GLB** (preferred) or glTF + bin + textures.
3. Place it at:

```text
desktop/localmap-viewer/assets/library/<catalog-id>/model.glb
```

Example: after licensing `cgt-hub-storage-kit-12`, put the rack bay GLB at  
`assets/library/cgt-hub-storage-kit-12/model.glb`.

4. In `catalog.json`, set that asset:

```json
"status": "local",
"library_path": "library/<catalog-id>/model.glb"
```

5. Keep `url` / provenance fields. Do **not** commit unlicensed paid files. Prefer CC0 for anything we must ship in git without a Todd license on file.

Statuses: `link_only` → `purchased_placeholder` (optional) → `local`. Free RF items stay `free_download` (local disk only, never git).
