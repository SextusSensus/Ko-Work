# K1 Finder UI (Tesla × SpaceX × Apple)

Near-black chrome, white type, cyan accent (`#32D4FF`), Tesla red STOP (`#E31937`).

## Local Map = domains

The **Local Map** tab is a **domain** system: each domain is a separate environment/area the robot has mapped (kitchen, warehouse bay, patio). Follow/capture runs **merge** occupancy into the **active** domain over time — they do not blindly replace the whole map.

### Persistence

| Path | Role |
|------|------|
| `desktop/localmap-data/domains.json` | Registry: `active` + domain list (name, updated, run_count, cell_count) |
| `desktop/localmap-data/domains/<id>/manifest.json` | Per-domain metadata |
| `desktop/localmap-data/domains/<id>/occupancy.json` | Accumulated cells + pose for the Three.js viewer |
| `desktop/localmap-viewer/` | Interactive 3D host (`index.html`, `viewer.js`, vendored `three.min.js`) |
| `desktop/localmap-viewer/feed.json` | Live poll mirror of the **active** domain occupancy |

### Operator UX

- **Domain chips** in the 3D viewer (and Domain combo on the WinForms toolbar): click to switch; scene reloads that domain’s occupancy.
- **+ New domain**: create when you bring the robot to a new environment (empty map ready to accumulate).
- **Import last run** / **Refresh**: pull robot dump (or sample) and **merge** cells into the active domain (hits accumulate by quantized grid key).
- Seed data ships for **Kitchen**, **Warehouse Bay A**, and **Outdoor Patio** so the preview is never empty.

### Hosting in K1 Finder

1. **WebView2** (preferred): Chromium embeds `localmap-viewer/index.html`.
2. **WebBrowser** fallback: IE-based; prefer **Open in browser** for full Three.js.
3. **Open in browser**: always available.

HTML `+ New domain` / chip switches sync to disk via a document-title bridge polled by the WinForms host.

### Drive safety

Local Map is observe-only. Tracker DRIVE / ARM / STOP / Deadman HB wiring is unchanged.

### Linux preview

Open `desktop/k1finder-ui-preview.html` — Discover + Tracker + Local Map with **domain chips** and a 3D canvas mock (no WinForms required).
