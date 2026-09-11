# Local Map worker coordination
Updated: 2026-09-11T09:12:30Z

## User bar
- Map must use real FREE 3D assets (no paid CGTrader).
- Must support 360° mouse-drag orbit (azimuth + polar), scroll zoom, pan — NOT a locked top-down / below-map view.
- Patio domain replaced by Distribution Hub.

## Ownership (do not steal lanes)
| Owner | Agent | Owns |
| Orbit camera / OrbitControls | bc-19ceb965 (+ follow-up) | camera only |
| Scene 3D assets UI | bc-5eb7524d | placing free meshes in scene |
| Interaction QA | bc-4e7074e9 | verify drag-orbit + assets |
| Free-only catalog | bc-1677a491 | downloads + FREE-ASSETS.md; strip paid |
| CGTrader→free twins | bc-9406d79e | free equivalents only |
| Asset ontology / label→asset | bc-9bd2d943 | catalog schema + assignAsset API |
| Distribution hub domain | bc-f69a0a82 | hub domain content |
| Realtime telemetry | bc-137a8586 | WS bridge/feed only |
| Local Map polish | bc-653b389d | warehouse beauty; defer camera |

## Protocol
1. Pull `cursor/k1finder-ui-85c6` before editing.
2. Commit+push often so siblings can merge.
3. Never lock camera to top-down.
4. Never add paid asset URLs.
5. If conflict on viewer.js: keep OrbitControls + asset loading both.
