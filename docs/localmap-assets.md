# Local Map 3D assets — catalog → label → place

Foundation for Todd’s future loop:

1. **Create domain** (viewer `+ New domain` / K1 Finder)
2. **Robot maps** over follow/capture runs → occupancy merges into `domains/<id>/occupancy.json`
3. **Rerun / labeling** (Batch-Label, Plan A YOLO classes, or live detect) emits detections `{class, x, y, yaw, w, h, confidence, run_id}`
4. **Assign 3D asset** from ontology → instance appears on Local Map and accumulates in `domains/<id>/instances.json`

This ships the catalog, ontology, viewer hooks, procedural fallbacks, and a mock demo path. Live robot labeling will call the **same** API.

## Paths

| Path | Role |
|------|------|
| `desktop/localmap-viewer/assets/catalog.json` | Machine-readable asset list (`downloaded` / `procedural` / `free_link`) |
| `desktop/localmap-viewer/assets/library/<category>/<asset-id>/` | glTF/GLB + textures |
| `desktop/localmap-viewer/assets/ATTRIBUTION.md` | License / source courtesy |
| `desktop/localmap-data/asset-ontology.json` | Label class → default asset + scale + domains |
| `desktop/localmap-data/domains/<id>/instances.json` | Placed instances (reruns append) |
| `desktop/localmap-viewer/asset-placer.js` | Runtime: resolve label, load mesh, place, persist |

## Viewer API (`window.k1LocalMap`)

```js
// From offline Batch-Label / live YOLO / mock:
k1LocalMap.registerDetections([
  { class: 'pallet_rack', x: -3.2, y: 1.0, yaw: 0, w: 1.2, h: 0.6, confidence: 0.9, run_id: 'run-42' }
]);

// Single place:
k1LocalMap.assignAsset('chair', { x: 0.5, y: -1.2, yaw: 0.4 });

// Inspect / host persistence bridge:
k1LocalMap.getAssetInstances();
k1LocalMap.exportAssetInstances(); // localStorage + optional k1LocalMapHostSaveInstances

// Demo seed for active domain (also ?demo_assets=1):
k1LocalMap.runAssetDemo();
```

Lower-level: `window.k1LocalMapAssets` (catalog/ontology accessors, procedural builders).

## Preview

```bash
cd desktop
python3 -m http.server 8765
# Kitchen with seed instances:
# http://127.0.0.1:8765/localmap-viewer/index.html?domain=kitchen
# Distribution hub + force mock detections:
# http://127.0.0.1:8765/localmap-viewer/index.html?domain=distribution-hub&demo_assets=1
```

## How label → asset works

1. Detection `class` (or COCO id) looks up `asset-ontology.json` `label_classes` (id, aliases, `coco_id`).
2. `default_asset` selects a row in `catalog.json`.
3. If `status: downloaded`, GLTFLoader fetches `path`; on failure uses `fallback_procedural`.
4. If `status: procedural`, Three.js recipe builds a dimensionally plausible proxy.
5. Instance is recorded and written to localStorage; host apps should mirror into `instances.json`.

Plan A / COCO classes in `robot/common.py` (`CLASS_BRAKE_*`) are first-class ontology entries (`person`, `chair`, `couch`, `refrigerator`, …). Warehouse labels (`pallet_rack`, `conveyor`, `dock_door`, …) are custom classes for hub/bay domains.

## Add a new label class / asset

1. Drop CC0 glTF under `assets/library/<category>/<id>/` (or add a procedural recipe in `asset-placer.js`).
2. Append an entry to `catalog.json` with `license`, `source_url`, `status`.
3. Append a `label_classes` row in `asset-ontology.json` pointing `default_asset` at that id; set `scale_m` and `domains`.
4. Optionally seed `domains/<id>/instances.json` or call `registerDetections` once.

## Domains covered

- **kitchen** — stove, cabinets, table/chairs, fridge (procedural), microwave, trash, doorway, person
- **warehouse-bay-a** — racks, pallets, boxes, cones, bollards, dock
- **distribution-hub** — multi-aisle racks, conveyors, dock doors, totes, forklift proxy, barriers (replaces outdoor-patio)
- **generic / future** — sofa, plant, pillar, wall, stairs, elevator, signage

## Licenses

Prefer **CC0** (Poly Haven, ambientCG, Kenney, Quaternius). See `assets/ATTRIBUTION.md`. Skipped non-CC0 / ambiguous Sketchfab Free3D hits (full fridge mesh, branded forklift) — procedural proxies used instead.
