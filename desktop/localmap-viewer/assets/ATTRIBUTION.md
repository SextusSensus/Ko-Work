# Map viewer assets (CC0)

Attribution is courtesy (CC0 does not require it). Every redistributable file used by the Local Map viewer is listed below.

## Root (`assets/`)

| File | Source | Author | License |
|------|--------|--------|---------|
| `floor_diff.jpg` | [Poly Haven — Concrete Floor Painted](https://polyhaven.com/a/concrete_floor_painted) (1K Diffuse) | Poly Haven contributors | CC0 |
| `metal_diff.jpg` | [Poly Haven — Metal Plate](https://polyhaven.com/a/metal_plate) (1K Diffuse) | Poly Haven contributors | CC0 |
| `plaster_diff.jpg` | [Poly Haven — Painted Plaster Wall](https://polyhaven.com/a/painted_plaster_wall) (1K Diffuse) | Poly Haven contributors | CC0 |
| `concrete_color.jpg` | [ambientCG — Concrete034](https://ambientcg.com/view?id=Concrete034) (1K Color) | ambientCG | CC0 |
| `concrete_rough.jpg` | [ambientCG — Concrete034](https://ambientcg.com/view?id=Concrete034) (1K Roughness) | ambientCG | CC0 |

## `distribution-hub/` textures

| File | Source URL | Author | License |
|------|------------|--------|---------|
| `floor_warehouse_diff.jpg` | https://polyhaven.com/a/concrete_floor_painted | Poly Haven | CC0 |
| `floor_anti_slip_diff.jpg` | https://polyhaven.com/a/anti_slip_concrete | Poly Haven | CC0 |
| `metal_plate_diff.jpg` | https://polyhaven.com/a/metal_plate | Poly Haven | CC0 |
| `painted_concrete_diff.jpg` | https://polyhaven.com/a/painted_concrete | Poly Haven | CC0 |
| `corrugated_diff.jpg` | https://polyhaven.com/a/corrugated_iron | Poly Haven | CC0 |
| `corrugated_02_diff.jpg` | https://polyhaven.com/a/corrugated_iron_02 | Poly Haven | CC0 |
| `wood_plank_diff.jpg` | https://polyhaven.com/a/black_painted_planks | Poly Haven | CC0 |
| `wood_pallet_diff.jpg` | https://polyhaven.com/a/brown_planks_03 | Poly Haven | CC0 |
| `shutter_diff.jpg` | https://polyhaven.com/a/painted_metal_shutter | Poly Haven | CC0 |
| `cardboard_diff.jpg` | https://ambientcg.com/view?id=Paper005 (`Paper005_1K-JPG_Color.jpg`) | ambientCG | CC0 |

## `distribution-hub/kenney/` (Kenney Factory Kit 3.0)

Pack: https://kenney.nl/assets/factory-kit — License: **CC0 1.0** (`kenney/LICENSE.txt`). Credit “Kenney.nl” appreciated, not required.

| File | Role |
|------|------|
| `box-large.glb` | Cardboard box prop |
| `box-long.glb` | Long carton |
| `box-small.glb` | Small carton |
| `box-wide.glb` | Wide carton |
| `conveyor.glb` | Conveyor segment |
| `conveyor-long.glb` | Long conveyor |
| `conveyor-long-stripe.glb` | Striped conveyor |
| `conveyor-corner.glb` | Conveyor corner |
| `cone.glb` | Safety cone |
| `door-wide-closed.glb` | Wide door closed |
| `door-wide-open.glb` | Wide door open |
| `structure-tall.glb` / `structure-high.glb` / `structure-wall.glb` | Structural frames |
| `structure-yellow-tall.glb` / `structure-yellow-high.glb` | Yellow safety structure |
| `colormap.png` | Shared Kenney color atlas |

## `distribution-hub/polyhaven/` (glTF props, 1K)

All **CC0** from https://polyhaven.com/ — downloaded via Poly Haven files API (`gltf/1k` + includes).

| Folder | Source | Role |
|--------|--------|------|
| `cardboard_box_01/` | https://polyhaven.com/a/cardboard_box_01 | Photogrammetry cardboard box |
| `wooden_crate_01/` | https://polyhaven.com/a/wooden_crate_01 | Wooden crate |
| `plastic_crate_01/` | https://polyhaven.com/a/plastic_crate_01 | Plastic crate / tote |
| `industrial_pastic_container/` | https://polyhaven.com/a/industrial_pastic_container | Industrial plastic container (asset id spelling) |
| `worn_metal_rack/` | https://polyhaven.com/a/worn_metal_rack | Metal rack / shelving |

Each folder contains `.gltf`, `.bin`, and `textures/*_diff|_nor_gl|_arm_1k.jpg`.

## Procedural meshes (no external mesh license)

Built in `viewer.js` for Distribution Hub density: pallet racks, aisle tape, bollards, dock roll-up doors, forklift proxies, pallet jacks, conveyor frames, shrink-wrapped pallet loads, bay signage, skylights, industrial pendants. Materials sample the CC0 textures above.

## Robot proxy

Booster K1 dimension proxy (~95×40×18 cm, 19.5 kg, 22 DoF) — not an official Booster mesh.
Official URDF: https://github.com/BoosterRobotics/booster_assets (`robots/K1/`).

## Not obtained (license / paywall / unavailable)

| Asset | Reason |
|-------|--------|
| Kenney Conveyor Kit (standalone pack) | Direct zip URL 404 during ingest; Factory Kit already includes conveyor GLBs |
| Quaternius forklift / warehouse packs | No clear CC0 forklift mesh located without paywall; used procedural forklift proxy |
| Sketchfab “realistic warehouse” GLTFs | Many CC-BY with non-commercial / no-derivatives clauses — skipped |
| Free3D high-detail forklift / dock door | Paywalled or license-unclear — skipped |
| ambientCG API host `api.ambientcg.com` | TLS name mismatch in this environment; used `ambientcg.com` web get instead |


## `library/warehouse/` (shared domain library)

Poly Haven glTF 1K props — **CC0** (https://polyhaven.com/):

| Folder | Source |
|--------|--------|
| `cardboard_box_01/` | https://polyhaven.com/a/cardboard_box_01 |
| `wooden_crate_01/` | https://polyhaven.com/a/wooden_crate_01 |
| `plastic_crate_01/` | https://polyhaven.com/a/plastic_crate_01 |
| `industrial_pastic_container/` | https://polyhaven.com/a/industrial_pastic_container |
| `worn_metal_rack/` | https://polyhaven.com/a/worn_metal_rack |
| `concrete_road_barrier/` | https://polyhaven.com/a/concrete_road_barrier |
| `hand_truck/` | https://polyhaven.com/a/hand_truck |
| `rollershutter_door/` | https://polyhaven.com/a/rollershutter_door |
| `steel_frame_shelves_01/` | https://polyhaven.com/a/steel_frame_shelves_01 |
| `Barrel_01/` | https://polyhaven.com/a/Barrel_01 |
| `kenney/` | Kenney Factory Kit subset — CC0 (see `kenney/LICENSE.txt`) |
| `wooden_pallet_cc0/` | Lightweight procedural/CC0 pallet proxy mesh (no third-party textures) |

## `library/cross-domain/`

| Path | Source | License |
|------|--------|---------|
| `traffic/*.glb` | MilkAndBanana — Traffic Road Assets | CC0 (`traffic/LICENSE.txt`) |
| `caged_hanging_light/` | Poly Haven | CC0 |
| `WetFloorSign_01/` | Poly Haven | CC0 |
