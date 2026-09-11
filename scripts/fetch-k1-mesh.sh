#!/usr/bin/env bash
# Fetch official Booster K1 URDF/STL from booster_assets (BSD-3-Clause) and
# assemble a real-scale Y-up GLB for the Local Map viewer.
#
# Output: desktop/localmap-viewer/assets/library/robot/k1/k1_22dof.glb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/desktop/localmap-viewer/assets/library/robot/k1"
OUT_GLB="$OUT_DIR/k1_22dof.glb"
WORK="${TMPDIR:-/tmp}/booster_k1_fetch_$$"
REPO_URL="${BOOSTER_ASSETS_URL:-https://github.com/BoosterRobotics/booster_assets.git}"

mkdir -p "$OUT_DIR" "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "==> Cloning $REPO_URL (sparse: robots/K1 + LICENSE)"
git clone --depth 1 --filter=blob:none --sparse "$REPO_URL" "$WORK/repo"
git -C "$WORK/repo" sparse-checkout set robots/K1 LICENSE

cp "$WORK/repo/LICENSE" "$OUT_DIR/LICENSE"

if [[ -x /tmp/k1mesh/bin/python ]]; then
  PY=/tmp/k1mesh/bin/python
else
  python3 -m venv "$WORK/venv"
  # shellcheck disable=SC1091
  source "$WORK/venv/bin/activate"
  pip install -q 'trimesh[easy]' fast_simplification lxml
  PY="$WORK/venv/bin/python"
fi

echo "==> Assembling URDF visuals → $OUT_GLB"
export WORK OUT_GLB
"$PY" - <<'PY'
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
import trimesh
import os

ROOT = Path(os.environ["WORK"]) / "repo" / "robots" / "K1"
URDF = ROOT / "K1_22dof.urdf"
OUT = Path(os.environ["OUT_GLB"])
MESH_DIR = ROOT / "meshes"

JOINT_Q = {
    "left_shoulder_roll_joint": -1.35,
    "right_shoulder_roll_joint": 1.35,
    "aaleft_shoulder_pitch_joint": 0.2,
    "aaright_shoulder_pitch_joint": 0.2,
    "left_elbow_pitch_joint": 0.4,
    "right_elbow_pitch_joint": 0.4,
}

def rpy_matrix(rpy):
    r, p, y = rpy
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx

def axis_angle(axis, q):
    axis = np.asarray(axis, dtype=float)
    n = np.linalg.norm(axis)
    if n < 1e-12 or abs(q) < 1e-12:
        return np.eye(3)
    axis = axis / n
    x, y, z = axis
    c, s = np.cos(q), np.sin(q)
    C = 1 - c
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])

def T_from_xyz_rpy(xyz, rpy):
    T = np.eye(4)
    T[:3, :3] = rpy_matrix(rpy)
    T[:3, 3] = xyz
    return T

def parse_xyz(s):
    return np.array([float(x) for x in s.split()], dtype=float)

root = ET.parse(URDF).getroot()
link_visuals = {}
for link in root.findall("link"):
    visuals = []
    for v in link.findall("visual"):
        mesh = v.find("geometry/mesh")
        if mesh is None:
            continue
        fn = mesh.get("filename")
        base = Path(fn).name.lower()
        if "collision" in base or "_old" in base or "zed" in base:
            continue
        origin = v.find("origin")
        xyz = parse_xyz(origin.get("xyz") if origin is not None and origin.get("xyz") else "0 0 0")
        rpy = parse_xyz(origin.get("rpy") if origin is not None and origin.get("rpy") else "0 0 0")
        color_el = v.find("material/color")
        rgba = parse_xyz(color_el.get("rgba") if color_el is not None else "0.75 0.75 0.75 1")
        visuals.append(dict(file=fn, xyz=xyz, rpy=rpy, rgba=rgba))
    link_visuals[link.get("name")] = visuals

joints = []
for j in root.findall("joint"):
    origin = j.find("origin")
    xyz = parse_xyz(origin.get("xyz") if origin is not None and origin.get("xyz") else "0 0 0")
    rpy = parse_xyz(origin.get("rpy") if origin is not None and origin.get("rpy") else "0 0 0")
    axis_el = j.find("axis")
    axis = parse_xyz(axis_el.get("xyz") if axis_el is not None else "0 0 1")
    joints.append(dict(
        name=j.get("name"), type=j.get("type"),
        parent=j.find("parent").get("link"), child=j.find("child").get("link"),
        xyz=xyz, rpy=rpy, axis=axis,
    ))

world_T = {"trunk": np.eye(4)}
changed = True
while changed:
    changed = False
    for j in joints:
        if j["child"] in world_T or j["parent"] not in world_T:
            continue
        T = T_from_xyz_rpy(j["xyz"], j["rpy"])
        q = JOINT_Q.get(j["name"], 0.0)
        if j["type"] == "revolute" and abs(q) > 1e-12:
            Rq = np.eye(4)
            Rq[:3, :3] = axis_angle(j["axis"], q)
            T = T @ Rq
        world_T[j["child"]] = world_T[j["parent"]] @ T
        changed = True

T_ros_to_three = np.eye(4)
T_ros_to_three[:3, :3] = np.array([[0, -1, 0], [0, 0, 1], [1, 0, 0]], float)

meshes = []
for link, visuals in link_visuals.items():
    if link not in world_T:
        continue
    Tw = world_T[link]
    for v in visuals:
        path = ROOT / v["file"]
        if not path.exists():
            path = MESH_DIR / Path(v["file"]).name
        if not path.exists():
            raise SystemExit(f"missing mesh {v['file']}")
        m = trimesh.load_mesh(str(path), force="mesh")
        if len(m.faces) > 20000:
            try:
                m = m.simplify_quadric_decimation(0.65)
            except Exception:
                pass
        m.apply_transform(Tw @ T_from_xyz_rpy(v["xyz"], v["rpy"]))
        m.apply_transform(T_ros_to_three)
        rgba = (np.clip(v["rgba"][:3], 0, 1) * 255).astype(np.uint8)
        alpha = int(np.clip(v["rgba"][3], 0, 1) * 255)
        m.visual.face_colors = np.tile(list(rgba) + [alpha], (len(m.faces), 1))
        meshes.append(m)

scene = trimesh.util.concatenate(meshes)
scene.apply_translation([0, -scene.bounds[0, 1], 0])
cx = 0.5 * (scene.bounds[0, 0] + scene.bounds[1, 0])
cz = 0.5 * (scene.bounds[0, 2] + scene.bounds[1, 2])
scene.apply_translation([-cx, 0, -cz])
w, h, d = scene.extents
print(f"extents W={w:.3f} H={h:.3f} D={d:.3f} m (K1 spec ~0.40×0.95×0.18)")
OUT.parent.mkdir(parents=True, exist_ok=True)
scene.export(str(OUT), file_type="glb")
print("wrote", OUT, OUT.stat().st_size, "bytes")
PY

echo "==> Done. Place expectation: $OUT_GLB"
ls -la "$OUT_GLB" "$OUT_DIR/LICENSE"
