# Booster K1 — official mesh (Local Map)

| Field | Value |
|-------|--------|
| Source | [BoosterRobotics/booster_assets](https://github.com/BoosterRobotics/booster_assets) |
| URDF | `robots/K1/K1_22dof.urdf` |
| Meshes | `robots/K1/meshes/*.STL` |
| License | **BSD 3-Clause** (see `LICENSE`) |
| Copyright | (c) 2025, BoosterRobotics |

## Packaged asset

- `k1_22dof.glb` — visual links from the 22-DoF URDF, assembled at **real-world meters**, Y-up (Three.js), feet on y=0, standing pose (arms at sides).
- Height ≈ **0.95 m** (matches public K1 spec / URDF).

## Rebuild

```bash
./scripts/fetch-k1-mesh.sh
```

Redistribution of the converted GLB is permitted under the BSD 3-Clause terms of `booster_assets` (retain copyright notice and disclaimer — shipped as `LICENSE` beside this file).
