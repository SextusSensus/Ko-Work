# Local Map 3D assets — catalog → label → place

Foundation for Todd’s future loop:

1. **Create domain** (viewer `+ New domain` / Sky Connect)
2. **Robot maps** over follow/capture runs → occupancy merges into `domains/<id>/occupancy.json`
3. **Rerun / labeling** (Batch-Label, Plan A YOLO classes, or live detect) emits detections `{class, x, y, yaw, w, h, confidence, run_id}`
4. **Assign 3D asset** from ontology → instance appears on Local Map and accumulates in `domains/<id>/instances.json`

This ships the catalog, ontology, viewer hooks and procedural fallbacks. Live robot labeling will call the **same** API.

## Paths

| Path | Role |
|------|------|
| `desktop/localmap-viewer/assets/catalog.json` | Machine-readable **CC0** asset list (`downloaded` / `procedural` / `free_link`) |
| `desktop/localmap-viewer/assets/FREE-ASSETS.md` | Free-only policy + domain coverage |
| `desktop/localmap-viewer/assets/MESHY.md` | Meshy.ai license verdict + optional local drop-in (not preferred in-repo) |
| `desktop/localmap-viewer/assets/library/<category>/<asset-id>/` | glTF/GLB + textures |
| `desktop/localmap-viewer/assets/ATTRIBUTION.md` | License / source courtesy |
| `desktop/localmap-data/asset-ontology.json` | Label class → default asset + scale + domains |
| `desktop/localmap-data/global-assets.json` | Cross-domain `scope:global` catalog + `label_aliases` for autofill |
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

// Building envelope walls (default ON):
k1LocalMap.setExteriorVisible(false); // hide shell to inspect from outside
```

Lower-level: `window.k1LocalMapAssets` (catalog/ontology accessors, procedural builders).

## Preview

```bash
cd desktop
python3 -m http.server 8765
# Exact Lab stays empty until Import -Fixture / place:
# http://127.0.0.1:8765/localmap-viewer/index.html?domain=exact-lab
```

## How label → asset works

1. Detection `class` (or COCO id) looks up `asset-ontology.json` `label_classes` (id, aliases, `coco_id`).
2. `default_asset` selects a row in `catalog.json`.
3. If `status: downloaded`, GLTFLoader fetches `path`; on failure uses `fallback_procedural`.
4. If `status: procedural`, Three.js recipe builds a dimensionally plausible proxy.
5. Instance is recorded and written to localStorage; host apps should mirror into `instances.json`.

Plan A / COCO classes in `robot/common.py` (`CLASS_BRAKE_*`) are first-class ontology entries (`person`, `chair`, `couch`, `refrigerator`, …). Warehouse labels (`pallet_rack`, `conveyor`, `dock_door`, …) are custom (non-COCO) classes.

## Exact poses from Rerun

For mathematically exact placement from `.rrd` / labeling (homogeneous `T_map_cam`, depth back-project, fail-closed ground raycast), see **`docs/localmap-rerun-integration.md`**. CLI: `eval/localmap_rerun_ingest.py`, `eval/localmap_asset_placer.py`.

## Add a new label class / asset

1. Drop CC0 glTF under `assets/library/<category>/<id>/` (or add a procedural recipe in `asset-placer.js`).
2. Append an entry to `catalog.json` with `license`, `source_url`, `status`.
3. Append a `label_classes` row in `asset-ontology.json` pointing `default_asset` at that id; set `scale_m` and `domains`.
4. Optionally seed `domains/<id>/instances.json` or call `registerDetections` once.

## Domains

The repo ships one domain, **`exact-lab`**. It stays empty until `Import-RerunToDomain.ps1 -Domain exact-lab -Fixture` (or a real `.rrd`) places instances. Create a domain per real environment; the label→asset catalog is global, so any domain can place any catalog asset.

## Licenses

Prefer **CC0** (Poly Haven, ambientCG, Kenney, Quaternius). See `assets/ATTRIBUTION.md` and `assets/FREE-ASSETS.md`. Skipped non-CC0 / ambiguous marketplace hits (full fridge mesh, branded forklift) — procedural proxies used instead. **No paid marketplace URLs** (including CGTrader) are kept as a purchase wishlist.

**Meshy.ai:** Free outputs are CC BY 4.0; Community publish is CC0; paid = private ownership. Meshy 6/7 downloads are paywalled on Free, so Meshy is **optional local drop-in only** (`library/meshy-local/`, binaries gitignored) — not a default in-repo source. Do not commit paid/watermarked Meshy assets. Details: [`desktop/localmap-viewer/assets/MESHY.md`](../desktop/localmap-viewer/assets/MESHY.md).

See also: [`localmap-autofill.md`](./localmap-autofill.md) for the collect → label → ontology → place loop.
