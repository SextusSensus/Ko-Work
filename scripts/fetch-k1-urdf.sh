#!/usr/bin/env bash
# Sync official Booster K1 URDF + STL meshes from booster_assets (BSD-3-Clause)
# into the Local Map viewer. The viewer loads the URDF at runtime — no GLB bake.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/desktop/localmap-viewer/assets/library/robot/k1"
WORK="${TMPDIR:-/tmp}/booster_k1_urdf_$$"
REPO_URL="${BOOSTER_ASSETS_URL:-https://github.com/BoosterRobotics/booster_assets.git}"
mkdir -p "$OUT_DIR/meshes" "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
echo "==> Cloning $REPO_URL (sparse: robots/K1)"
git clone --depth 1 --filter=blob:none --sparse "$REPO_URL" "$WORK/repo"
git -C "$WORK/repo" sparse-checkout set robots/K1
echo "==> Copying URDF + meshes → $OUT_DIR"
cp "$WORK/repo/robots/K1/K1_22dof.urdf" "$OUT_DIR/K1_22dof.urdf"
cp "$WORK/repo/robots/K1/meshes/"* "$OUT_DIR/meshes/"
git -C "$WORK/repo" show HEAD:LICENSE > "$OUT_DIR/LICENSE"
rm -f "$OUT_DIR/k1_22dof.glb" "$OUT_DIR/k1_22dof.glb"
MESH_COUNT="$(find "$OUT_DIR/meshes" -type f | wc -l | tr -d ' ')"
echo "==> Done: URDF + ${MESH_COUNT} mesh files (no character GLB)"
