# Local Map autofill — label → global asset → domain instance

How future **auto-tune / data-collection** runs place 3D props on a new Local Map domain from real-world detections.

## Flow

```
collect run (follow / capture)
        │
        ▼
label detections  { class|label, x, y, yaw, w?, h?, confidence?, run_id? }
        │
        ▼
ontology / global-assets  label_aliases lookup
        │
        ▼
catalog asset (scope: "global" preferred)  +  scale_m (metres)
        │
        ▼
k1LocalMap.registerDetections / assignAsset
        │
        ▼
instance at robot pose  →  domains/<id>/instances.json
```

Building a **new domain from scratch**: create the domain → merge occupancy from runs → call `registerDetections` with labeled poses → autofill places the canonical mesh where the robot saw that object.

## Lookup order

1. Detection `class` / `label` / COCO id (`coco:56`) → `desktop/localmap-data/asset-ontology.json` `label_classes` (id + aliases).
2. Same keys also live in `desktop/localmap-data/global-assets.json` `label_aliases` (cross-domain; loaded by `asset-placer.js`).
3. `default_asset` selects a row in `desktop/localmap-viewer/assets/catalog.json`.
4. If the active domain has `domain_assets[domainId]`, that asset wins (e.g. office chairs vs kitchen dining chairs).
5. `status: downloaded` loads glTF at real-world `scale_m`; failure → `fallback_procedural`. `status: procedural` builds a metre-scale stand-in.

## API

```js
// Offline Batch-Label / live YOLO / mock demo:
k1LocalMap.registerDetections([
  { class: 'desk', x: -3.0, y: -1.2, yaw: 0, w: 1.4, h: 0.7, confidence: 0.91, run_id: 'run-7' },
  { class: 'chair', x: -3.0, y: -0.45, yaw: 3.14, confidence: 0.86, run_id: 'run-7' },
  { class: 'elevator', x: -5.4, y: 4.6, yaw: 0, confidence: 0.94, run_id: 'run-7' }
]);

k1LocalMap.assignAsset('cubicle', { x: 2.0, y: 0, yaw: 0, w: 1.6, h: 1.6 });

k1LocalMap.getAssetInstances();
k1LocalMap.getGlobalAssets(); // scope:global catalog slice + label_aliases
k1LocalMap.runAssetDemo();    // or ?demo_assets=1
```

Coordinates are **map metres** (FLU): `x`/`y` on the floor plane; viewer maps `y` → Three.js `z`. Scales are absolute metres — **not** relative to K1.

## Office demo

```bash
cd desktop
python3 -m http.server 8765
# http://127.0.0.1:8765/localmap-viewer/index.html?domain=office&demo_assets=1
```

Seeded domain: `desktop/localmap-data/domains/office/` (`instances.json` + occupancy). Primary domain id: **`office`**.

## Files

| Path | Role |
|------|------|
| `desktop/localmap-data/global-assets.json` | `scope: "global"` asset list + `label_aliases` for autofill |
| `desktop/localmap-data/asset-ontology.json` | Canonical label classes, aliases, `domain_assets`, `scale_m` |
| `desktop/localmap-viewer/assets/catalog.json` | CC0 / procedural / free_link inventory (`scope` field) |
| `desktop/localmap-viewer/asset-placer.js` | Runtime resolve + place + demo seeds |
| `docs/localmap-assets.md` | Catalog policy + domain coverage |

## Free-only policy

Runtime uses **CC0 / procedural / free_link** only (Poly Haven, ambientCG, Kenney, Quaternius, authored stand-ins). No paid CGTrader paths. See `desktop/localmap-viewer/assets/FREE-ASSETS.md`.
