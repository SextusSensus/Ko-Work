# Booster K1 — official URDF (Local Map)

| Field | Value |
|-------|--------|
| Source | [BoosterRobotics/booster_assets](https://github.com/BoosterRobotics/booster_assets) |
| URDF | `robots/K1/K1_22dof.urdf` |
| Meshes | `robots/K1/meshes/*.STL` |
| License | **BSD 3-Clause** (see `LICENSE`) |
| Copyright | (c) 2025, BoosterRobotics |

## Viewer usage

Local Map loads **`K1_22dof.urdf`** at runtime via `URDFLoader` + `STLLoader` (real-world metres).  
There is **no** separate character GLB — the URDF package is the source of truth.

## Sync / refresh

```bash
./scripts/fetch-k1-urdf.sh
```

Redistribution of the URDF + STL package is permitted under the BSD 3-Clause terms of `booster_assets` (retain copyright notice and disclaimer — shipped as `LICENSE` beside this file).
