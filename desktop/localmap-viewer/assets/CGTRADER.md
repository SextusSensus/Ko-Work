# CGTrader shortlist — Local Map (hub / kitchen / indoor / robot)

**Policy:** CGTrader is the **premium discovery + purchase wishlist** for hub/kitchen props. It does **not** replace the CC0 runtime catalog (`catalog.json` / `FREE-ASSETS.md`).
Paid models stay **`link_only`** until Todd licenses them. Free CGTrader listings are **Royalty Free** — redistributing source files into this repo is forbidden; status `free_download` means Todd may download locally, **never commit**.
Runtime keeps Poly Haven / ambientCG / Kenney / procedural fallbacks. Ontology `cgtrader_preferred` points at wishlist ids while `default_asset` stays CC0/procedural.

Statuses: `link_only` | `free_download` | `purchased_placeholder` | `local`

Machine-readable: [`cgtrader-catalog.json`](./cgtrader-catalog.json) · wired in [`../../localmap-data/asset-ontology.json`](../../localmap-data/asset-ontology.json).

### Drop-in after purchase
1. Buy/download on cgtrader.com (your account, accept RF terms).
2. Put GLB at `assets/library/<catalog-id>/model.glb`.
3. In `cgtrader-catalog.json`: `"status":"local"`, `"library_path":"library/<catalog-id>/model.glb"`.
4. Keep `url`. Do not commit unlicensed paid/RF binaries.

---

## Distribution hub / warehouse

| id | Title | Labels | ~Price | Formats | Status |
|----|-------|--------|--------|---------|--------|
| [cgt-hub-dock-bundle-68](https://www.cgtrader.com/3d-model-packs/warehouse-logistics-and-loading-dock-bundle-68-assets) | Warehouse Logistics and Loading Dock Bundle — 68 Assets | `conveyor`, `dock_door`, `bollard`, `safety_barrier` | **$10** | BLEND, FBX, OBJ, GLB, glTF | `link_only` |
| [cgt-hub-dock-kit-32](https://www.cgtrader.com/3d-models/industrial/industrial-machine/modular-loading-dock-equipment-kit-32-assets) | Modular Loading Dock Equipment Kit — 32 Assets | `dock_door`, `bollard`, `safety_barrier` | **$6** | BLEND, FBX, OBJ, GLB | `link_only` |
| [cgt-hub-conveyor-kit-36](https://www.cgtrader.com/3d-models/industrial/industrial-machine/modular-industrial-conveyor-and-roller-system-kit-36-assets) | Modular Industrial Conveyor and Roller System Kit — 36 Assets | `conveyor` | **$4.99** | BLEND, FBX, OBJ, GLB | `link_only` |
| [cgt-hub-rack-system](https://www.cgtrader.com/3d-models/industrial/industrial-machine/industrial-warehouse-pallet-racking-system) | Industrial Warehouse Pallet Racking System | `pallet_rack`, `pallet`, `cardboard_box` | **$59** | FBX, OBJ, GLB, STL, USDZ | `link_only` |
| [cgt-hub-rack-detailed](https://www.cgtrader.com/3d-models/interior/other/detailed-full-warehouse-rack-3678dbe5-35a1-4ea1-9eab-ba96779138d1) | Detailed Full Warehouse Rack | `pallet_rack` | paid (RF) | glTF, FBX, OBJ, BLEND | `link_only` |
| [cgt-hub-storage-kit-12](https://www.cgtrader.com/3d-models/industrial/other/industrial-storage-and-racking-kit-12-pbr-warehouse-assets) | Industrial Storage and Racking Kit — 12 PBR Warehouse Assets | `pallet_rack`, `pallet`, `tote_bin`, `cardboard_box`, `barrel` | **$18** | FBX, OBJ, GLB, glTF | `link_only` |
| [cgt-hub-rack-modular-game](https://www.cgtrader.com/3d-models/industrial/industrial-part/modular-industrial-storage-rack-pack-game-ready) | Modular Industrial Storage Rack Pack (Game-Ready) | `pallet_rack` | paid (RF) | FBX, UE, Unity | `link_only` |
| [cgt-hub-asrs-59](https://www.cgtrader.com/3d-models/industrial/industrial-machine/modular-asrs-warehouse-automation-collection) | ASRS Warehouse Automation Kit | `conveyor`, `pallet_rack` | paid (RF) | BLEND, FBX, OBJ, GLB, USD | `link_only` |
| [cgt-hub-forklift-cargo](https://www.cgtrader.com/3d-models/industrial/other/industrial-forklift-warehouse-cargo-collection) | Industrial Forklift & Warehouse Cargo Collection | `forklift`, `pallet`, `cardboard_box` | **$3** | MAX, FBX, OBJ | `link_only` |
| [cgt-hub-forklift-hd](https://www.cgtrader.com/3d-models/vehicle/industrial-vehicle/an-industrial-forklift-wooden-pallet-stacked-with-high-detail) | Industrial Forklift + Wooden Pallet (High Detail) | `forklift`, `pallet` | paid (RF) | GLB, FBX | `link_only` |
| [cgt-hub-logistics-scene](https://www.cgtrader.com/3d-models/industrial/other/logistics-warehouse-truck-forklift-worker-cargo-scene-3d-pack) | Logistics Warehouse Truck / Forklift / Worker / Cargo Pack | `forklift`, `cardboard_box`, `signage` | paid (RF) | MAX, FBX, OBJ, BLEND | `link_only` |
| [cgt-hub-pallet-jack-free](https://www.cgtrader.com/free-3d-models/industrial/industrial-machine/stylized-industrial-electric-pallet-jack) | Stylized Industrial Electric Pallet Jack | `pallet_jack` | **Free** | BLEND, FBX, GLB, OBJ | `free_download` |
| [cgt-hub-rack-free-game](https://www.cgtrader.com/free-3d-models/interior/hall/warehouse-storage-rack-game-ready-props) | Warehouse Storage Rack — Game Ready Props | `pallet_rack` | **Free** | MAX, FBX, OBJ | `free_download` |
| [cgt-hub-shelving-free](https://www.cgtrader.com/free-3d-models/furniture/kitchen-cabinet/warehouse-shelving-unit-organized-strength-for-industrial) | Warehouse Shelving Unit (Industrial) | `pallet_rack` | **Free** | FBX, OBJ, STL, BLEND, glTF | `free_download` |
| [cgt-hub-boxes-stacked-free](https://www.cgtrader.com/free-3d-models/industrial/other/stacked-cardboard-shipping-boxes-free) | Stacked Cardboard Shipping Boxes | `cardboard_box` | **Free** | BLEND, FBX, OBJ, GLB, USDZ | `free_download` |
| [cgt-hub-box-tall-free](https://www.cgtrader.com/free-3d-models/furniture/other/tall-cardboard-parcel-box) | Tall Cardboard Parcel Box | `cardboard_box` | **Free** | BLEND, FBX, GLB, OBJ, STL | `free_download` |
| [cgt-hub-box-display-free](https://www.cgtrader.com/free-3d-models/industrial/other/cardboard-display-box-packaging-mockup) | Cardboard Display Box Packaging Mockup | `cardboard_box` | **Free** | BLEND, FBX, OBJ, glTF | `free_download` |
| [cgt-hub-boxes-free](https://www.cgtrader.com/free-3d-models/industrial/other/cardboard-boxes-dd2cde11-da83-421f-8fdd-9664e2796390) | Cardboard Boxes | `cardboard_box` | **Free** | FBX, OBJ, BLEND, STL | `free_download` |
| [cgt-hub-tote-green](https://www.cgtrader.com/3d-models/industrial/tool/standard-green-shallow-pallet-storage-plastic-crate-box-tray-bin) | Standard Green Shallow Pallet Storage Plastic Crate / Tote | `tote_bin` | paid (RF) | FBX, OBJ, GLB, STEP | `link_only` |
| [cgt-hub-stretch-wrap-machine](https://www.cgtrader.com/3d-models/industrial/tool/industrial-automatic-pallet-stretch-wrapping-machine) | Industrial Automatic Pallet Stretch Wrapping Machine | `pallet` | paid (RF) | FBX, OBJ, GLB, STL, USDZ | `link_only` |
| [cgt-hub-wrap-turntable](https://www.cgtrader.com/3d-models/industrial/industrial-machine/automatic-pallet-wrapping-machine-with-turntable) | Automatic Pallet Wrapping Machine with Turntable | `pallet` | paid (RF) | CAD | `link_only` |
| [cgt-hub-safety-barrier](https://www.cgtrader.com/3d-models/industrial/other/safety-barrier-pbr-game-ready) | Safety Barrier — PBR Game Ready | `safety_barrier`, `signage` | paid (RF) | FBX, PBR | `link_only` |
| [cgt-hub-roll-door-clean](https://www.cgtrader.com/3d-models/architectural/door/industrial-roll-up-door-warehouse-gate-clean) | Industrial Roll Up Door Warehouse Gate (Clean) | `dock_door`, `doorway` | paid (RF) | PBR | `link_only` |
| [cgt-hub-roll-door-lp](https://www.cgtrader.com/3d-models/architectural/door/roll-up-door-and-damaged-door-warehouse-porta-de-enrolar) | Roll-Up Door and Damaged Door (Warehouse) | `dock_door`, `doorway` | paid (RF) | BLEND, FBX, OBJ, glTF, STL | `link_only` |
| [cgt-hub-prop-pack-ue](https://www.cgtrader.com/3d-models/industrial/industrial-part/warehouse-and-office-industrial-prop-pack) | Warehouse and Office Industrial Prop Pack (UE5) | `forklift`, `pallet_rack`, `pallet`, `cardboard_box`, `oven` | paid (RF) | UE5 | `link_only` |
| [cgt-hub-old-warehouse-mod](https://www.cgtrader.com/3d-models/architectural/other/asset-pack-vol-11-modular-old-warehouse) | Asset Pack Vol 11 — Modular Old Warehouse | `wall`, `pillar`, `doorway` | paid (RF) | BLEND, FBX, GLB, glTF | `link_only` |
| [cgt-hub-hangar-kit](https://www.cgtrader.com/3d-models/industrial/other/modular-industrial-hangar-e-warehouse-kit) | Modular Industrial Hangar & Warehouse Kit | `wall`, `pillar` | paid (RF) | FBX, PBR | `link_only` |
| [cgt-robot-digit-loader](https://www.cgtrader.com/3d-models/character/sci-fi-character/agility-robotics-digit-loader-robot-animated-rigged-for-blender) | Agility Robotics Digit — Loader (pick/place) | `robot`, `person` | paid (RF) | BLEND, PNG | `link_only` |

## Kitchen

| id | Title | Labels | ~Price | Formats | Status |
|----|-------|--------|--------|---------|--------|
| [cgt-hub-prop-pack-ue](https://www.cgtrader.com/3d-models/industrial/industrial-part/warehouse-and-office-industrial-prop-pack) | Warehouse and Office Industrial Prop Pack (UE5) | `forklift`, `pallet_rack`, `pallet`, `cardboard_box`, `oven` | paid (RF) | UE5 | `link_only` |
| [cgt-kit-cozy-scandi](https://www.cgtrader.com/3d-models/interior/kitchen/cozy-kitchen-pack-scandinavian-kitchen-with-island-and-exterior) | Cozy Kitchen Pack — Scandinavian + Island + Exterior | `cabinet`, `counter`, `dining_table`, `chair`, `refrigerator` | paid (RF) | BLEND, FBX, GLB | `link_only` |
| [cgt-kit-pack-ad](https://www.cgtrader.com/3d-model-packs/kitchen-pack-a-d-hq-low-poly) | Kitchen Pack A–D — HQ Low Poly | `cabinet`, `counter`, `oven`, `refrigerator` | **$155** | MAX, BLEND, C4D, FBX, OBJ | `link_only` |
| [cgt-kit-pack-v2](https://www.cgtrader.com/3d-models/furniture/kitchen-furniture/kitchen-pack-1ab1687b-d832-4896-acaf-4dc698bacca0) | Kitchen Pack V2 — Kitchens / Appliances | `cabinet`, `oven`, `refrigerator`, `microwave` | paid (RF) | FBX, OBJ, BLEND, Maya, GLB | `link_only` |
| [cgt-kit-classic](https://www.cgtrader.com/3d-models/interior/kitchen/classic-kitchen-79c833aa-48a8-431c-a2b8-b5c91618b30f) | Classic Kitchen | `refrigerator`, `oven`, `dining_table`, `chair`, `cabinet` | paid (RF) | glTF, PNG | `link_only` |
| [cgt-kit-classic-collection](https://www.cgtrader.com/3d-model-packs/classic-kitchen-collection-4-models-included) | Classic Kitchen Collection — 4 models | `cabinet`, `oven`, `refrigerator` | **$50** | BLEND, FBX, OBJ, DAE, 3DS | `link_only` |
| [cgt-kit-japandi](https://www.cgtrader.com/3d-models/interior/kitchen/cozy-scandinavian-kitchen-dining-scene-japandi) | Cozy Scandinavian Kitchen Dining Scene (Japandi) | `cabinet`, `dining_table`, `chair`, `counter` | **$30.63** | scene | `link_only` |
| [cgt-kit-scandi-island](https://www.cgtrader.com/3d-models/interior/kitchen/scandinavian-cosy-kitchen-with-island-3d-model) | Scandinavian Kitchen with Island | `cabinet`, `counter` | **$15** | MAX | `link_only` |
| [cgt-kit-apartment-props](https://www.cgtrader.com/3d-models/interior/other/apartment-interior-and-props) | Apartment — Interior and Props | `refrigerator`, `oven`, `cabinet`, `chair`, `dining_table` | paid (RF) | glTF | `link_only` |
| [cgt-kit-minifridge-free](https://www.cgtrader.com/free-3d-models/interior/kitchen/mini-fridge-3d-model-compact-high-poly-refrigerator) | Mini Fridge — Compact High Poly | `refrigerator` | **Free** | BLEND, FBX, OBJ, GLB, glTF | `free_download` |

## Generic indoor

| id | Title | Labels | ~Price | Formats | Status |
|----|-------|--------|--------|---------|--------|
| [cgt-hub-safety-barrier](https://www.cgtrader.com/3d-models/industrial/other/safety-barrier-pbr-game-ready) | Safety Barrier — PBR Game Ready | `safety_barrier`, `signage` | paid (RF) | FBX, PBR | `link_only` |
| [cgt-hub-roll-door-clean](https://www.cgtrader.com/3d-models/architectural/door/industrial-roll-up-door-warehouse-gate-clean) | Industrial Roll Up Door Warehouse Gate (Clean) | `dock_door`, `doorway` | paid (RF) | PBR | `link_only` |
| [cgt-hub-old-warehouse-mod](https://www.cgtrader.com/3d-models/architectural/other/asset-pack-vol-11-modular-old-warehouse) | Asset Pack Vol 11 — Modular Old Warehouse | `wall`, `pillar`, `doorway` | paid (RF) | BLEND, FBX, GLB, glTF | `link_only` |
| [cgt-hub-hangar-kit](https://www.cgtrader.com/3d-models/industrial/other/modular-industrial-hangar-e-warehouse-kit) | Modular Industrial Hangar & Warehouse Kit | `wall`, `pillar` | paid (RF) | FBX, PBR | `link_only` |
| [cgt-kit-apartment-props](https://www.cgtrader.com/3d-models/interior/other/apartment-interior-and-props) | Apartment — Interior and Props | `refrigerator`, `oven`, `cabinet`, `chair`, `dining_table` | paid (RF) | glTF | `link_only` |

## Robot / humanoid proxies

| id | Title | Labels | ~Price | Formats | Status |
|----|-------|--------|--------|---------|--------|
| [cgt-robot-digit](https://www.cgtrader.com/3d-models/character/sci-fi-character/digit-robot-for-blender-animated-set) | Digit Robot For Blender — Animated Set | `robot`, `person` | paid (RF) | BLEND | `link_only` |
| [cgt-robot-digit-walk](https://www.cgtrader.com/3d-models/character/sci-fi-character/agility-robotics-digit-walking-robot-animated-rigged-for-blender) | Agility Robotics Digit — Walking (rigged) | `robot`, `person` | paid (RF) | BLEND, PNG | `link_only` |
| [cgt-robot-digit-loader](https://www.cgtrader.com/3d-models/character/sci-fi-character/agility-robotics-digit-loader-robot-animated-rigged-for-blender) | Agility Robotics Digit — Loader (pick/place) | `robot`, `person` | paid (RF) | BLEND, PNG | `link_only` |
| [cgt-robot-digit-199](https://www.cgtrader.com/3d-models/agility) | Digit from Agility Robotics (~$199) | `robot`, `person` | **$199** | various | `link_only` |

## Counts

| Domain | Unique URLs |
|--------|-------------|
| distribution_hub | 28 |
| warehouse | 27 |
| kitchen | 10 |
| generic_indoor | 5 |
| robot | 4 |

| Status | N |
|--------|---|
| `link_only` | 32 |
| `free_download` | 8 |

Total catalog entries: **40**. **Free CGTrader binaries committed: 0.**

**Buy-first hub:** `cgt-hub-dock-bundle-68` (~$10) + `cgt-hub-storage-kit-12` (~$18). **Buy-first kitchen:** `cgt-kit-cozy-scandi` or `cgt-kit-classic`.

Prices sampled 2026-09-11; confirm at purchase. Paid CGTrader assets are link-only until licensed.
