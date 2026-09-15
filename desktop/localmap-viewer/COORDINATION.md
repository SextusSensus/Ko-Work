# Local Map worker coordination
Updated: 2026-09-15

## User bar
- Map must use real FREE 3D assets (no paid CGTrader).
- Must support 360° mouse-drag orbit (azimuth + polar), scroll zoom, pan — NOT a locked top-down / below-map view.
- Demo domains removed (office, assembly-factory, warehouse-bay-a, distribution-hub; older kitchen/outdoor-patio). `exact-lab` is the only shipped domain; real domains come from + New domain / Import-RerunToDomain.

## Ownership (do not steal lanes)
| Owner | Agent | Owns |
| Orbit camera / OrbitControls | bc-19ceb965 (+ follow-up) | camera only |
| Scene 3D assets UI | bc-5eb7524d | placing free meshes in scene |
| Interaction QA | bc-4e7074e9 | verify drag-orbit + assets |
| Free-only catalog | bc-1677a491 | downloads + FREE-ASSETS.md; strip paid |
| Free twins (no paid URLs) | bc-9406d79e | CC0/procedural equivalents only; FREE-ALTERNATIVES.md; strip paid wishlists |
| Asset ontology / label→asset | bc-9bd2d943 | catalog schema + assignAsset API + asset-placer.js + instances.json (**shipped**) |
| Rerun exact pose / domain ingest | bc-25df0a39 | `localmap_frames` + ingest CLI + placer math + fixture + docs (**this lane**) |
| Assembly line assets | bc-a078625b | library/assembly-line + ontology labels (no camera) |
| Realtime telemetry | bc-137a8586 | WS bridge/feed only |
| Local Map polish | bc-653b389d | scene polish; defer camera |

## Protocol
1. Pull `cursor/k1finder-ui-85c6` before editing.
2. Commit+push often so siblings can merge.
3. Never lock camera to top-down.
4. Never add paid asset URLs.
5. If conflict on viewer.js: keep OrbitControls + asset loading both.
