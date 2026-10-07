"""Visual layer for the K1 warehouse sim: steel shelving stocked with cartons, wooden pallets,
cardboard boxes, people in hi-vis, a forklift, a concrete floor with aisle tape, painted walls
and overhead lights.

Render-only, by construction. Every geom added here is group 3 (group 4 for the few that ride on
the robot) with contype = conaffinity = 0 and mass 0, so the depth rays (ENV_GROUPS: groups 0/1),
the route planner (world statics) and the physics never see it, and a loose box keeps its
inertia. The only bodies added are massless mocap twins of the workers (so a dressed person can
turn without rotating its proxy). The world proxies it dresses up (groups 0/1) are hidden from
the detailed view by the scene option (k1_warehouse.render_option), NOT by alpha 0: mj_ray skips
alpha-0 geoms, so that would blind the robot too. The robot (group 2, its tote included) stays
visible and is only recoloured. Everything derives from scenario data, never from env.rng, and
none of it goes into the scenario JSON.

Meshes come from the repo's CC0 asset library (desktop/localmap-viewer/assets/library; licenses
in desktop/localmap-viewer/assets/ATTRIBUTION.md). MuJoCo cannot read glTF, so load_gltf does
(stdlib json + numpy). People and the forklift are built from primitives: the library has none.

Usage:  python assets.py selftest      -> ASSETS-SELFTEST-OK
"""
import base64, functools, hashlib, io, json, math, os, pathlib, random, struct, sys, tempfile, threading, urllib.parse

import mujoco
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
LIB = HERE.parents[1] / "desktop/localmap-viewer/assets/library"
CACHE = HERE / ".asset_cache"           # JPG -> PNG conversions: MuJoCo textures load PNG only

_CTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
Y_UP = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)   # glTF is Y-up, MuJoCo Z-up: (x,y,z) -> (x,-z,y)


def _png(key, read):
    """Cached PNG for a texture image; read() returns the source bytes (a JPG/PNG file or a glTF
    image). key must change when the source does (path + mtime). Checked on every build, so a
    deleted cache just regenerates. A read-only checkout caches in the temp dir instead."""
    name = hashlib.sha1(key.encode()).hexdigest()[:16] + ".png"
    dirs = (CACHE, pathlib.Path(tempfile.gettempdir()) / "k1sim_asset_cache")
    hit = next((d / name for d in dirs if (d / name).exists()), None)
    if hit:
        return hit

    def put(d):
        from PIL import Image
        out = d / name
        d.mkdir(exist_ok=True)
        tmp = out.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")   # one per writer
        Image.open(io.BytesIO(read())).convert("RGB").save(tmp, "PNG")
        try:
            tmp.replace(out)                   # atomic: two processes or threads may convert the same file
        except OSError:                        # Windows: out exists and another writer has it open
            tmp.unlink(missing_ok=True)
            if not out.exists():
                raise                          # same bytes either way: the winner's PNG will do
        return out
    try:
        return put(dirs[0])
    except OSError:                            # ponytail: read-only checkout -> temp cache, same bytes
        return put(dirs[1])


def _src(path):
    """An image file as _png's (key, read) pair."""
    return f"{path.resolve()}|{path.stat().st_mtime_ns}", path.read_bytes


@functools.lru_cache(maxsize=None)
def load_gltf(path):
    """A .gltf/.glb file -> one part per material: dict(vert (n,3) Z-up metres with the node
    transforms baked in, face (m,3), uv (n,2) or None, rgba, tex = _png's (key, read) or None).
    uv stays as glTF has it: v = 0 is the image's top row, which is also what MuJoCo's
    usertexcoord means (the selftest renders a quad to check). Parsed once per process; every
    reset reuses the arrays."""
    path = pathlib.Path(path)
    raw = path.read_bytes()
    blob = None
    if raw[:4] == b"glTF":                     # GLB: 12-byte header, then JSON and BIN chunks
        n = struct.unpack_from("<I", raw, 12)[0]
        g = json.loads(raw[20:20 + n])
        if len(raw) > 28 + n:
            blob = raw[28 + n:28 + n + struct.unpack_from("<I", raw, 20 + n)[0]]
    else:
        g = json.loads(raw)
    mtime = path.stat().st_mtime_ns

    def buffer(b):
        uri = b.get("uri")
        if uri is None:
            return blob
        if uri.startswith("data:"):
            return base64.b64decode(uri.split(",", 1)[1])
        return (path.parent / urllib.parse.unquote(uri)).read_bytes()
    bufs = [buffer(b) for b in g.get("buffers", [])]

    def view(i):
        bv = g["bufferViews"][i]
        return bufs[bv["buffer"]], bv.get("byteOffset", 0), bv.get("byteStride")

    def accessor(i):
        # ponytail: no sparse accessors (none in the library); add when an asset needs them.
        a = g["accessors"][i]
        buf, off, stride = view(a["bufferView"])
        dt, n = np.dtype(_CTYPE[a["componentType"]]).newbyteorder("<"), _NCOMP[a["type"]]
        arr = np.ndarray((a["count"], n), dt, buf, off + a.get("byteOffset", 0), (stride or n * dt.itemsize, dt.itemsize))
        return arr / np.iinfo(dt).max if a.get("normalized") else arr.astype(np.float64 if dt.kind == "f" else np.int64)

    def texture(mat):
        info = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture")
        if info is None:
            return None
        k = g["textures"][info["index"]]["source"]
        img, key = g["images"][k], f"{path.resolve()}|{mtime}|image{k}"
        if "uri" not in img:                   # GLB: in a buffer view
            buf, off, _ = view(img["bufferView"])
            n = g["bufferViews"][img["bufferView"]]["byteLength"]
            return key, lambda: buf[off:off + n]
        if img["uri"].startswith("data:"):
            return key, lambda: base64.b64decode(img["uri"].split(",", 1)[1])
        src = path.parent / urllib.parse.unquote(img["uri"])
        return _src(src) if src.is_file() else None   # texture folder not checked out: the colour factor stands in

    def local(nd):
        if "matrix" in nd:
            return np.array(nd["matrix"], float).reshape(4, 4).T     # column-major
        x, y, z, w = nd.get("rotation", [0, 0, 0, 1])
        r = np.zeros(9)
        mujoco.mju_quat2Mat(r, np.array([w, x, y, z], float))
        out = np.eye(4)
        out[:3, :3] = r.reshape(3, 3) * nd.get("scale", [1, 1, 1])   # R @ diag(s)
        out[:3, 3] = nd.get("translation", [0, 0, 0])
        return out

    groups = {}                                # material index -> [verts], [faces], [uvs]
    def walk(i, parent):
        nd = g["nodes"][i]
        tf = parent @ local(nd)
        for pr in g["meshes"][nd["mesh"]]["primitives"] if "mesh" in nd else []:
            if pr.get("mode", 4) != 4:
                continue                       # ponytail: triangle lists only; strips/lines/points skipped
            at = pr["attributes"]
            v = accessor(at["POSITION"]) @ tf[:3, :3].T + tf[:3, 3]
            f = accessor(pr["indices"]).reshape(-1, 3) if "indices" in pr else np.arange(len(v)).reshape(-1, 3)
            if np.linalg.det(tf[:3, :3]) < 0:
                f = f[:, ::-1]                 # mirrored node: keep the winding outward
            vs, fs, uvs = groups.setdefault(pr.get("material", -1), ([], [], []))
            fs.append(f + sum(len(x) for x in vs))
            vs.append(v)
            uvs.append(accessor(at["TEXCOORD_0"]) if "TEXCOORD_0" in at else None)
        for c in nd.get("children", []):
            walk(c, tf)
    kids = {c for nd in g.get("nodes", []) for c in nd.get("children", [])}
    for i in (g["scenes"][g.get("scene", 0)]["nodes"] if "scenes" in g else
              [i for i in range(len(g["nodes"])) if i not in kids]):   # no scenes: walk the root nodes
        walk(i, np.eye(4))

    parts = []
    for k, (vs, fs, uvs) in sorted(groups.items()):
        mat = g["materials"][k] if k >= 0 else {}
        uv = None if any(u is None for u in uvs) else np.concatenate(uvs)
        tex = texture(mat) if uv is not None else None
        rgba = mat.get("pbrMetallicRoughness", {}).get("baseColorFactor", [1, 1, 1, 1])
        if tex is None and "baseColorFactor" not in mat.get("pbrMetallicRoughness", {}):
            rgba = [0.6, 0.6, 0.6, 1]          # texture missing and no factor: neutral grey, not white
        parts.append(dict(vert=np.concatenate(vs) @ Y_UP.T, face=np.concatenate(fs).astype(np.int32),
                          uv=uv, rgba=[float(c) for c in rgba], tex=tex))
    return parts


# ---------------------------------------------------------------------- the visual layer
SHELF = "warehouse/steel_frame_shelves_01/steel_frame_shelves_01_1k.gltf"
SHELF_LEVELS = (0.062, 0.298, 0.535, 0.771)    # shelf tops / height, measured off the mesh (top shelf left bare)
SHELF_TONE = (0.8, 0.8, 0.88)                  # cools the raw wood decks: saturated orange tiles from the top camera
CARTON = "warehouse/cardboard_box_01/cardboard_box_01_1k.gltf"
# Stock sizes on the shelves (m, x along the shelf): the mesh's short side runs along x, so these
# scale it near-uniformly.
CARTONS = ((0.30, 0.40, 0.28), (0.26, 0.30, 0.22), (0.36, 0.46, 0.34))
# Other stock, at the mesh's own size (x along the shelf): red stacking crates, a wooden crate.
CRATES = (("warehouse/plastic_crate_03/plastic_crate_03_1k.gltf", (0.481, 0.265, 0.266)),
          ("warehouse/plastic_crate_01/plastic_crate_01_1k.gltf", (0.298, 0.408, 0.264)))
WOOD_CRATE = ("warehouse/wooden_crate_01/wooden_crate_01_1k.gltf", (0.825, 0.409, 0.35))
STOCK_VERTS = 2_600_000                         # render budget (mesh vertices); the biggest generated hall stocks ~2M
# Dressing budget (geoms made for the statics, vis_geoms): a generated hall is ~500, 20000 one-cell
# obstacles of an imported map 40k (~6 s). A world over it is refused, not dressed for minutes.
MAX_VIS_GEOMS = 50_000
SCALE_STEP = math.log(1.02)                     # library meshes scale in 2% steps: drawn within 1% of the proxy
# A rack's shelving reuses an earlier rack's size if that fits in it and fills BAY_FILL of it on
# every axis, and a loose box an earlier box's carton within BOX_FIT: an imported map's measured
# racks and boxes are all slightly different sizes, and each size is its own 3.6k / 8.8k-vertex mesh.
BAY_FILL, BOX_FIT = 0.9, 0.05
DOOR = "warehouse/rollershutter_door/rollershutter_door_1k.gltf"
FLOOR_TEX = "materials/concrete_color.jpg"     # ambientCG Concrete034
WALL_TEX = "materials/plaster_diff.jpg"        # Poly Haven painted_plaster_wall
# (the library's wooden_pallet_cc0 has every board stacked in one spot, so pallets are built here)
VIS = dict(group=3, contype=0, conaffinity=0, mass=0)
# Group 4: visual parts riding on the robot (its tote carrier). Same rules as group 3, but
# render_option hides them with the robot's body in the head camera.
VIS_ROBOT = dict(VIS, group=4)
BOX, CAP, CYL, SPH, ELL = (mujoco.mjtGeom.mjGEOM_BOX, mujoco.mjtGeom.mjGEOM_CAPSULE, mujoco.mjtGeom.mjGEOM_CYLINDER,
                           mujoco.mjtGeom.mjGEOM_SPHERE, mujoco.mjtGeom.mjGEOM_ELLIPSOID)
TAPE, WOOD, LANE = [0.95, 0.75, 0.05, 1], [0.66, 0.5, 0.32, 1], [0.92, 0.92, 0.9, 1]
# K1 colours: white shell, dark joints (the URDF paints every link the same grey).
K1_SHELL = {"Trunk", "Head_2", "Left_Arm_2", "Right_Arm_2", "Left_Arm_4", "Right_Arm_4", "Left_Hip_Yaw", "Right_Hip_Yaw",
            "Left_Shank", "Right_Shank"}
SKIN = ([0.87, 0.69, 0.56, 1], [0.62, 0.44, 0.32, 1], [0.42, 0.29, 0.2, 1], [0.95, 0.8, 0.68, 1])
HAIR = ([0.12, 0.09, 0.07, 1], [0.35, 0.22, 0.12, 1], [0.05, 0.05, 0.05, 1], [0.6, 0.5, 0.35, 1])


def _yaw(yaw):
    return [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]


@functools.lru_cache(maxsize=None)
def _aabb(name, parts=None):
    v = np.concatenate([p["vert"] for p in load_gltf(LIB / name)[:parts]])
    return v.min(0), v.max(0), len(v)


def _xy_half(half, yaw):
    """World x/y half extents of a yawed box's footprint."""
    c, s_ = abs(math.cos(yaw)), abs(math.sin(yaw))
    return c * half[0] + s_ * half[1], s_ * half[0] + c * half[1]


def _bays(hx, hz):
    """Shelving bays along a rack hx long (half, its long side): the asset's aspect, none narrower
    than 0.5 m (a long, low rack is not 1000s of bays)."""
    lo, hi, _ = _aabb(SHELF)
    return max(1, min(round(2 * hx / (2 * hz * (hi - lo)[0] / (hi - lo)[2])), round(2 * hx / 0.5)))


def _decks(hx):
    """Deck boards across a pallet hx long (half): ~0.16 m apart, wider past 64 (a 500 m pallet is not 6k geoms)."""
    return max(3, min(64, round(2 * hx / 0.16)))


def vis_geoms(st):
    """At most how many geoms add_visuals makes for one static (its own fan-out formulas). Rack
    stock is not counted: STOCK_VERTS caps it for the whole world (~550 geoms)."""
    hx, hy, hz = st["half"]
    if st["kind"] == "pallet":
        return 6 + _decks(hx)
    if st["kind"] == "rack":
        return _bays(max(hx, hy), hz) * len(load_gltf(LIB / SHELF)) + 4   # + its row's floor tape
    return 2


def add_visuals(spec, world, episode):
    """Dress the scene in spec (built by K1WarehouseEnv._build, not yet compiled): one call adds
    the whole render-only layer. Proxies keep their geometry and physics; only the tote's and the
    K1's rgba is repainted; see the module docstring. ValueError, before adding anything, for a
    world over MAX_VIS_GEOMS."""
    n = sum(map(vis_geoms, world["statics"]))
    if n > MAX_VIS_GEOMS:
        raise ValueError(f"world: too detailed to dress (~{n} visual geoms for its statics > {MAX_VIS_GEOMS})")
    made = set()
    wb = spec.worldbody
    L, W = world["size"]

    def material(name, **kw):
        if name not in made:
            made.add(name)
            spec.add_material(name=name, **kw)
        return name

    def textured(name, tex, **kw):            # tex: _png's (key, read), resolved once per build
        if name not in made:
            material(name, **kw)
            spec.add_texture(name=name, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(_png(*tex)))
            spec.material(name).textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = name
        return name

    def asset(body, name, pos, half, yaw, vis=VIS, parts=None):
        """Library asset (its first `parts` parts) scaled (non-uniformly) so its bounding box fills
        pos +- half, yawed."""
        lo, hi, _ = _aabb(name, parts)
        # Scale in SCALE_STEP steps, so instances of nearly the same size (an imported map's measured
        # racks, loose boxes) share one mesh; floored, as MuJoCo rejects a flat mesh.
        k = np.round(np.log(np.maximum(np.asarray(half, float) * 2 / (hi - lo), 1e-3)) / SCALE_STEP).astype(int)
        s = np.exp(k * SCALE_STEP)
        tone = SHELF_TONE if name == SHELF else (1, 1, 1)
        for i, p in enumerate(load_gltf(LIB / name)[:parts]):
            mesh = f"vis_{pathlib.Path(name).stem}{i}_" + "_".join(map(str, k))
            if mesh not in made:               # one mesh per asset and scale; instances share it
                made.add(mesh)
                # Lists, not arrays: add_mesh converts a list ~4x faster (a shelf: 0.4 ms vs 1.9 ms).
                kw = dict(usertexcoord=p["uv"].ravel().tolist()) if p["tex"] else {}
                spec.add_mesh(name=mesh, uservert=((p["vert"] - (lo + hi) / 2) * s).ravel().tolist(),
                              userface=p["face"].ravel().tolist(), **kw)
            mat, rgba = f"vis_{pathlib.Path(name).stem}{i}", [c * t for c, t in zip(p["rgba"], tone)] + p["rgba"][3:]
            if p["tex"]:
                textured(mat, p["tex"], rgba=rgba, specular=0.2, shininess=0.3)
            else:
                material(mat, rgba=rgba, specular=0.2, shininess=0.3)
            body.add_geom(type=mujoco.mjtGeom.mjGEOM_MESH, meshname=mesh, material=mat, pos=pos, quat=_yaw(yaw), **vis)

    def box(pos, half, rgba, yaw=0.0):
        wb.add_geom(type=BOX, pos=pos, size=half, quat=_yaw(yaw), rgba=rgba, material="vis_paint", **VIS)

    material("vis_paint", specular=0.3, shininess=0.5)
    material("vis_matte", specular=0.05, shininess=0.1)
    material("vis_glow", emission=1.0, specular=0.0)

    # Look: a dim neutral roof void above the walls, a soft headlight so no corner goes black.
    spec.add_texture(name="vis_sky", type=mujoco.mjtTexture.mjTEXTURE_SKYBOX, builtin=mujoco.mjtBuiltin.mjBUILTIN_GRADIENT,
                     rgb1=[0.62, 0.64, 0.67], rgb2=[0.42, 0.43, 0.45], width=512, height=3072)
    spec.visual.headlight.ambient = [0.3, 0.3, 0.3]
    spec.visual.headlight.diffuse = [0.25, 0.25, 0.25]
    spec.visual.headlight.specular = [0.05, 0.05, 0.05]
    spec.visual.map.znear = 0.002              # near clip ~5 cm, not 25 cm: a camera at the head sees what it bumps
    # The key light's shadow box spans +-shadowclip * extent; the default (1) spreads the shadow map
    # over ~4x the hall, so every edge stair-steps. 0.45 still covers the largest generated hall.
    spec.visual.map.shadowclip = 0.45

    # Floor: the physics plane (group 0) is hidden in the detailed view, so this layer has its own,
    # as big as that one: past the walls too, without growing the model's extent (which scales the
    # clip planes and shadow box). Not infinite (size 0): that one shadows itself all over. No
    # reflectance: mirrored people under people would end up in the recorded head frames.
    textured("vis_concrete", _src(LIB / FLOOR_TEX), texuniform=True, texrepeat=[0.4, 0.4],
             specular=0.15, shininess=0.3)
    wb.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[L, W, 0.1], pos=[L / 2, W / 2, 0], rgba=[0.78, 0.78, 0.76, 1],
                material="vis_concrete", **VIS)
    textured("vis_wall", _src(LIB / WALL_TEX), texuniform=True, texrepeat=[0.5, 0.5], specular=0.05, shininess=0.1)

    # Lighting: the build's plain overhead light off (the GL renderer uses only 8 lights, the
    # headlight included: headlight + key + up to 6 spots); one near-vertical directional key light
    # casts every shadow (a shadow-casting light costs two extra passes over ~2M carton vertices,
    # so the overhead spot grid fills the hall without them). The key light sits up-beam of the
    # hall centre: its shadow box is centred on the light's axis.
    for light in spec.lights:
        light.active = False
    wb.add_light(type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL, pos=[L / 2 - 2.0, W / 2 - 1.2, 8.0], dir=[0.25, 0.15, -1],
                 castshadow=True, diffuse=[0.35, 0.35, 0.33], specular=[0.0, 0.0, 0.0])
    nx = max(1, min(3, round(L / 6)))
    ny = max(1, min(6 // nx, round(W / 6)))
    for i in range(nx):
        for j in range(ny):
            wb.add_light(pos=[(i + 0.5) * L / nx, (j + 0.5) * W / ny, 6.0], dir=[0, 0, -1], castshadow=False,
                         cutoff=70, exponent=1, diffuse=[0.2, 0.2, 0.19], specular=[0.0, 0.0, 0.0])

    racks = [st for st in world["statics"] if st["kind"] == "rack"]
    # Rack centres and footprint radius bounds (a + b), so each pallet tests only the racks near it.
    rxy = np.array([r["pos"][:2] for r in racks], float).reshape(-1, 2)
    rr = np.array([r["half"][0] + r["half"][1] for r in racks], float)
    rows = []                                  # rack rows for the floor tape: [yaw, half depth, lateral, a0, a1]
    bays = []                                  # shelving sizes drawn so far (half extents)
    verts = 0
    for k, st in enumerate(world["statics"]):
        (px, py, pz), (hx, hy, hz), yaw = st["pos"], st["half"], st["yaw"]
        c, s_ = math.cos(yaw), math.sin(yaw)
        if st["kind"] == "rack":
            if hy > hx:                        # shelving runs along the static's long side
                hx, hy, yaw, c, s_ = hy, hx, yaw + math.pi / 2, -s_, c
            n = _bays(hx, hz)
            want = np.array([hx / n, hy, hz])  # a bay's slot (half extents); bh = the shelving drawn in it
            bh = next((q for q in bays if (q <= want).all() and (q >= BAY_FILL * want).all()), None)
            if bh is None:
                bays.append(bh := want)
            gap = 2 * bh[2] * (SHELF_LEVELS[1] - SHELF_LEVELS[0])
            rnd = random.Random(k)             # stock pattern: fixed per rack, never env.rng
            for b in range(n):
                bx = -hx + (2 * b + 1) * hx / n
                asset(wb, SHELF, [px + c * bx, py + s_ * bx, pz - hz + bh[2]], bh, yaw)   # on the rack's floor
                for lv in SHELF_LEVELS:
                    # One kind of stock per shelf, mostly cartons: a wooden crate on 1 in 3 bottom
                    # shelves, red stacking crates on 1 in 5 shelves.
                    r = rnd.random()
                    sku = WOOD_CRATE if lv == SHELF_LEVELS[0] and r < 0.33 else rnd.choice(CRATES) if r > 0.8 else None
                    x = bx - bh[0] + 0.05
                    while True:
                        name, (sx, sy, sz) = sku or (CARTON, rnd.choice(CARTONS))
                        # ponytail: a carton is ~9k vertices drawn per pass, a crate ~22k; a big imported
                        # site stops stocking at STOCK_VERTS rather than a cheaper mesh per distance.
                        if x + sx > bx + bh[0] - 0.05 or sy + 0.04 > 2 * bh[1] or sz + 0.02 > gap \
                                or verts + _aabb(name)[2] > STOCK_VERTS:
                            break
                        if rnd.random() < 0.8:
                            verts += _aabb(name)[2]
                            z = pz - hz + 2 * bh[2] * lv + sz / 2
                            asset(wb, name, [px + c * (x + sx / 2), py + s_ * (x + sx / 2), z], [sx / 2, sy / 2, sz / 2], yaw)
                        x += sx + 0.03
            a, lat = px * c + py * s_, -px * s_ + py * c
            for r in rows:                     # bays side by side on one line make one row
                if abs(math.remainder(r[0] - yaw, 2 * math.pi)) < 1e-3 and abs(r[1] - hy) < 0.01 \
                        and abs(r[2] - lat) < 0.01 and a - hx < r[4] + 0.3 and a + hx > r[3] - 0.3:
                    r[3], r[4] = min(r[3], a - hx), max(r[4], a + hx)
                    break
            else:
                rows.append([yaw, hy, lat, a - hx, a + hx])
        elif st["kind"] == "pallet":
            # The end of a pallet pushed into a rack bay would slice through the bottom shelf: cut it
            # off (the proxy is inside the rack's proxy there anyway), but only where the rack is
            # square to the pallet and covers that whole end, full height. Any other cut would leave
            # pallet undrawn that the low scan sees and the robot can trip on, so then the pallet is
            # drawn whole.
            lo, hi = [-hx, -hy], [hx, hy]
            # A rack whose footprint (within its centre +- (a + b)) misses the pallet's (+- (hx + hy)) can't cut it.
            for r in (racks[i] for i in np.flatnonzero(np.abs(rxy - (px, py)).max(1) <= rr + (hx + hy))):
                dx, dy = r["pos"][0] - px, r["pos"][1] - py
                cen, ext = (c * dx + s_ * dy, -s_ * dx + c * dy), _xy_half(r["half"], r["yaw"] - yaw)
                ov = [min(hi[i], cen[i] + ext[i]) - max(lo[i], cen[i] - ext[i]) for i in (0, 1)]
                i = int(ov[1] < ov[0])         # cut across the axis it sticks out along
                j = 1 - i
                if min(ov) > 0 and abs(math.remainder(r["yaw"] - yaw, math.pi / 2)) < 1e-3 \
                        and cen[j] - ext[j] <= lo[j] and cen[j] + ext[j] >= hi[j] \
                        and r["pos"][2] - r["half"][2] <= pz - hz + 1e-6 and r["pos"][2] + r["half"][2] >= pz + hz - 1e-6 \
                        and (cen[i] + ext[i] >= hi[i] if cen[i] > 0 else cen[i] - ext[i] <= lo[i]):
                    if cen[i] > 0:
                        hi[i] = cen[i] - ext[i]
                    else:
                        lo[i] = cen[i] + ext[i]
            if hi[0] > lo[0] and hi[1] > lo[1]:
                mx, my = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
                _pallet(wb, [px + c * mx - s_ * my, py + s_ * mx + c * my, pz], [(hi[0] - lo[0]) / 2, (hi[1] - lo[1]) / 2, hz], yaw)
        else:
            # Walls, and obstacles: on an imported Local Map those are mostly occupancy-cell runs of
            # wall, so they get the same paint and adjacent cells read as one wall.
            # A 2D texture maps from a box's local x/y: tip the box so local z is the wall's thin axis,
            # and the plaster lies flat on both faces instead of streaking down them.
            q, flat = np.zeros(4), hy <= hx
            mujoco.mju_mulQuat(q, np.array(_yaw(yaw)), np.array([math.sqrt(0.5), flat * math.sqrt(0.5), (not flat) * math.sqrt(0.5), 0]))
            wb.add_geom(type=BOX, pos=[px, py, pz], size=[hx, hz, hy] if flat else [hz, hy, hx], quat=q,
                        rgba=[1.0, 1.0, 0.97, 1], material="vis_wall", **VIS)
            sh = min(0.15, hz)                 # painted base stripe, proud of both faces
            box([px, py, pz - hz + sh], [hx + 0.004, hy + 0.004, sh], [0.18, 0.2, 0.24, 1], yaw)

    # Floor markings, all from world data so imported maps get them too.
    for yaw, hy, lat, a0, a1 in rows:          # yellow tape around each rack row, 0.1 m out
        c, s_ = math.cos(yaw), math.sin(yaw)
        am, ah, lh = (a0 + a1) / 2, (a1 - a0) / 2 + 0.125, hy + 0.1
        for side in (-1, 1):
            for a, l, half in ((am, lat + side * lh, [ah, 0.025, 0.002]),             # long sides
                               (am + side * (ah - 0.025), lat, [0.025, lh, 0.002])):  # end caps
                box([c * a - s_ * l, s_ * a + c * l, 0.002], half, TAPE, yaw)
    lane = world.get("forklift_lane")
    if lane:                                   # white lane lines 0.1 m outside the forklift's 1.2 x 1.0 m proxy
        (ax, ay), (bx, by) = lane
        yaw = math.atan2(by - ay, bx - ax)
        for side in (-1, 1):
            box([(ax + bx) / 2 - math.sin(yaw) * side * 0.6, (ay + by) / 2 + math.cos(yaw) * side * 0.6, 0.002],
                [math.dist(lane[0], lane[1]) / 2 + 0.7, 0.04, 0.002], LANE, yaw)
    x0, y0, x1, y1 = world["spawn"]            # the dock (spawn area): outlined and hatched
    for side in (-1, 1):
        box([(x0 + x1) / 2, (y0 + y1) / 2 + side * (y1 - y0) / 2, 0.002], [(x1 - x0) / 2 + 0.025, 0.025, 0.002], TAPE)
        box([(x0 + x1) / 2 + side * (x1 - x0) / 2, (y0 + y1) / 2, 0.002], [0.025, max((y1 - y0) / 2, 1e-3), 0.002],
            TAPE)                              # floored: a valid line/point spawn has ymin == ymax
    x0, y0, x1, y1 = x0 + 0.05, y0 + 0.05, x1 - 0.05, y1 - 0.05
    for c in np.arange(y0 - x1 + 0.5, y1 - x0, 1.0):          # 45 deg stripes y = x + c, clipped to the box
        xa, xb = max(x0, y0 - c), min(x1, y1 - c)
        if xb - xa > 0.05:
            box([(xa + xb) / 2, (xa + xb) / 2 + c, 0.002], [(xb - xa) * math.sqrt(0.5), 0.012, 0.002], TAPE, math.pi / 4)
    # A roller-shutter door on the wall the dock runs along, 3 cm proud of the wall proxy: the asset's
    # clean (first) door, stretched to dock width. Imported maps spawn in a 1 m square mid-site: no
    # wall runs along that, so no door.
    x0, y0, x1, y1 = world["spawn"]
    for st in world["statics"]:
        (px, py, pz), (hx, hy, hz), yaw = st["pos"], st["half"], st["yaw"]
        if hy > hx:
            hx, hy, yaw = hy, hx, yaw + math.pi / 2
        c, s_ = math.cos(yaw), math.sin(yaw)
        a = [c * (x - px) + s_ * (y - py) for x in (x0, x1) for y in (y0, y1)]     # dock corners along the wall
        n = [-s_ * (x - px) + c * (y - py) for x in (x0, x1) for y in (y0, y1)]    # ...and off its mid-plane
        a0, a1 = max(min(a), -hx), min(max(a), hx)
        h = 2 * hz - 0.1
        w = h * 1.3
        if st["kind"] == "wall" and h >= 1.0 and a1 - a0 >= w and min(n) * max(n) > 0 and min(map(abs, n)) - hy < 1.0:
            am, off = (a0 + a1) / 2, math.copysign(hy + 0.015, n[0])
            asset(wb, DOOR, [px + c * am - s_ * off, py + s_ * am + c * off, pz - hz + h / 2], [w / 2, 0.015, h / 2],
                  yaw if off > 0 else yaw + math.pi, parts=1)
            break

    cubes = []                                 # loose-box carton sizes drawn so far
    for k, b in enumerate(list(world.get("boxes", [])) + episode["boxes"]):
        h = next((q for q in cubes if abs(q / b["half"] - 1) <= BOX_FIT), None)
        if h is None:
            cubes.append(h := b["half"])
        asset(spec.body(f"box{k}"), CARTON, [0, 0, 0], [h, h, h], 0.0)
    sx, sy = episode["start"][:2]
    for k, w in enumerate(episode["workers"]):
        # Dressed people ride their own mocap body, a twin of the proxy's that K1WarehouseEnv also
        # turns to face where they walk. The proxy capsule never rotates, so the rays stay bit-identical.
        x, y = w["pos"]
        body = wb.add_body(name=f"worker{k}_vis", mocap=True, pos=[x, y, 0.85], quat=_yaw(math.atan2(y - sy, x - sx)))
        _person(body, k, w["role"] == "target")
    if episode["forklift"]:
        _forklift(spec.body("forklift"))

    # K1 paint job + a grey tote (rgba only: not physics). The meshless collision primitives would
    # poke through the shell as dark sleeves: alpha 0 hides them (group 2: no depth ray casts it).
    for g in spec.geoms:
        if g.name == "tote":
            g.rgba = [0.25, 0.32, 0.4, 1]
        elif g.parent.name.startswith("k1/") and g.meshname != "k1/K1logo":
            g.rgba = ([0.92, 0.92, 0.9, 1] if g.meshname[3:] in K1_SHELL else [0.13, 0.13, 0.14, 1]) if g.meshname \
                else [0, 0, 0, 0]
    _tote(spec.body("k1"), asset)


def _tote(body, asset):
    """Dress the payload tote (k1 base frame: centre (0.2, 0, 0.1), half (0.12, 0.15, 0.1)) as an
    open tote on a chest carrier: a lighter rim, a dark opening with a carton in it, a tray under
    it and a bracket plate between it and the trunk."""
    rim, dark = [0.42, 0.5, 0.58, 1], [0.1, 0.1, 0.11, 1]

    def b(pos, half, rgba, mat="vis_paint"):
        body.add_geom(type=BOX, pos=pos, size=half, rgba=rgba, material=mat, **VIS_ROBOT)
    for s in (-1, 1):
        b((0.2 + s * 0.12, 0, 0.19), (0.007, 0.157, 0.012), rim)                 # rim, front and back
        b((0.2, s * 0.15, 0.19), (0.127, 0.007, 0.012), rim)                     # rim, sides
    b((0.2, 0, 0.2), (0.113, 0.143, 0.001), [0.07, 0.08, 0.09, 1], "vis_matte")  # the open top
    b((0.2, 0, -0.008), (0.126, 0.156, 0.008), dark, "vis_matte")                # carrier tray
    b((0.08, 0, 0.12), (0.006, 0.09, 0.07), dark, "vis_matte")                   # bracket to the chest
    asset(body, CARTON, [0.18, 0.03, 0.215], [0.07, 0.09, 0.05], 0.15, VIS_ROBOT)   # something picked


def _pallet(body, pos, half, yaw):
    """Wooden pallet filling the box pos +- half: three stringers, deck boards on top, three on the bottom."""
    (px, py, pz), (hx, hy, hz) = pos, half
    c, s_ = math.cos(yaw), math.sin(yaw)

    def board(x, y, z, bx, by, bz):
        body.add_geom(type=BOX, pos=[px + c * x - s_ * y, py + s_ * x + c * y, pz + z], size=[bx, by, bz],
                      quat=_yaw(yaw), rgba=WOOD, material="vis_matte", **VIS)
    t = hz * 0.18                              # board thickness (half)
    e = min(0.05, hy)                          # bottom board half width: a strip under 10 cm stays inside +- hy
    for y in (-hy + e, 0.0, hy - e):
        board(0, y, 0, hx, min(0.045, e), hz - 2 * t)              # stringers
        board(0, y, -hz + t, hx, e, t)                             # bottom boards
    n = _decks(hx)
    for i in range(n):                                             # deck boards across the stringers
        board(-hx + (2 * i + 1) * hx / n, 0, hz - t, hx / n * 0.7, hy, t)


def _person(body, k, target):
    """Warehouse worker in a body frame 0.85 m up (the proxy capsule's centre), facing +x, ~1.75 m
    tall and inside the 0.22 m capsule radius. The follow target wears an orange vest and a white
    hard hat; everyone else a yellow vest. Skin and hair vary by worker index."""
    z0 = -0.85                                 # the floor, in this frame
    skin, hair = SKIN[k % len(SKIN)], HAIR[(k * 3 + 1) % len(HAIR)]
    vest = [1.0, 0.42, 0.04, 1] if target else [0.82, 0.95, 0.12, 1]
    shirt, legs, shoe = [0.3, 0.34, 0.4, 1], [0.14, 0.16, 0.22, 1], [0.1, 0.09, 0.08, 1]
    band, dark = [0.85, 0.85, 0.82, 1], [0.05, 0.05, 0.06, 1]

    def g(type, size, rgba, pos=(0, 0, 0), fromto=None, mat="vis_matte"):
        kw = dict(fromto=fromto) if fromto else dict(pos=[pos[0], pos[1], z0 + pos[2]])
        body.add_geom(type=type, size=[*size, 0, 0][:3], rgba=rgba, material=mat, **kw, **VIS)

    def ft(a, b):
        return [a[0], a[1], z0 + a[2], b[0], b[1], z0 + b[2]]
    for s in (-1, 1):
        g(ELL, [0.125, 0.05, 0.042], shoe, (0.04, s * 0.09, 0.042))                       # shoe
        g(CAP, [0.052], legs, fromto=ft((0, s * 0.09, 0.11), (0.02, s * 0.09, 0.5)))      # shin
        g(CAP, [0.07], legs, fromto=ft((0.02, s * 0.09, 0.5), (0, s * 0.085, 0.9)))       # thigh
        g(SPH, [0.052], shirt, (0, s * 0.165, 1.4))                                      # shoulder
        g(CAP, [0.045], shirt, fromto=ft((0, s * 0.17, 1.39), (-0.01, s * 0.175, 1.15)))   # upper arm
        g(CAP, [0.037], shirt, fromto=ft((-0.01, s * 0.175, 1.15), (0.05, s * 0.165, 0.93)))  # forearm
        g(ELL, [0.04, 0.025, 0.055], skin, (0.06, s * 0.163, 0.87))                      # hand
    g(ELL, [0.1, 0.14, 0.09], legs, (0, 0, 0.9))                       # hips
    vz, vh = 1.17, 0.3
    g(ELL, [0.115, 0.17, vh], vest, (0, 0, vz), mat="vis_paint")       # torso in a hi-vis vest
    g(ELL, [0.085, 0.165, 0.11], vest, (0, 0, 1.37), mat="vis_paint")  # chest, square to the shoulders
    for z in (1.03, 1.17):                                             # reflective bands
        r = math.sqrt(1 - ((z - vz) / vh) ** 2) + 0.02
        g(ELL, [0.115 * r, 0.17 * r, 0.014], band, (0, 0, z), mat="vis_paint")
    g(CYL, [0.048, 0.05], skin, (0, 0, 1.5))                           # neck
    g(ELL, [0.1, 0.08, 0.112], skin, (0.01, 0, 1.62))                  # head
    g(ELL, [0.103, 0.083, 0.005], dark, (0.01, 0, 1.628), mat="vis_paint")  # clear safety glasses: temples...
    g(ELL, [0.016, 0.06, 0.022], [0.7, 0.78, 0.85, 1], (0.094, 0, 1.618), mat="vis_paint")   # ...lenses
    g(ELL, [0.018, 0.062, 0.005], dark, (0.095, 0, 1.638), mat="vis_paint")  # ...and the top of the frame
    g(ELL, [0.018, 0.013, 0.022], skin, (0.11, 0, 1.59))               # nose
    if target:
        white = [0.96, 0.96, 0.94, 1]
        g(ELL, [0.12, 0.1, 0.075], white, (0, 0, 1.665), mat="vis_paint")      # hard hat, a shell over the head
        g(ELL, [0.125, 0.105, 0.006], white, (0, 0, 1.645), mat="vis_paint")   # rim...
        g(ELL, [0.06, 0.09, 0.006], white, (0.1, 0, 1.645), mat="vis_paint")   # ...out to a short front peak
    else:
        g(ELL, [0.1, 0.084, 0.1], hair, (-0.012, 0, 1.645))


def _forklift(body):
    """Counterbalance forklift in the forklift body frame (origin 1.0 m up, the proxy box's centre;
    x along its lane, forks forward). Kept inside the 1.2 x 1.0 x 2.0 m proxy box, so what you see
    is what the robot can hit."""
    z0 = -1.0
    paint, dark, black = [0.96, 0.62, 0.08, 1], [0.17, 0.17, 0.18, 1], [0.06, 0.06, 0.06, 1]

    def b(pos, half, rgba, mat="vis_paint"):
        body.add_geom(type=BOX, pos=[pos[0], pos[1], z0 + pos[2]], size=half, rgba=rgba, material=mat, **VIS)

    def cyl(a, c, r, rgba, mat="vis_paint"):
        body.add_geom(type=CYL, fromto=[a[0], a[1], z0 + a[2], c[0], c[1], z0 + c[2]], size=[r, 0, 0], rgba=rgba,
                      material=mat, **VIS)
    for x, r in ((0.27, 0.17), (-0.3, 0.13)):          # drive wheels front, steer wheels rear
        for s in (-1, 1):
            cyl((x, s * 0.36, r), (x, s * 0.48, r), r, black, "vis_matte")
            cyl((x, s * 0.355, r), (x, s * 0.485, r), r * 0.55, [0.55, 0.55, 0.55, 1])
    b((-0.06, 0, 0.3), (0.38, 0.35, 0.16), paint)                      # chassis, between the wheels
    b((-0.1, 0, 0.575), (0.37, 0.47, 0.175), paint)                    # body: one deck at 0.75 m over the wheels
    b((-0.47, 0, 0.455), (0.12, 0.49, 0.175), paint)                   # counterweight...
    cyl((-0.47, -0.49, 0.63), (-0.47, 0.49, 0.63), 0.12, paint)        # ...with a rounded top
    b((-0.52, 0, 0.3), (0.075, 0.5, 0.05), black, "vis_matte")         # rear bumper
    b((-0.17, 0, 0.8), (0.13, 0.17, 0.05), black, "vis_matte")         # seat
    b((-0.31, 0, 0.96), (0.03, 0.17, 0.13), black, "vis_matte")        # backrest
    b((0.18, 0, 0.805), (0.07, 0.25, 0.055), paint)                    # dash
    cyl((0.15, 0, 0.86), (0.07, 0, 1.02), 0.022, dark)                 # steering column
    cyl((0.08, 0, 1.025), (0.055, 0, 1.045), 0.12, black, "vis_matte") # steering wheel
    for x in (0.22, -0.42):                                            # overhead guard posts
        for s in (-1, 1):
            b((x, s * 0.42, 1.325), (0.025, 0.025, 0.575), dark)
    b((-0.1, 0, 1.91), (0.35, 0.45, 0.02), dark)                       # guard roof
    for x in (-0.3, -0.15, 0.0, 0.15):
        b((x, 0, 1.935), (0.02, 0.44, 0.006), black)                   # roof slats
    cyl((-0.38, 0, 1.93), (-0.38, 0, 1.99), 0.045, [1.0, 0.55, 0.0, 1], "vis_glow")   # warning beacon
    for s in (-1, 1):
        cyl((0.245, s * 0.42, 1.84), (0.29, s * 0.42, 1.84), 0.045, dark)                     # headlight housing
        cyl((0.29, s * 0.42, 1.84), (0.295, s * 0.42, 1.84), 0.036, [1.0, 0.97, 0.85, 1], "vis_glow")   # lens
        b((0.36, s * 0.22, 0.98), (0.03, 0.04, 0.95), dark)            # mast uprights
        b((0.44, s * 0.18, 0.18), (0.015, 0.05, 0.16), dark)           # fork shanks
        b((0.52, s * 0.18, 0.03), (0.08, 0.05, 0.02), dark)            # forks
    for z in (0.05, 1.0, 1.9):
        b((0.36, 0, z), (0.03, 0.25, 0.03), dark)                      # mast cross members
    b((0.41, 0, 0.36), (0.015, 0.3, 0.2), dark)                        # fork carriage


def selftest():
    # Loader math on a hand-made glTF: data-URI buffer, u8 indices, a column-major matrix parent
    # (+1 along glTF z) over a TRS child (90 deg about y, mirrored x). Expect Z-up vertices
    # (x, -z, y), the winding flipped back by the mirror, and uv as glTF has it.
    blob = (np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32).tobytes()
            + np.array([[0, 0], [1, 0], [0, 0.25]], np.float32).tobytes() + bytes([0, 1, 2, 0]))
    s45 = math.sqrt(0.5)
    g = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
         "nodes": [{"matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1], "children": [1]},
                   {"mesh": 0, "rotation": [0, s45, 0, s45], "scale": [-1, 1, 1]}],
         "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
         "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.2, 0.4, 0.6, 1]}}],
         "buffers": [{"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(blob).decode()}],
         "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}, {"buffer": 0, "byteOffset": 36, "byteLength": 24},
                         {"buffer": 0, "byteOffset": 60, "byteLength": 3}],
         "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
                       {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC2"},
                       {"bufferView": 2, "componentType": 5121, "count": 3, "type": "SCALAR"}]}
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "t.gltf"
        f.write_text(json.dumps(g))
        (p,) = load_gltf(f)
        assert np.allclose(p["vert"], [[0, -1, 0], [0, -2, 0], [0, -1, 1]]), p["vert"]
        assert p["face"].tolist() == [[2, 1, 0]] and np.allclose(p["uv"], [[0, 0], [1, 0], [0, 0.25]]), p
        assert p["rgba"] == [0.2, 0.4, 0.6, 1] and p["tex"] is None

        # ...and glTF's uv convention is MuJoCo's: a quad (on a shallow pyramid: MuJoCo meshes need
        # volume) textured from v in [0, 0.25] of a PNG that is red on top, blue below, renders red.
        from PIL import Image
        img = np.zeros((8, 8, 3), np.uint8)
        img[:4, :, 0], img[4:, :, 2] = 255, 255
        Image.fromarray(img).save(pathlib.Path(tmp) / "uv.png")
        s = mujoco.MjSpec()
        s.add_texture(name="t", type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(pathlib.Path(tmp) / "uv.png"))
        s.add_material(name="m", emission=1)
        s.material("m").textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = "t"
        s.add_mesh(name="q", uservert=[0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0.5, 0.2, 0.5],
                   userface=[0, 1, 2, 0, 2, 3, 0, 4, 1, 1, 4, 2, 2, 4, 3, 3, 4, 0],
                   usertexcoord=[0, 0, 1, 0, 1, 0.25, 0, 0.25, 0.5, 0.1])
        s.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_MESH, meshname="q", material="m")
        m = s.compile()
        d = mujoco.MjData(m)
        mujoco.mj_forward(m, d)
        cam = mujoco.MjvCamera()
        cam.lookat[:], cam.azimuth, cam.elevation, cam.distance = [0.5, 0, 0.5], 90, 0, 2.0   # looking +y at the quad
        with mujoco.Renderer(m, 16, 16) as r:
            r.update_scene(d, cam)
            px = r.render()[8, 8]
        assert px[0] > 200 and px[2] < 50, ("usertexcoord v runs the other way", px)

        # Texture cache: four threads converting one new image all get the cached PNG (each writer
        # its own temp file), and a cache that can't be written (under a file) falls back to temp.
        src, cache, tdir = (pathlib.Path(tmp) / "uv.png").read_bytes(), CACHE, tempfile.tempdir
        try:
            globals()["CACHE"], tempfile.tempdir = pathlib.Path(tmp) / "cache", tmp
            got = []
            ts = [threading.Thread(target=lambda: got.append(_png("selftest-race", lambda: src))) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert len(got) == 4 and {p.parent for p in got} == {CACHE} and got[0].is_file(), got
            globals()["CACHE"] = pathlib.Path(tmp) / "uv.png" / "cache"
            out = _png("selftest-ro", lambda: src)
            assert out.parent == pathlib.Path(tmp) / "k1sim_asset_cache" and out.is_file(), out
        finally:
            globals()["CACHE"], tempfile.tempdir = cache, tdir

    # Every library asset the scene uses (+ two GLBs for loader coverage: u8 indices, an external
    # and an embedded image): sane vertices/faces/uvs, outward winding, a PNG texture, and MuJoCo
    # accepts the mesh.
    spec = mujoco.MjSpec()
    for name in (SHELF, CARTON, *(c[0] for c in CRATES), WOOD_CRATE[0], DOOR, "cross-domain/kenney/cone.glb",
                 "cross-domain/traffic/Traffic_Cone.glb"):
        for p in load_gltf(LIB / name):
            v, f, uv = p["vert"], p["face"], p["uv"]
            assert len(v) >= 3 and np.isfinite(v).all() and f.min() >= 0 and f.max() < len(v), name
            assert uv is not None and uv.shape == (len(v), 2) and np.isfinite(uv).all(), name
            assert p["tex"] is not None and _png(*p["tex"]).is_file(), name
            a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
            assert np.einsum("ij,ij->i", a, np.cross(b, c)).sum() > 0, (name, "inside-out")
            spec.add_mesh(name=f"m{len(spec.meshes)}", uservert=v.ravel(), userface=f.ravel(), usertexcoord=uv.ravel())
            spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_MESH, meshname=f"m{len(spec.meshes) - 1}", **VIS)
    m = spec.compile()
    assert m.nmesh == len(spec.meshes) and (m.mesh_texcoordnum > 0).all()
    lo, hi, _ = _aabb(SHELF)
    assert np.allclose(hi - lo, [10.97, 5.02, 21.41], atol=0.01), hi - lo   # SHELF_LEVELS were measured on this mesh

    def dress(statics, boxes=(), spawn=(0.8, 0.8, 2, 7.2)):
        """add_visuals on a bare spec (a k1 body with a collision box, the loose boxes' bodies), compiled."""
        spec = mujoco.MjSpec()
        for name in ["k1"] + [f"box{k}" for k in range(len(boxes))]:
            spec.worldbody.add_body(name=name)
        spec.body("k1").add_body(name="k1/trunk").add_geom(type=BOX, size=[0.1, 0.1, 0.1])
        add_visuals(spec, {"size": [12, 8], "spawn": list(spawn), "statics": statics},
                    dict(boxes=[{"half": h} for h in boxes], workers=[], forklift=None, start=[1, 1, 0]))
        return spec, spec.compile()

    def st(kind, pos, half, yaw=0.0):
        return {"kind": kind, "pos": pos, "half": half, "yaw": yaw}

    def boards(spec):                          # x0, y0, x1, y1 of the (unyawed) pallet boards drawn
        b = np.array([[*(g.pos[:2] - g.size[:2]), *(g.pos[:2] + g.size[:2])] for g in spec.geoms if np.allclose(g.rgba, WOOD)])
        return [*b[:, :2].min(0), *b[:, 2:].max(0)]

    # A pallet pushed 0.2 m into a rack bay is drawn cut at the rack's face, its outer end kept...
    spec, _ = dress([st("rack", [3, 2, 1], [0.6, 0.4, 1]), st("pallet", [3, 2.55, 0.07], [0.5, 0.25, 0.07])])
    assert np.allclose(boards(spec), [2.5, 2.4, 3.5, 2.8]), boards(spec)
    # (the K1's meshless collision primitives are hidden, not drawn dark over its shell)
    assert next(g for g in spec.geoms if g.parent.name == "k1/trunk").rgba[3] == 0
    # ...but drawn whole where the rack does not cover the cut-off end: a narrow rack across it, a
    # rack over one corner, a rack raised 1 m off the floor, a 6 cm one, a yawed rack into it (the
    # low scan sees all of the proxy; it must not look like empty floor).
    for rack in (st("rack", [4, 3, 1], [0.1, 0.8, 1]), st("rack", [4.65, 3.525, 1], [0.35, 0.475, 1]),
                 st("rack", [4, 3.6, 1.5], [0.7, 0.4, 0.5]), st("rack", [4, 3.6, 0.03], [0.7, 0.4, 0.03])):
        spec, _ = dress([rack, st("pallet", [4, 3, 0.07], [0.6, 0.4, 0.07])])
        assert np.allclose(boards(spec), [3.4, 2.6, 4.6, 3.4]), (rack, boards(spec))
    spec, _ = dress([st("rack", [4, 3, 1], [0.6, 0.4, 1], math.pi / 4), st("pallet", [4, 3.75, 0.07], [0.6, 0.4, 0.07])])
    assert np.allclose(boards(spec), [3.4, 3.35, 4.6, 4.15]), boards(spec)
    # A pallet under 10 cm wide stays inside its proxy: an imported 2 cm strip, and the 3 cm a
    # pallet sticks out of a rack face once cut.
    spec, _ = dress([st("pallet", [6, 6, 0.07], [0.5, 0.01, 0.07])])
    assert np.allclose(boards(spec), [5.5, 5.99, 6.5, 6.01]), boards(spec)
    spec, _ = dress([st("rack", [6, 5, 1], [0.6, 0.5, 1]), st("pallet", [6, 4.72, 0.07], [0.5, 0.25, 0.07])])
    assert np.allclose(boards(spec), [5.5, 4.47, 6.5, 4.5]), boards(spec)

    # Dressing budget: a 1 km pallet is 70 geoms (wider deck boards), and a world over
    # MAX_VIS_GEOMS is refused before anything is added (it took minutes to dress).
    assert vis_geoms(st("pallet", [0, -9, 0.07], [500, 0.5, 0.07])) == 70
    spec = mujoco.MjSpec()
    try:
        add_visuals(spec, {"size": [6, 6], "spawn": [1, 1, 2, 2], "statics": [st("rack", [3, 2, 1], [500, 0.4, 1])] * 100},
                    None)
        raise AssertionError("an over-budget world was dressed")
    except ValueError as e:
        assert "too detailed" in str(e) and not spec.geoms, e

    # Degenerate statics a valid world can hold (any half >= 1 mm) still compile: a 4 mm-high rack,
    # a 1 mm-deep one, a 1 mm loose box, and a 0.1 m wall along the dock (too low for a door).
    spec, _ = dress([st("rack", [4, 3, 0.004], [0.6, 0.4, 0.004]), st("rack", [8, 3, 1], [0.6, 0.001, 1]),
                     st("wall", [6, -0.05, 0.05], [6, 0.05, 0.05])], boxes=[0.001])
    assert not any(g.meshname.startswith("vis_" + pathlib.Path(DOOR).stem) for g in spec.geoms), "door on a 0.1 m wall"
    for spawn in ((1, 1, 3, 1), (2, 3, 2, 3)):   # a line and a point spawn (min == max is valid)
        dress([], spawn=spawn)

    # Mesh sharing: a rack within BAY_FILL of an earlier, smaller one is drawn at that size, on its
    # own floor; a deeper one gets its own mesh. Loose boxes within BOX_FIT share a carton.
    spec, _ = dress([st("rack", [3, 2, 0.95], [0.58, 0.38, 0.95]), st("rack", [3, 4, 1], [0.6, 0.4, 1]),
                     st("rack", [3, 6, 1], [0.6, 0.5, 1])], boxes=[0.2, 0.205, 0.25])
    shelf = [g for g in spec.geoms if g.meshname.startswith("vis_" + pathlib.Path(SHELF).stem)]
    assert len(shelf) == 3 and shelf[0].meshname == shelf[1].meshname != shelf[2].meshname, [g.meshname for g in shelf]
    assert np.isclose(shelf[1].pos[2], 0.95), shelf[1].pos
    box = [next(g.meshname for g in spec.geoms if g.parent.name == f"box{k}") for k in range(3)]
    assert box[0] == box[1] != box[2], box
    print("ASSETS-SELFTEST-OK")


if __name__ == "__main__":
    if sys.argv[1:] != ["selftest"]:
        sys.exit(__doc__)
    selftest()
