"""Importers / exporters for the K1 warehouse sim: scenarios, Local Map sites, MJCF.

  scenario JSON  validate_scenario / validate_world (hostile-input safe: the web UI feeds uploads
                 straight in), save_scenario / load_scenario
  Local Map      localmap_to_world: a robot-mapped site (desktop/localmap-data/domains/<id>) -> sim
                 world; world_to_localmap: the inverse, so a sim world opens in the Local Map viewer
  MJCF           export_mjcf / export_mjcf_zip: the built scene as scene.xml + meshes/ + textures/,
                 loadable by any MuJoCo with no repo checkout

Local Map conventions (from desktop/localmap-viewer/{viewer,asset-placer}.js and
eval/localmap_asset_placer.py):
  frame      map = FLU meters (x fwd, y left, yaw rad about +z). The viewer draws map (x, y) at
             Three.js (x, z) with rotation.y = -yaw.
  occupancy  cells [{x, y, hits}] are cell CENTERS on a res_m grid: the robot keys a cell as
             round(x / res) (robot/follow_person_k1.py), the viewer merges by the same key.
  instances  {label_class, x, y, yaw, w?, h?, pose_map?, scale_m?}. Ontology scale_m is
             [along yaw, height, across]: asset-placer.js scales a footprint asset to
             [w, scale_m[1], h], and rotation.y = -yaw puts its local +x on the map heading.
             So w = extent along yaw, h = extent across it, scale_m[1] = height -- a sim static's
             2*half with the same yaw, no axis swap.
  sim frame  x in [0, L], y in [0, W]. An import translates by source.map_offset so everything
             sits >= MARGIN inside the perimeter walls; a point maps back as map = sim - offset.

Usage:  python sim_io.py selftest                                  -> SIM-IO-SELFTEST-OK
        python sim_io.py list-domains
        python sim_io.py import-localmap <domain> --out scn.json [--task pick|follow] [--seed N]
        python sim_io.py export-localmap <scenario.json> <domain> [--overwrite]
        python sim_io.py export-mjcf <scenario.json> <out_dir>
"""
import argparse, copy, io, json, math, numbers, os, pathlib, re, shutil, tempfile, threading, time, zipfile

import mujoco
import numpy as np

from k1_warehouse import (GRID_RES, INFLATE, PHYS_DT, PLAN_MIN_H, REPO, SCENARIO_FORMAT, SCENARIO_VERSION, VX_MAX,
                          K1WarehouseEnv, _Grid, _static, _yaw_quat, generate_world, heuristic)

DEFAULT_DATA_ROOT = REPO / "desktop/localmap-data"

# ---------------------------------------------------------------------- validation
# Caps on everything a web upload can grow: no memory blowups, no absurd values.
KINDS = ("wall", "rack", "pallet", "obstacle")
MAX_STATICS, MAX_STATIONS, MAX_WORKERS, MAX_BOXES, MAX_GOALS = 20000, 5000, 50, 500, 100
# Loose boxes are free bodies: ~75 in one spot overflow MuJoCo's stack arena (FatalError), 200+
# overflow the constraint arena and every contact is silently dropped (boxes and robot fall
# through statics). Generated episodes have <= 6 boxes, so <= 15 pairs.
MAX_BOX_OVERLAPS = 64
MAX_SIZE_M = 500.0
# Longest t_max sample_episode can generate (3 x pick route / VX_MAX + 30) on a world we accept.
MAX_T = 3.0 * (MAX_GOALS + 1) * math.hypot(MAX_SIZE_M, MAX_SIZE_M) / VX_MAX + 30.0
# The planner grid (k1_warehouse._Grid) costs 20-35 ns per cell of each tall static's
# (inflated) bounding-circle window, clipped to the grid (measured), plus 8 B per grid cell for
# each of up to 257 cached distance fields. MAX_GRID_WORK = sum of those windows: ~1-2 s a build.
MAX_GRID_CELLS, MAX_GRID_WORK = 1_000_000, 5e7
PAD = 10.0                              # statics may sit this far outside [0,L]x[0,W] (perimeter walls)
ANG = 100.0                             # |yaw| bound (rad): any finite yaw works, this keeps junk out


def _obj(d, what, req, opt=()):
    if not isinstance(d, dict):
        raise ValueError(f"{what}: expected an object, got {type(d).__name__}")
    extra = set(d) - set(req) - set(opt)
    if extra:
        raise ValueError(f"{what}: unknown key(s) {sorted(str(k)[:40] for k in extra)[:5]}")
    miss = [k for k in req if k not in d]
    if miss:
        raise ValueError(f"{what}: missing key(s) {miss}")
    return d


def _num(v, what, lo, hi):
    if isinstance(v, bool) or not isinstance(v, numbers.Real):
        raise ValueError(f"{what}: expected a number, got {type(v).__name__}")
    try:
        v = float(v)
    except OverflowError:
        raise ValueError(f"{what}: number too large") from None
    if not lo <= v <= hi:                                   # NaN fails every comparison
        raise ValueError(f"{what}: {v} outside [{lo}, {hi}]")
    return v


def _int(v, what, lo, hi):
    if isinstance(v, bool) or not isinstance(v, numbers.Integral) or not lo <= v <= hi:
        raise ValueError(f"{what}: expected an integer in [{lo}, {hi}]")
    return int(v)


def _list(v, what, lo, hi):
    if not isinstance(v, (list, tuple)):
        raise ValueError(f"{what}: expected a list, got {type(v).__name__}")
    if not lo <= len(v) <= hi:
        raise ValueError(f"{what}: {len(v)} items, expected {lo}..{hi}")
    return v


def _str(v, what, cap=200, choices=None):
    if not isinstance(v, str) or len(v) > cap or (choices and v not in choices):
        raise ValueError(f"{what}: expected " + (f"one of {list(choices)}" if choices else f"a string <= {cap} chars")
                         + f", got {repr(v)[:40]}")
    return v


def _bool(v, what):
    if not isinstance(v, bool):
        raise ValueError(f"{what}: expected true/false, got {type(v).__name__}")
    return v


def _xy(v, what, L, W):
    """A point inside the world. The env places actors on planner cell centers, and the last
    column's center sits up to GRID_RES / 2 past L when L is not a multiple of GRID_RES."""
    x, y = _list(v, what, 2, 2)
    return [_num(x, what + "[0]", 0, L + GRID_RES), _num(y, what + "[1]", 0, W + GRID_RES)]


def _boxes(v, what, L, W):
    out = []
    for i, b in enumerate(_list(v, what, 0, MAX_BOXES)):
        w = f"{what}[{i}]"
        _obj(b, w, ("pos", "half", "mass"))
        out.append({"pos": _xy(b["pos"], w + ".pos", L, W), "half": _num(b["half"], w + ".half", 0.01, 1.0),
                    "mass": _num(b["mass"], w + ".mass", 0.01, 1000)})
    return out


def _box_overlaps(boxes, what):
    """Reject piles of footprint-overlapping loose boxes (see MAX_BOX_OVERLAPS); never edits them."""
    if len(boxes) < 2:
        return
    p, h = np.array([b["pos"] for b in boxes]), np.array([b["half"] for b in boxes])
    mask = np.all(np.abs(p[:, None] - p[None]) < (h[:, None] + h[None])[..., None], axis=-1)
    pairs = (int(mask.sum()) - len(boxes)) // 2
    if pairs > MAX_BOX_OVERLAPS:
        raise ValueError(f"{what}: {pairs} overlapping loose-box pairs (max {MAX_BOX_OVERLAPS}); "
                         "MuJoCo's contact arena overflows")


def _source(v):
    """Provenance: free-form but flat -- short strings, numbers, bools, short number lists."""
    if not isinstance(v, dict) or len(v) > 32 or not isinstance(v.get("kind"), str):
        raise ValueError("world.source: expected an object (<= 32 keys) with a string 'kind'")
    out = {}
    for k, x in v.items():
        what = f"world.source.{_str(k, 'world.source key', 64)}"
        if x is None or isinstance(x, bool):
            out[k] = x
        elif isinstance(x, str):
            out[k] = _str(x, what, 256)
        elif isinstance(x, numbers.Integral):
            out[k] = _int(x, what, -10 ** 18, 10 ** 18)
        elif isinstance(x, (list, tuple)):
            out[k] = [_num(e, what, -1e9, 1e9) for e in _list(x, what, 0, 16)]
        else:
            out[k] = _num(x, what, -1e9, 1e9)
    if out["kind"] == "localmap" and "map_offset" in out and not (
            isinstance(out["map_offset"], list) and len(out["map_offset"]) == 2):
        raise ValueError("world.source.map_offset: expected [x, y] for a localmap source")
    return out


def validate_world(world):
    """Strict, normalized deep copy of a world dict (schema in k1_warehouse.py). ValueError if bad."""
    return _validate_world(world)[0]


def _validate_world(world):
    """validate_world + the planner grid it builds (validate_scenario reuses it)."""
    w = _obj(world, "world", ("name", "size", "statics", "stations", "spawn"),
             ("forklift_lane", "hints", "boxes", "source"))
    L, W = (_num(v, "world.size", 1, MAX_SIZE_M) for v in _list(w["size"], "world.size", 2, 2))
    statics = []
    for i, st in enumerate(_list(w["statics"], "world.statics", 0, MAX_STATICS)):
        what = f"world.statics[{i}]"
        _obj(st, what, ("kind", "pos", "half", "yaw"))
        statics.append({"kind": _str(st["kind"], what + ".kind", choices=KINDS),
                        "pos": [_num(v, what + ".pos", -PAD, hi) for v, hi in
                                zip(_list(st["pos"], what + ".pos", 3, 3), (L + PAD, W + PAD, 50.0))],
                        "half": [_num(v, what + ".half", 1e-3, MAX_SIZE_M) for v in _list(st["half"], what + ".half", 3, 3)],
                        "yaw": _num(st["yaw"], what + ".yaw", -ANG, ANG)})
    nx, ny = math.ceil(L / GRID_RES), math.ceil(W / GRID_RES)
    work = 0
    for st in statics:                  # the window k1_warehouse._Grid rasterizes each tall static over
        (px, py, pz), (hx, hy, hz) = st["pos"], st["half"]
        if pz + hz >= PLAN_MIN_H:
            r = math.hypot(hx + INFLATE, hy + INFLATE)
            work += (max(0, min(nx, int((px + r) / GRID_RES) + 1) - max(0, int((px - r) / GRID_RES)))
                     * max(0, min(ny, int((py + r) / GRID_RES) + 1) - max(0, int((py - r) / GRID_RES))))
    if nx * ny > MAX_GRID_CELLS or work > MAX_GRID_WORK:
        raise ValueError(f"world: too big to plan on ({nx * ny} grid cells, {work} cells under tall statics; "
                         f"limits {MAX_GRID_CELLS}, {MAX_GRID_WORK:.0e})")
    # Same check as sample_episode (a scenario replay skips it): the env's _free_point crashes on a full grid.
    grid = _Grid({"size": [L, W], "statics": statics})
    if not len(grid.free_cells):
        raise ValueError("world: no free floor for the robot (tall statics block every planner cell)")
    sp = [_num(v, "world.spawn", 0, (L, W)[i % 2]) for i, v in enumerate(_list(w["spawn"], "world.spawn", 4, 4))]
    if sp[0] > sp[2] or sp[1] > sp[3]:
        raise ValueError("world.spawn: expected [xmin, ymin, xmax, ymax] with min <= max")
    lane = w.get("forklift_lane")
    out = {"name": _str(w["name"], "world.name"), "size": [L, W], "statics": statics,
           "stations": [_xy(p, f"world.stations[{i}]", L, W)
                        for i, p in enumerate(_list(w["stations"], "world.stations", 0, MAX_STATIONS))],
           "spawn": sp,
           "forklift_lane": None if lane is None else
           [_xy(p, "world.forklift_lane", L, W) for p in _list(lane, "world.forklift_lane", 2, 2)]}
    if w.get("hints") is not None:                               # the env reads null hints as none
        h = _obj(w["hints"], "world.hints", (), ("x0", "x1", "aisles_y"))
        out["hints"] = {k: _num(h[k], "world.hints." + k, 0, L) for k in ("x0", "x1") if k in h}
        if "aisles_y" in h:
            out["hints"]["aisles_y"] = [_num(v, "world.hints.aisles_y", 0, W)
                                        for v in _list(h["aisles_y"], "world.hints.aisles_y", 0, 1000)]
    if "boxes" in w:
        out["boxes"] = _boxes(w["boxes"], "world.boxes", L, W)
        _box_overlaps(out["boxes"], "world.boxes")
    if "source" in w:
        out["source"] = _source(w["source"])
    return out, grid


def validate_scenario(scn):
    """Strict, normalized deep copy of a k1sim.scenario v1. Raises ValueError with what is wrong.
    Values pass through float()/int() unchanged, so a validated scenario replays bit for bit."""
    s = _obj(scn, "scenario", ("format", "version", "task", "seed", "world", "episode"))
    if s["format"] != SCENARIO_FORMAT:
        raise ValueError(f"scenario.format: expected {SCENARIO_FORMAT!r}")
    _int(s["version"], "scenario.version", SCENARIO_VERSION, SCENARIO_VERSION)
    task = _str(s["task"], "scenario.task", choices=("pick", "follow"))
    world, grid = _validate_world(s["world"])
    L, W = world["size"]
    e = _obj(s["episode"], "episode", ("start", "floor_friction", "robot", "link", "sensing", "boxes",
                                       "forklift", "workers", "goals", "t_max"))
    x, y, yaw = _list(e["start"], "episode.start", 3, 3)
    rb = _obj(e["robot"], "episode.robot", ("payload", "force", "kv", "accel", "tau", "gait_start", "sway", "trip_gain"))
    lk = _obj(e["link"], "episode.link", ("latency", "drop_p", "burst_p"))
    sn = _obj(e["sensing"], "episode.sensing", ("depth_c",))
    ep = {"start": [*_xy([x, y], "episode.start", L, W), _num(yaw, "episode.start[2]", -ANG, ANG)],
          "floor_friction": _num(e["floor_friction"], "episode.floor_friction", 0.01, 5),
          # force / kv / sway: the generator's ranges plus margin. Past them the base servo goes
          # unstable at PHYS_DT (explicit-Euler yaw servo: kv > ~85 at payload 0, i.e.
          # 2 * I_yaw_min / PHYS_DT) or sway pushes the robot with zero command, which would drive
          # it well past the action_to_cmd clamps.
          "robot": {"payload": _num(rb["payload"], "episode.robot.payload", 0, 50),
                    "force": _num(rb["force"], "episode.robot.force", 1, 200),
                    "kv": [_num(v, "episode.robot.kv", 1, hi)
                           for v, hi in zip(_list(rb["kv"], "episode.robot.kv", 3, 3), (1000, 1000, 80))],
                    "accel": [_num(v, "episode.robot.accel", 0.01, 100)
                              for v in _list(rb["accel"], "episode.robot.accel", 2, 2)],
                    "tau": _num(rb["tau"], "episode.robot.tau", PHYS_DT, 10),      # < PHYS_DT overshoots
                    "gait_start": _num(rb["gait_start"], "episode.robot.gait_start", 0, 10),
                    "sway": _num(rb["sway"], "episode.robot.sway", 0, 0.05),
                    "trip_gain": _num(rb["trip_gain"], "episode.robot.trip_gain", 0, 1000)},
          "link": {"latency": _num(lk["latency"], "episode.link.latency", 0, 10),
                   "drop_p": _num(lk["drop_p"], "episode.link.drop_p", 0, 1),
                   "burst_p": _num(lk["burst_p"], "episode.link.burst_p", 0, 1)},
          "sensing": {"depth_c": _num(sn["depth_c"], "episode.sensing.depth_c", 0, 1)},
          "boxes": _boxes(e["boxes"], "episode.boxes", L, W),
          "forklift": None, "workers": []}
    # The generator only starts on free planner cells. Inside or against a static, MuJoCo's
    # penetration recovery flings the robot at up to ~19x VX_MAX with a zero command.
    if grid.blocked[grid.cell(ep["start"][:2])]:
        raise ValueError("episode.start: inside or against a static (blocked planner cell); "
                         "the robot would be ejected past the bridge clamps")
    _box_overlaps(world.get("boxes", []) + ep["boxes"], "world.boxes + episode.boxes")   # the env builds both
    if e["forklift"] is not None:
        f = _obj(e["forklift"], "episode.forklift", ("path", "s", "dir", "speed"))
        ep["forklift"] = {"path": [_xy(p, "episode.forklift.path", L, W)
                                   for p in _list(f["path"], "episode.forklift.path", 2, 2)],
                          "s": _num(f["s"], "episode.forklift.s", 0, 1),
                          "dir": _num(f["dir"], "episode.forklift.dir", -1, 1),
                          "speed": _num(f["speed"], "episode.forklift.speed", 0, 10)}
        if ep["forklift"]["dir"] not in (-1.0, 1.0):
            raise ValueError("episode.forklift.dir: expected -1 or 1")
    for i, wk in enumerate(_list(e["workers"], "episode.workers", 1 if task == "follow" else 0, MAX_WORKERS)):
        what = f"episode.workers[{i}]"
        _obj(wk, what, ("pos", "speed", "yields", "role"))
        # speed * CTRL_DT must stay under the 0.7 m non-yield stand-off minus the ~0.57 m
        # tote-corner contact radius, else a worker steps INTO a stood robot in one tick.
        ep["workers"].append({"pos": _xy(wk["pos"], what + ".pos", L, W), "speed": _num(wk["speed"], what + ".speed", 0, 1.3),
                              "yields": _bool(wk["yields"], what + ".yields"),
                              "role": _str(wk["role"], what + ".role", choices=("target", "worker"))})
    # The env follows workers[0] by index; role only colours it. Same layout as sample_episode.
    roles = [w["role"] for w in ep["workers"]]
    if roles != (["target"] + ["worker"] * (len(roles) - 1) if task == "follow" else ["worker"] * len(roles)):
        raise ValueError("episode.workers.role: " + ("follow expects workers[0] = target, the rest worker"
                                                     if task == "follow" else "pick expects all worker"))
    ep["goals"] = [_xy(g, f"episode.goals[{i}]", L, W)
                   for i, g in enumerate(_list(e["goals"], "episode.goals", 1 if task == "pick" else 0, MAX_GOALS))]
    ep["t_max"] = _num(e["t_max"], "episode.t_max", 0.1, MAX_T)
    return {"format": SCENARIO_FORMAT, "version": SCENARIO_VERSION, "task": task,
            "seed": _int(s["seed"], "scenario.seed", 0, 2 ** 63), "world": world, "episode": ep}


def save_scenario(scn, path):
    pathlib.Path(path).write_text(json.dumps(validate_scenario(scn), indent=1, allow_nan=False), encoding="utf-8")


def _loads(path):
    """JSON from a file; utf-8-sig: PowerShell 5 writes a BOM. Deep nesting is a ValueError like any bad input."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))
    except RecursionError:
        raise ValueError(f"{path}: JSON nested too deeply") from None


def load_scenario(path):
    return validate_scenario(_loads(path))


# ---------------------------------------------------------------------- Local Map
DOMAIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MARGIN = 1.0                            # free floor (m) between the map content and the perimeter walls
EXPORT_RES = 0.25                       # occupancy res of an exported domain (exact-lab uses 0.25)
BAY = 1.2                               # one pick station per ~bay of rack face, like generate_world
RACK_CLASSES = {"pallet_rack", "corner_shelf", "shelf", "bookcase"}
WALL_CLASSES = {"wall", "fence", "safety_fence"}
BOX_MASS = {"cardboard_box": 4.0, "crate": 6.0}       # loose, pushable; masses are a guess (kg)
SKIP_CLASSES = {"person", "robot", "floor_markings", "floor_tape", "hallway_carpet"}   # not obstacles


def _domain_dir(domain_id, data_root):
    if not isinstance(domain_id, str) or not DOMAIN_RE.fullmatch(domain_id):
        raise ValueError(f"bad domain id {repr(domain_id)[:70]}: expected [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}")
    base = (pathlib.Path(data_root) / "domains").resolve()
    d = (base / domain_id).resolve()
    if d.parent != base:
        raise ValueError(f"domain id {domain_id!r} resolves outside {base}")
    return d


def _read(path):
    """JSON object from disk, {} if the file is missing."""
    path = pathlib.Path(path)
    if not path.is_file():
        return {}
    d = _loads(path)
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return d


def _write(path, obj):
    """Write via a temp file + rename so a crash never leaves half a domains.json. The temp name is
    unique per call (concurrent writers), and the rename retries: on Windows it fails while
    another process (K1Finder, the viewer) has the target open."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # not mkstemp: keep umask perms
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    for k in range(50):
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if k == 49:
                os.unlink(tmp)
                raise
            time.sleep(0.05)


def _ontology(data_root):
    """label name (id or alias) -> ontology row, first row wins (eval/localmap_asset_placer.py order)."""
    p = pathlib.Path(data_root) / "asset-ontology.json"
    rows = _read(p if p.is_file() else DEFAULT_DATA_ROOT / "asset-ontology.json").get("label_classes") or []
    out = {}
    for row in rows:
        for n in [row.get("id")] + list(row.get("aliases") or []):
            out.setdefault(n, row)
    return out


def list_localmap_domains(data_root=DEFAULT_DATA_ROOT):
    """Registered domains (domains.json order), then unregistered domain folders."""
    root = pathlib.Path(data_root)
    try:
        reg = _read(root / "domains.json").get("domains") or []
    except (OSError, ValueError):
        reg = []                                            # corrupt registry: still list the folders
    names = {d["id"]: d.get("name") for d in (reg if isinstance(reg, list) else [])
             if isinstance(d, dict) and isinstance(d.get("id"), str)}
    dirs = sorted(p.name for p in (root / "domains").glob("*") if p.is_dir())
    out = []
    for did in dict.fromkeys(list(names) + dirs):
        if not isinstance(did, str) or not DOMAIN_RE.fullmatch(did):
            continue
        d = root / "domains" / did
        try:
            n_cells, n_inst = len(_read(d / "occupancy.json").get("cells") or []), len(_read(d / "instances.json").get("instances") or [])
        except (OSError, ValueError, TypeError):
            n_cells = n_inst = 0                            # corrupt file: import says why
        out.append({"id": did, "name": names.get(did) or did, "cells": n_cells, "instances": n_inst})
    return out


def _extent(hx, hy, yaw):
    """Axis-aligned half extents of a yawed box footprint."""
    c, s = abs(math.cos(yaw)), abs(math.sin(yaw))
    return c * hx + s * hy, s * hx + c * hy


def _perimeter(L, W):
    """The 4 walls an import puts MARGIN outside the map content (not mapped: export leaves them out)."""
    return [_static("wall", [L / 2, -0.05, 1], [L / 2, 0.05, 1]), _static("wall", [L / 2, W + 0.05, 1], [L / 2, 0.05, 1]),
            _static("wall", [-0.05, W / 2, 1], [0.05, W / 2, 1]), _static("wall", [L + 0.05, W / 2, 1], [0.05, W / 2, 1])]


def localmap_to_world(domain_id, data_root=DEFAULT_DATA_ROOT, min_hits=1, wall_height=1.0):
    """Local Map domain -> sim world. Occupancy cells (hits >= min_hits) become obstacle statics
    wall_height tall, merged along x per row; instances become racks / pallets / walls /
    obstacles / loose boxes by label class; perimeter walls MARGIN outside the content."""
    ddir = _domain_dir(domain_id, data_root)
    min_hits = _int(min_hits, "min_hits", 1, 10 ** 9)
    wall_height = _num(wall_height, "wall_height", 0.05, 10)
    occ, onto = _read(ddir / "occupancy.json"), _ontology(data_root)
    res = _num(occ.get("res_m", 0.08), "occupancy.res_m", 0.01, 5)    # 0.08: the viewer's default
    hits = {}                               # grid key -> summed hits (duplicates add, like the viewer merge)
    for i, c in enumerate(_list(occ.get("cells") or [], "occupancy.cells", 0, 500_000)):
        what = f"occupancy.cells[{i}]"
        if not isinstance(c, dict):
            raise ValueError(f"{what}: expected an object")
        k = (round(_num(c.get("x"), what + ".x", -1e4, 1e4) / res), round(_num(c.get("y"), what + ".y", -1e4, 1e4) / res))
        hits[k] = hits.get(k, 0) + _num(c.get("hits", 1), what + ".hits", 0, 1e12)
    cells = {k for k, h in hits.items() if h >= min_hits}

    items, skipped = [], 0                  # (kind, x, y, yaw, size along yaw, across, height, box mass, box half)
    for inst in _list(_read(ddir / "instances.json").get("instances") or [], "instances", 0, 100_000):
        try:
            cls = inst.get("label_class") or inst.get("label") or inst.get("class")
            row = onto.get(cls) or {}
            cid = row.get("id", cls)
            if cid in SKIP_CLASSES or (cid not in WALL_CLASSES and row.get("placement") in ("anchor", "wall")):
                skipped += 1                # people, decals, wall/ceiling-mounted things
                continue
            pm = inst.get("pose_map") or {}
            x = _num(inst.get("x", pm.get("x")), "x", -1e4, 1e4)
            y = _num(inst.get("y", pm.get("y")), "y", -1e4, 1e4)
            yaw = inst.get("yaw", pm.get("yaw"))
            if yaw is None and pm.get("qw") is not None:          # placer pose_map is a quaternion
                qw, qx, qy, qz = (_num(pm.get(k, 0), "pose_map." + k, -1e3, 1e3) for k in ("qw", "qx", "qy", "qz"))
                yaw = math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy ** 2 + qz ** 2))
            yaw = _num(yaw or 0.0, "yaw", -ANG, ANG)
            scale = inst.get("scale_m") or row.get("scale_m")
            sx, sy = (inst["w"], inst["h"]) if inst.get("w") and inst.get("h") else (scale[0], scale[2])
            sx, sy = _num(sx, "w", 0.01, MAX_SIZE_M), _num(sy, "h", 0.01, MAX_SIZE_M)
            sz = _num(scale[1], "height", 0.01, 50) if scale else wall_height
            kind = ("rack" if cid in RACK_CLASSES else "wall" if cid in WALL_CLASSES else "pallet" if cid == "pallet"
                    else "box" if cid in BOX_MASS else "obstacle")
            h, mass = min(max((sx + sy + sz) / 6, 0.05), 0.5), BOX_MASS.get(cid)   # a loose box: cube of half h
            if kind == "box" and inst.get("source") == "k1sim":    # the sim's own export: exact size + mass back
                h, mass = _num(sx / 2, "w", 0.01, 1.0), _num(inst.get("mass"), "mass", 0.01, 1000)
            z = inst.get("z", pm.get("z"))                         # placer: the detection's measured height
            if kind == "box" and z is not None and _num(z, "z", -1e4, 1e4) - h > 0.15:
                skipped += 1                # shelf stock / upper carton of a stack: the rack or stack base covers it
                continue
        except (AttributeError, KeyError, TypeError, IndexError, ValueError, OverflowError):
            skipped += 1                    # unsized / unreadable instance
            continue
        items.append((kind, x, y, yaw, sx, sy, sz, mass, h))

    # A floor box must stand alone: MuJoCo flings one spawned inside a rack/wall or another box (up
    # to 25x VX_MAX measured). A carton on a static is that static's stock; a second box on the
    # same spot is a repeat detection. ponytail: O(boxes x statics) numpy, seconds at the 100k cap.
    solid = np.array([(x, y, *_extent(sx / 2, sy / 2, yaw)) for kind, x, y, yaw, sx, sy, *_ in items
                      if kind != "box"]).reshape(-1, 4)
    kept, floor = [], []
    for it in items:
        kind, x, y, *_, h = it
        if kind == "box":
            if (np.any((np.abs(solid[:, 0] - x) < solid[:, 2] + h) & (np.abs(solid[:, 1] - y) < solid[:, 3] + h))
                    or any(abs(x - bx) < h + bh and abs(y - by) < h + bh for bx, by, bh in floor)):
                skipped += 1
                continue
            if len(floor) == MAX_BOXES:
                raise ValueError(f"Local Map domain {domain_id!r}: more than {MAX_BOXES} loose boxes")
            floor.append((x, y, h))
            for i in range(round((x - h) / res), round((x + h) / res) + 1):     # the box replaces the cells the
                for j in range(round((y - h) / res), round((y + h) / res) + 1):  # depth camera saw on it, else
                    cells.discard((i, j))                                        # MuJoCo ejects it
        kept.append(it)
    items = kept

    runs = []                               # [i0, i1, j]: cells merged along x per row
    for i, j in sorted(cells, key=lambda c: (c[1], c[0])):
        if runs and runs[-1][2] == j and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i, j])
    xs, ys = [], []
    for i0, i1, j in runs:
        xs += [(i0 - 0.5) * res, (i1 + 0.5) * res]
        ys += [(j - 0.5) * res, (j + 0.5) * res]
    for _, x, y, yaw, sx, sy, *_ in items:
        ex, ey = _extent(sx / 2, sy / 2, yaw)
        xs += [x - ex, x + ex]
        ys += [y - ey, y + ey]
    if not xs:
        raise ValueError(f"Local Map domain {domain_id!r} is empty (no occupancy cells with hits >= {min_hits}, "
                         "no placeable instances)")
    ox, oy = MARGIN - min(xs), MARGIN - min(ys)
    L, W = max(xs) - min(xs) + 2 * MARGIN, max(ys) - min(ys) + 2 * MARGIN

    statics = _perimeter(L, W)
    statics += [_static("obstacle", [(i0 + i1) / 2 * res + ox, j * res + oy, wall_height / 2],
                        [(i1 - i0 + 1) * res / 2, res / 2, wall_height / 2]) for i0, i1, j in runs]
    boxes = []
    for kind, x, y, yaw, sx, sy, sz, mass, h in items:
        if kind == "box":
            boxes.append({"pos": [x + ox, y + oy], "half": h, "mass": mass})
        else:
            statics.append(_static(kind, [x + ox, y + oy, sz / 2], [sx / 2, sy / 2, sz / 2], yaw))

    try:
        pose = occ.get("pose") or occ["trail"][0]
        cx, cy = _num(pose["x"], "pose.x", -1e4, 1e4) + ox, _num(pose["y"], "pose.y", -1e4, 1e4) + oy
    except (KeyError, IndexError, TypeError, ValueError):
        cx = cy = -1.0
    if not (0 < cx < L and 0 < cy < W):
        cx, cy = L / 2, W / 2               # no usable map pose: the middle of the site
    world = {"name": str(_read(ddir / "manifest.json").get("name") or domain_id)[:200], "size": [L, W],
             "statics": statics, "stations": [],
             "spawn": [max(0.0, cx - 0.5), max(0.0, cy - 0.5), min(L, cx + 0.5), min(W, cy + 0.5)],
             "forklift_lane": None, "boxes": boxes,
             "source": {"kind": "localmap", "domain_id": domain_id, "res_m": res, "min_hits": min_hits,
                        "cells": len(cells), "instances": len(items), "skipped": skipped, "map_offset": [ox, oy]}}
    world = validate_world(world)                                 # caps: a huge site fails here, clearly

    # Pick stations: free floor 0.45 m in front of each rack's long faces, one per bay.
    grid = _Grid(world)
    for st in world["statics"]:
        if st["kind"] != "rack":
            continue
        (px, py, _), (hx, hy, _), yaw = st["pos"], st["half"], st["yaw"]
        c, s = math.cos(yaw), math.sin(yaw)
        (ax, ay), (nx, ny), hl, hs = ((c, s), (-s, c), hx, hy) if hx >= hy else ((-s, c), (c, s), hy, hx)
        n = max(1, round(2 * hl / BAY))
        for k in range(n):
            t = -hl + (k + 0.5) * 2 * hl / n
            for side in (-1, 1):
                p = [px + t * ax + side * (hs + 0.45) * nx, py + t * ay + side * (hs + 0.45) * ny]
                if 0 < p[0] < L and 0 < p[1] < W and not grid.blocked[grid.cell(p)]:
                    world["stations"].append(p)
    del world["stations"][MAX_STATIONS:]                          # ponytail: truncate, a site that big needs a real picker
    return world


def _raster(statics, res, ox=0.0, oy=0.0, size=None):
    """Map-frame cells (i, j) whose centers (i*res, j*res) lie inside a planner-visible static.
    size=[L, W]: only cells where validate_world lets statics sit (PAD around the sim world), so a
    500 m diagonal wall next to a 2 m site does not allocate its whole bounding box."""
    out = set()
    for st in statics:
        (px, py, pz), (hx, hy, hz), yaw = st["pos"], st["half"], st["yaw"]
        if pz + hz < PLAN_MIN_H:
            continue
        hx, hy = max(hx, res / 2), max(hy, res / 2)               # a thin wall still leaves one cell
        ex, ey = _extent(hx, hy, yaw)
        mx, my = px - ox, py - oy
        i0, i1 = math.floor((mx - ex) / res), math.ceil((mx + ex) / res) + 1
        j0, j1 = math.floor((my - ey) / res), math.ceil((my + ey) / res) + 1
        if size is not None:
            i0, i1 = max(i0, math.floor((-PAD - ox) / res)), min(i1, math.ceil((size[0] + PAD - ox) / res) + 1)
            j0, j1 = max(j0, math.floor((-PAD - oy) / res)), min(j1, math.ceil((size[1] + PAD - oy) / res) + 1)
        I, J = np.meshgrid(np.arange(i0, i1), np.arange(j0, j1), indexing="ij")
        dx, dy = I * res - mx, J * res - my
        c, s = math.cos(yaw), math.sin(yaw)
        inside = (np.abs(c * dx + s * dy) <= hx) & (np.abs(-s * dx + c * dy) <= hy)
        out.update(zip(I[inside].tolist(), J[inside].tolist()))
    return out


def world_to_localmap(world, domain_id, data_root=DEFAULT_DATA_ROOT, overwrite=False):
    """Sim world -> Local Map domain (occupancy + instances + manifest, registered in domains.json
    like the viewer/K1Finder create path). An imported world goes back to its own map frame."""
    world = validate_world(world)
    root = pathlib.Path(data_root)
    ddir = _domain_dir(domain_id, root)
    domain_id = ddir.name               # on-disk case: on Windows 'Site-A' IS an existing 'site-a'
    if ddir.exists():                   # robot data may sit in a folder with no manifest yet
        if not overwrite:
            raise FileExistsError(f"Local Map domain {domain_id!r} already exists (overwrite=True replaces it)")
        old = _read(ddir / "manifest.json")             # {} if missing: not the sim's, refused
        if old.get("source") != "k1sim" or old.get("run_count"):     # K1Finder bumps run_count on a robot run
            raise FileExistsError(f"refusing to overwrite robot-mapped Local Map domain {domain_id!r}")
    src = world.get("source") or {}
    statics = world["statics"]
    ox, oy = 0.0, 0.0
    if src.get("kind") == "localmap":   # back to its own map frame, minus the walls the import invented
        ox, oy = src.get("map_offset") or (0.0, 0.0)
        per = _perimeter(*world["size"])
        statics = [st for st in statics if st not in per]
    onto = _ontology(root)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    cells = [{"x": i * EXPORT_RES, "y": j * EXPORT_RES, "hits": 1}
             for i, j in sorted(_raster(statics, EXPORT_RES, ox, oy, world["size"]))]
    insts = []

    def inst(cls, x, y, yaw, w, h, height):
        k = len(insts)
        insts.append({"id": f"k1sim::{cls}{k}", "detection_id": f"{cls}{k}", "label": cls, "label_class": cls,
                      "asset_id": (onto.get(cls) or {}).get("default_asset"),
                      "pose_map": {"x": x, "y": y, "z": 0.0, "qw": math.cos(yaw / 2), "qx": 0.0, "qy": 0.0, "qz": math.sin(yaw / 2)},
                      "x": x, "y": y, "z": 0.0, "yaw": yaw, "w": w, "h": h, "scale_m": [w, height, h],
                      "T_source": "map", "run_id": "k1sim", "placement_method": "sim_export", "confidence": 1.0,
                      "source": "k1sim"})
    for st in world["statics"]:
        (px, py, pz), (hx, hy, hz) = st["pos"], st["half"]
        if st["kind"] in ("rack", "pallet"):
            inst("pallet_rack" if st["kind"] == "rack" else "pallet", px - ox, py - oy, st["yaw"], 2 * hx, 2 * hy, 2 * hz)
        elif pz + hz < PLAN_MIN_H:      # _raster skips it: keep the low trip hazard as an instance (import
            inst(st["kind"], px - ox, py - oy, st["yaw"], 2 * hx, 2 * hy, pz + hz)   # extrudes it from the floor)
    for b in world.get("boxes", []):
        inst("cardboard_box", b["pos"][0] - ox, b["pos"][1] - oy, 0.0, 2 * b["half"], 2 * b["half"], 2 * b["half"])
        insts[-1]["mass"] = b["mass"]   # read back exactly: the import trusts size + mass of source k1sim

    sp = world["spawn"]
    man = {"id": domain_id, "name": world["name"], "created": now, "updated": now, "run_count": 0,
           "cell_count": len(cells), "runs": [], "source": "k1sim",            # no robot runs over sim geometry
           "notes": "exported from the K1 warehouse sim (sim/warehouse/sim_io.py)"}
    ddir.mkdir(parents=True, exist_ok=True)
    _write(ddir / "occupancy.json", {"domain_id": domain_id, "res_m": EXPORT_RES, "range_m": 3.5,
                                     "pose": {"x": (sp[0] + sp[2]) / 2 - ox, "y": (sp[1] + sp[3]) / 2 - oy, "yaw": 0.0},
                                     "trail": [], "cells": cells})
    _write(ddir / "instances.json", {"domain_id": domain_id, "updated": now, "notes": man["notes"], "instances": insts})
    _write(ddir / "manifest.json", man)
    # Registry last, so it never names a half-written domain. Same upsert as server.js / K1Finder.ps1, but
    # 'active' is left alone: K1Finder merges the next real robot mapping run into the active domain.
    # The read-upsert-write holds an exclusive lock file so concurrent exports never lose an entry.
    # ponytail: sim-side lock only; K1Finder.ps1 and server.js don't take it. A registry lock shared
    # by all three tools is out of sim_io's scope.
    lock = root / "domains.json.lock"
    for _ in range(200):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
            break
        except (FileExistsError, PermissionError):       # Windows: PermissionError while a delete is pending
            time.sleep(0.05)
    else:
        raise FileExistsError(f"{lock} held for 10 s: another export is running, or it is stale (delete it)")
    try:
        reg = _read(root / "domains.json")
        doms = reg["domains"] = [d for d in reg.get("domains") or [] if isinstance(d, dict)]
        meta = {"id": domain_id, "name": man["name"], "updated": now, "run_count": man["run_count"], "cell_count": len(cells)}
        k = next((k for k, d in enumerate(doms) if d.get("id") == domain_id), None)
        if k is None:
            doms.append(meta)
        else:
            doms[k] = meta
        _write(root / "domains.json", reg)
    finally:
        lock.unlink()
    return ddir


# ---------------------------------------------------------------------- MJCF
def export_mjcf(env, out_dir):
    """The env's scene as a portable MJCF bundle: out_dir/scene.xml + out_dir/meshes/ +
    out_dir/textures/ (only the files it uses; the visual layer's own meshes are inline in
    scene.xml). This is the scene as BUILT at the last reset: keyframe "start" holds the
    episode's start state (robot pose, workers, forklift, loose boxes), not wherever the episode has
    moved them. A plain viewer opens at qpos0 (robot and actors at the origin) until that key is
    loaded. Verified to reload, and the key to place the robot, before returning."""
    import xml.etree.ElementTree as ET
    if env.mj_spec is None:
        raise ValueError("env has no scene yet: call env.reset() first")
    out = pathlib.Path(out_dir)
    # to_xml() re-opens every texture file. The visual layer's PNGs live in the git-ignored
    # .asset_cache, which may have been cleared since the last reset; the compiled model still has
    # their pixels (identical to the PNG's), so put any missing one back. env.model/data untouched.
    for t in env.mj_spec.textures:
        f = pathlib.Path(env.mj_spec.texturedir) / t.file
        if t.file and not f.exists():
            from PIL import Image
            m, i = env.model, env.model.texture(t.name).id
            w, h, nc = m.tex_width[i], m.tex_height[i], m.tex_nchannel[i]
            px = m.tex_data[m.tex_adr[i]:m.tex_adr[i] + w * h * nc].reshape(h, w, nc)
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            Image.fromarray(px.squeeze(-1) if nc == 1 else px).save(tmp, "PNG")
            os.replace(tmp, f)
    root = ET.fromstring(env.mj_spec.to_xml())
    comp = root.find("compiler")
    for tag, attr, sub in (("mesh", "meshdir", "meshes"), ("texture", "texturedir", "textures")):
        (out / sub).mkdir(parents=True, exist_ok=True)
        base = pathlib.Path(comp.get(attr, "") if comp is not None else "")
        seen = {}
        for el in root.iter(tag):
            if el.get("file"):
                src = base / el.get("file")
                if seen.setdefault(src.name, src) != src:
                    raise ValueError(f"two {tag}s named {src.name} from different folders")
                shutil.copyfile(src, out / sub / src.name)
                el.set("file", f"{sub}/{src.name}")
        if comp is not None:
            comp.set(attr, ".")                                   # relative to scene.xml

    # The spec only has build-time poses (robot and actors at the origin); reset() places them in
    # env.data, which has moved on since. So the start state goes in as a keyframe, from the scenario.
    ep, em = env.scenario["episode"], env.model
    qpos = em.qpos0.copy()                                        # loose boxes: qpos0 is their start pose
    qpos[:3] = ep["start"]
    # Mocap bodies start as built (a dressed worker's twin is built at the worker's start pose),
    # then the actors go where reset() puts them.
    mb = np.nonzero(em.body_mocapid >= 0)[0]
    mpos, mquat = np.zeros((em.nmocap, 3)), np.zeros((em.nmocap, 4))
    mpos[em.body_mocapid[mb]], mquat[em.body_mocapid[mb]] = em.body_pos[mb], em.body_quat[mb]
    for k, w in enumerate(ep["workers"]):                         # as _place_workers / _place_forklift
        mpos[em.body_mocapid[em.body(f"worker{k}").id]] = [w["pos"][0], w["pos"][1], 0.85]
    if ep["forklift"]:
        f = ep["forklift"]
        (ax, ay), (bx, by) = f["path"]
        mid = em.body_mocapid[em.body("forklift").id]
        mpos[mid] = [ax + f["s"] * (bx - ax), ay + f["s"] * (by - ay), 1.0]
        mquat[mid] = _yaw_quat(math.atan2(by - ay, bx - ax))

    def fmt(a):                                                   # repr(np.float64) is "np.float64(..)"
        return " ".join(repr(float(v)) for v in np.ravel(a))
    key = {"name": "start", "qpos": fmt(qpos)}
    if em.nmocap:
        key.update(mpos=fmt(mpos), mquat=fmt(mquat))
    ET.SubElement(ET.SubElement(root, "keyframe"), "key", key)

    path = out / "scene.xml"
    path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    m = mujoco.MjModel.from_xml_path(str(path))
    if (m.nbody, m.ngeom, m.nmesh, m.ntex) != (em.nbody, em.ngeom, em.nmesh, em.ntex):
        raise RuntimeError(f"exported scene reloads as {m.nbody} bodies / {m.ngeom} geoms / {m.nmesh} meshes / "
                           f"{m.ntex} textures, expected {em.nbody} / {em.ngeom} / {em.nmesh} / {em.ntex}")
    d = mujoco.MjData(m)
    mujoco.mj_resetDataKeyframe(m, d, m.key("start").id)
    mujoco.mj_forward(m, d)
    if not np.allclose(d.xpos[m.body("k1").id][:2], ep["start"][:2]):
        raise RuntimeError(f"exported keyframe puts the robot at {d.xpos[m.body('k1').id][:2]}, "
                           f"expected {ep['start'][:2]}")
    return path


def export_mjcf_zip(env):
    """export_mjcf as zip bytes (scene.xml + meshes/ + textures/ at the archive root)."""
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td) / "mjcf"
        export_mjcf(env, d)
        return pathlib.Path(shutil.make_archive(str(d), "zip", d)).read_bytes()


# ---------------------------------------------------------------------- selftest
def _rollout(env, n, **reset_kw):
    o, _ = env.reset(**reset_kw)
    seq = [o]
    for _ in range(n):
        seq.append(env.step(heuristic(seq[-1]))[0])
    return np.array(seq)


def _fixture(root):
    """Synthetic Local Map domain 'fixture' in map coords that go negative (exercises the offset):
    an L of wall cells, two pallet racks (one sized by w/h, one rotated + sized by the ontology),
    a cardboard box (over a depth cell it must replace), a person (skipped) and a 1-hit noise cell.
    49 cell entries, 47 distinct grid cells."""
    res = 0.25
    cells = [{"x": i * res, "y": -2.0, "hits": 3} for i in range(-12, 17)]          # row y=-2, x -3..4
    cells += [{"x": -3.0, "y": j * res, "hits": 3} for j in range(-8, 9)]           # column x=-3, y -2..2
    cells += [{"x": -3.01, "y": 0.02, "hits": 2}, {"x": 3.0, "y": 1.75, "hits": 1},  # jittered dup, noise,
              {"x": 2.5, "y": 1.0, "hits": 2}]                                      # the box seen by depth
    insts = [{"label_class": "pallet_rack", "x": 0.5, "y": -0.5, "yaw": 0.0, "w": 2.4, "h": 0.9},
             {"label": "rack", "pose_map": {"x": 0.5, "y": 1.5, "z": 0.0, "qw": math.cos(math.pi / 4), "qx": 0.0,
                                            "qy": 0.0, "qz": math.sin(math.pi / 4)}},
             {"label_class": "cardboard_box", "x": 2.5, "y": 1.0, "yaw": 0.3},
             {"label_class": "person", "x": 1.0, "y": 1.0}]
    d = root / "domains" / "fixture"
    d.mkdir(parents=True)
    shutil.copyfile(DEFAULT_DATA_ROOT / "asset-ontology.json", root / "asset-ontology.json")
    _write(root / "domains.json", {"active": "fixture", "domains": [{"id": "fixture", "name": "Fixture site"}]})
    _write(d / "manifest.json", {"id": "fixture", "name": "Fixture site", "runs": []})
    (d / "occupancy.json").write_text(chr(0xFEFF) + json.dumps({"domain_id": "fixture", "res_m": res, "pose": {"x": 2.0, "y": 0.4, "yaw": 0},
                                                            "cells": cells}), encoding="utf-8")   # BOM like PowerShell
    _write(d / "instances.json", {"domain_id": "fixture", "instances": insts})
    return {(round(c["x"] / res), round(c["y"] / res)) for c in cells if c["hits"] >= 1}


def selftest():
    repo_reg = (DEFAULT_DATA_ROOT / "domains.json").read_bytes()

    # 1. Validation is an identity on real scenarios: a validated (and saved/loaded) scenario
    #    replays bit for bit against the seeded reset it came from.
    with tempfile.TemporaryDirectory() as td:
        for task in ("pick", "follow"):
            env = K1WarehouseEnv(task=task)
            base = _rollout(env, 40, seed=21)
            scn = validate_scenario(env.make_scenario(21))
            assert np.array_equal(base, _rollout(env, 40, options={"scenario": scn})), task
            save_scenario(scn, pathlib.Path(td) / "s.json")
            assert load_scenario(pathlib.Path(td) / "s.json") == scn
            assert np.array_equal(base, _rollout(env, 40, options={"scenario": load_scenario(pathlib.Path(td) / "s.json")}))

    # 2. Hostile input: every one of these must be a clean ValueError, and the input untouched.
    good = K1WarehouseEnv(task="pick").make_scenario(3)
    keep = copy.deepcopy(good)
    out = validate_scenario(good)
    out["world"]["statics"][0]["pos"][0] = 99.0
    assert good == keep and out is not good, "validate must deep-copy"
    wk = {"pos": [1.0, 1.0], "speed": 0.5, "yields": True, "role": "worker"}
    bad = {
        "format": lambda s: s.update(format="x"), "version": lambda s: s.update(version=2),
        "version bool": lambda s: s.update(version=True), "task": lambda s: s.update(task="dance"),
        "seed<0": lambda s: s.update(seed=-1), "seed float": lambda s: s.update(seed=1.5),
        "seed bool": lambda s: s.update(seed=True), "seed big": lambda s: s.update(seed=2 ** 64),
        "unknown key": lambda s: s["episode"].update(extra=1), "missing key": lambda s: s["episode"].pop("t_max"),
        "size small": lambda s: s["world"].update(size=[0.5, 10]), "size nan": lambda s: s["world"].update(size=[float("nan"), 10]),
        "size big": lambda s: s["world"].update(size=[1000, 10]), "size str": lambda s: s["world"].update(size="10x10"),
        "half 0": lambda s: s["world"]["statics"][0].update(half=[0, 1, 1]),
        "half <0": lambda s: s["world"]["statics"][0].update(half=[-1, 1, 1]),
        "pos str": lambda s: s["world"]["statics"][0].update(pos=["1", 2, 3]),
        "kind": lambda s: s["world"]["statics"][0].update(kind="lava"),
        "statics cap": lambda s: s["world"].update(statics=[s["world"]["statics"][0]] * (MAX_STATICS + 1)),
        "grid cells": lambda s: s["world"].update(size=[400, 400]),
        "grid work": lambda s: s["world"].update(size=[150, 150], statics=[_static("obstacle", [75, 75, 0.5], [20, 20, 0.5])] * 1000),
        "station out": lambda s: s["world"]["stations"].append([-1.0, 1.0]),
        "spawn flipped": lambda s: s["world"].update(spawn=[2, 1, 1, 2]),
        "spawn out": lambda s: s["world"]["spawn"].__setitem__(2, 1e6),
        "source long": lambda s: s["world"].update(source={"kind": "x" * 1000}),
        "box half": lambda s: s["world"].update(boxes=[{"pos": [1, 1], "half": 5, "mass": 1}]),
        "workers cap": lambda s: s["episode"].update(workers=[wk] * (MAX_WORKERS + 1)),
        "worker out": lambda s: s["episode"]["workers"].append(dict(wk, pos=[1e3, 1.0])),
        "yields int": lambda s: s["episode"]["workers"].append(dict(wk, yields=1)),
        "start inf": lambda s: s["episode"]["start"].__setitem__(0, float("inf")),
        "drop_p": lambda s: s["episode"]["link"].update(drop_p=1.5),
        "t_max": lambda s: s["episode"].update(t_max=MAX_T + 1), "t_max huge int": lambda s: s["episode"].update(t_max=10 ** 400),
        "no goals": lambda s: s["episode"].update(goals=[]), "goals str": lambda s: s["episode"].update(goals="abc"),
        "goals cap": lambda s: s["episode"].update(goals=[[1.0, 1.0]] * (MAX_GOALS + 1)),
        "kv len": lambda s: s["episode"]["robot"].update(kv=[1, 2]), "tau 0": lambda s: s["episode"]["robot"].update(tau=0),
        "kv huge": lambda s: s["episode"]["robot"].update(kv=[1e5] * 3), "yaw kv": lambda s: s["episode"]["robot"]["kv"].__setitem__(2, 150),
        "force huge": lambda s: s["episode"]["robot"].update(force=1e4), "sway": lambda s: s["episode"]["robot"].update(sway=1.0),
        "no free floor": lambda s: s["world"]["statics"].append(
            {"kind": "obstacle", "pos": [s["world"]["size"][0] / 2, s["world"]["size"][1] / 2, 0.5],
             "half": [s["world"]["size"][0] / 2, s["world"]["size"][1] / 2, 0.5], "yaw": 0.0}),
        "forklift dir": lambda s: s["episode"].update(forklift={"path": [[1, 1], [2, 2]], "s": 0, "dir": 0.5, "speed": 1}),
        "follow no target": lambda s: s.update(task="follow") or s["episode"].update(workers=[]),
        "follow target not [0]": lambda s: s.update(task="follow") or s["episode"].update(workers=[wk, dict(wk, role="target")]),
        "follow two targets": lambda s: s.update(task="follow") or s["episode"].update(workers=[dict(wk, role="target")] * 2),
        "pick target": lambda s: s["episode"]["workers"].append(dict(wk, role="target")),
        "worker speed 3": lambda s: s["episode"]["workers"].append(dict(wk, speed=3)),
        "yaw kv 90": lambda s: s["episode"]["robot"]["kv"].__setitem__(2, 90),
        "start in rack": lambda s: s["episode"].update(start=[rk["pos"][0], rk["pos"][1] - rk["half"][1] - 0.1, 1.5708]),
        "boxes piled": lambda s: s["episode"].update(boxes=[{"pos": [1.0, 1.0], "half": 0.3, "mass": 1.0}] * 75),
        "boxes piled world+ep": lambda s: s["world"].update(boxes=[{"pos": [1.0, 1.0], "half": 0.3, "mass": 1.0}] * 10)
        or s["episode"].update(boxes=[{"pos": [1.2, 1.0], "half": 0.3, "mass": 1.0}] * 10),   # 45 + 45 pairs pass alone
        "map_offset str": lambda s: s["world"].update(source={"kind": "localmap", "map_offset": "ab"}),
    }
    rk = next(t for t in good["world"]["statics"] if t["kind"] == "rack")
    for name, mutate in bad.items():
        s = copy.deepcopy(good)
        mutate(s)
        try:
            validate_scenario(s)
        except ValueError as e:
            assert len(str(e)) < 300, (name, str(e)[:300])
            continue
        raise AssertionError(f"hostile scenario accepted: {name}")
    for junk in (None, [], "x", 1):
        try:
            validate_scenario(junk)
            raise AssertionError(f"accepted {junk!r}")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as td:                    # too deep for json: ValueError, not RecursionError
        p = pathlib.Path(td) / "deep.json"
        p.write_text('{"format": ' + "[" * 100000 + "]" * 100000 + "}")
        try:
            load_scenario(p)
            raise AssertionError("accepted deeply nested JSON")
        except ValueError:
            pass
    # Grid cost is the tall statics' own footprints: a 100x60 m site of 8000 short wall runs is cheap.
    validate_world({"name": "site", "size": [100, 60], "stations": [], "spawn": [0, 0, 1, 1],
                    "statics": [_static("wall", [1 + k % 190 * 0.5, 1 + k // 190 * 1.4, 0.5], [0.1, 0.04, 0.5])
                                for k in range(8000)]})

    # ...but every scenario the env generates passes: actors on the last grid column, whose
    # centers sit past L on a wall-less 5.05 m map, and a long pick tour (t_max > 2 h) down a hall.
    env = K1WarehouseEnv(task="follow", n_boxes=(6, 6), world=validate_world(
        {"name": "open", "size": [5.05, 5.05], "statics": [], "stations": [], "spawn": [1, 1, 2, 2]}))
    scns = [validate_scenario(env.make_scenario(s)) for s in range(10)]
    assert max(p[0] for s in scns for p in [b["pos"] for b in s["episode"]["boxes"]]
               + [w["pos"] for w in s["episode"]["workers"]]) > 5.05
    hall = {"name": "hall", "size": [400, 6], "stations": [[5, 3], [395, 3]], "spawn": [1, 1, 3, 5],
            "statics": [_static("wall", [200, -0.05, 1], [200, 0.05, 1]), _static("wall", [200, 6.05, 1], [200, 0.05, 1]),
                        _static("wall", [-0.05, 3, 1], [0.05, 3, 1]), _static("wall", [400.05, 3, 1], [0.05, 3, 1])]}
    scn = K1WarehouseEnv(task="pick", world=validate_world(hall)).make_scenario(0)
    assert scn["episode"]["t_max"] > 7200 and validate_scenario(scn) == scn

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # 3. Local Map import of the synthetic site.
        map_cells = _fixture(root)
        world = localmap_to_world("fixture", data_root=root)
        kinds = [st["kind"] for st in world["statics"]]
        src = world["source"]
        assert kinds.count("rack") == 2 and kinds.count("wall") == 4 and len(world["boxes"]) == 1, kinds
        assert src["skipped"] == 1 and src["cells"] == len(map_cells) - 1, src    # person; box ate its cell
        assert np.allclose(src["map_offset"], [1 + 3.125, 1 + 2.125]) and np.allclose(world["size"], [9.25, 6.25]), src
        # Row y=-2 is one merged static; the column is one per cell (same row index merges never).
        assert kinds.count("obstacle") == 1 + 16 + 1, kinds.count("obstacle")
        rot = [st for st in world["statics"] if st["kind"] == "rack" and abs(st["yaw"]) > 1][0]
        assert np.allclose(rot["half"], [0.6, 0.45, 1.5]) and np.isclose(rot["yaw"], math.pi / 2), rot   # ontology scale_m
        ox, oy = src["map_offset"]
        assert any(abs(st["pos"][0] - (-3.0 + ox)) <= st["half"][0] and abs(st["pos"][1] - (-2.0 + oy)) <= st["half"][1]
                   for st in world["statics"] if st["kind"] == "obstacle"), "map (-3,-2) not at sim (-3,-2)+offset"
        assert localmap_to_world("fixture", data_root=root, min_hits=3)["source"]["cells"] == len(map_cells) - 2   # noise, box
        assert len(world["stations"]) >= 4 and world["spawn"] == [2.0 + ox - 0.5, 0.4 + oy - 0.5, 2.0 + ox + 0.5, 0.4 + oy + 0.5]
        assert list_localmap_domains(root) == [{"id": "fixture", "name": "Fixture site", "cells": 49, "instances": 4}]
        with tempfile.TemporaryDirectory() as td2:    # corrupt registry / domain files never break the listing
            r2 = pathlib.Path(td2)
            (r2 / "domains/bad").mkdir(parents=True)
            (r2 / "domains/bad/occupancy.json").write_text('{"cells": 5}')
            (r2 / "domains/bad/instances.json").write_text('{"instances": 1.5}')
            for reg in ('{"domains": [{"id": ["x"]}, 5, {"id": "bad", "name": "Bad"}]}', '{"domains": 5}', "{oops"):
                (r2 / "domains.json").write_text(reg)
                assert [(d["id"], d["cells"], d["instances"]) for d in list_localmap_domains(r2)] == [("bad", 0, 0)], reg

        # Robot shelf data: a carton on a rack shelf (z), one inside the rack footprint, and a repeat
        # detection of a floor carton are not loose floor boxes, and keep the cells under them;
        # quaternion junk (overflow, str * huge int) skips just that instance.
        d = root / "domains/shelf"
        d.mkdir(parents=True)
        shelf_cells = [{"x": i * 0.08, "y": 0.32, "hits": 2} for i in range(-5, 6)] + [{"x": 3.04, "y": 0.0, "hits": 2}]
        _write(d / "occupancy.json", {"res_m": 0.08, "pose": {"x": 3.0, "y": 2.0}, "cells": shelf_cells})
        _write(d / "instances.json", {"instances": [
            {"label_class": "pallet_rack", "x": 0, "y": 0, "yaw": 0, "w": 2.4, "h": 0.9},
            {"label_class": "cardboard_box", "x": 0, "y": 0.3, "z": 1.1},
            {"label_class": "cardboard_box", "x": 0.5, "y": 0.0},
            {"label_class": "cardboard_box", "x": 3.0, "y": 0.0, "pose_map": {"x": 3.0, "y": 0.0, "z": 0.2}},
            {"label_class": "cardboard_box", "x": 3.05, "y": 0.02},
            {"label_class": "pallet_rack", "pose_map": {"x": 1, "y": 3, "qw": 1.0, "qy": 1e200}, "w": 1, "h": 1},
            {"label_class": "pallet_rack", "pose_map": {"x": 1, "y": 3, "qw": "x", "qz": 10 ** 13}, "w": 1, "h": 1}]})
        shelf = localmap_to_world("shelf", data_root=root)
        assert len(shelf["boxes"]) == 1 and shelf["source"]["skipped"] == 5, shelf["source"]
        assert shelf["source"]["cells"] == len(shelf_cells) - 1, shelf["source"]       # only the floor box's cell

        # The router can reach every station from the spawn, and the scripted baseline walks a
        # 1 m pick to the nearest rack face within 50 steps on the imported map.
        env = K1WarehouseEnv(world=world, n_workers=(0, 0), n_boxes=(0, 0))
        obs = _rollout(env, 50, seed=0)
        assert np.isfinite(obs).all()
        sx, sy = env.scenario["episode"]["start"][:2]
        for st in world["stations"]:
            cells = env.grid.path((sx, sy), st, n=10 ** 6)
            assert cells and cells[-1] == env.grid.nearest_free(env.grid.cell(st)), st
        st = min(world["stations"], key=lambda p: math.dist(p, (1.1 + ox, 0.4 + oy)))    # rack A, +y face
        scn = env.make_scenario(0)
        scn["episode"].update(start=[st[0] + 1.0, st[1], math.pi], goals=[st], forklift=None, boxes=[], workers=[])
        scn["episode"]["link"].update(drop_p=0.0, burst_p=0.0)
        o, _ = env.reset(options={"scenario": validate_scenario(scn)})
        info = {}
        for _ in range(50):
            o, _, term, trunc, info = env.step(heuristic(o))
            if term or trunc:
                break
        assert info.get("success"), ("baseline did not reach the station", info, env._pose())

        # 4. Local Map export: imported world goes back to its own map frame (the original wall
        #    cells are all there), refuses to overwrite, registers like the viewer does.
        world_to_localmap(world, "fixture-out", data_root=root)
        occ = _read(root / "domains/fixture-out/occupancy.json")
        out_cells = {(round(c["x"] / EXPORT_RES), round(c["y"] / EXPORT_RES)) for c in occ["cells"]}
        assert map_cells - {(10, 4)} <= out_cells, sorted(map_cells - out_cells)[:5]        # minus the box's cell
        try:
            world_to_localmap(world, "fixture-out", data_root=root)
            raise AssertionError("overwrote without overwrite=True")
        except FileExistsError:
            pass
        world_to_localmap(world, "fixture-out", data_root=root, overwrite=True)
        try:                                # a robot-mapped domain is never replaced by sim geometry
            world_to_localmap(world, "fixture", data_root=root, overwrite=True)
            raise AssertionError("overwrote a robot-mapped domain")
        except FileExistsError:
            pass
        assert _read(root / "domains/fixture/manifest.json") == {"id": "fixture", "name": "Fixture site", "runs": []}
        reg = _read(root / "domains.json")  # the robot's next mapping run must not land in the sim domain
        assert reg["active"] == "fixture" and [d["id"] for d in reg["domains"]] == ["fixture", "fixture-out"]
        assert reg["domains"][1]["cell_count"] == len(occ["cells"])
        for did in ("../evil", "a/b", "", "-x", "x" * 65, "ok\n", None, "a b", "..", "C:"):
            try:
                world_to_localmap(world, did, data_root=root)
                raise AssertionError(f"accepted domain id {did!r}")
            except ValueError:
                pass
        # The import's invented perimeter walls are not exported as mapped walls: a 2nd cycle keeps the size.
        assert np.allclose(localmap_to_world("fixture-out", data_root=root)["size"], world["size"])
        (root / "domains/robot").mkdir()    # robot data in a folder with no manifest: never overwritten
        (root / "domains/robot/occupancy.json").write_text('{"cells": []}')
        for ow in (False, True):
            try:
                world_to_localmap(world, "robot", data_root=root, overwrite=ow)
                raise AssertionError("overwrote a robot domain that has no manifest")
            except FileExistsError:
                pass
        assert (root / "domains/robot/occupancy.json").read_text() == '{"cells": []}'
        from concurrent.futures import ThreadPoolExecutor   # concurrent exports: no lost registry entry
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda i: world_to_localmap(world, f"par{i}", data_root=root), range(8)))
        assert {f"par{i}" for i in range(8)} <= {d["id"] for d in _read(root / "domains.json")["domains"]}
        if os.path.normcase("A") == "a":    # case-insensitive FS: 'X' is the existing 'x', one registry entry
            world_to_localmap(world, "x", data_root=root)
            world_to_localmap(world, "X", data_root=root, overwrite=True)
            assert [d["id"] for d in _read(root / "domains.json")["domains"] if d["id"] in ("x", "X")] == ["x"]

        # Round trip of a generated warehouse: same racks/pallets, occupancy on the same cells.
        gen = generate_world(np.random.default_rng(4))
        world_to_localmap(gen, "gen", data_root=root)
        back = localmap_to_world("gen", data_root=root)
        for k in ("rack", "pallet"):
            assert sum(s["kind"] == k for s in gen["statics"]) == sum(s["kind"] == k for s in back["statics"]), k
        a = _raster(gen["statics"], EXPORT_RES)
        bx, by = back["source"]["map_offset"]
        assert _raster([s for s in back["statics"] if s["kind"] == "obstacle"], EXPORT_RES, bx, by) == a
        racks = sorted((round(s["pos"][0] - bx, 6), round(s["pos"][1] - by, 6)) for s in back["statics"] if s["kind"] == "rack")
        assert racks == sorted((round(s["pos"][0], 6), round(s["pos"][1], 6)) for s in gen["statics"] if s["kind"] == "rack")
        K1WarehouseEnv(world=back).reset(seed=1)                  # and it runs
        assert _raster(gen["statics"], EXPORT_RES, size=gen["size"]) == a     # the export's clip drops nothing real

        # Low statics (no occupancy: under PLAN_MIN_H) come back as instances, and the sim's own
        # boxes keep their exact size and mass (5 small parts boxes 2 cm apart, a 200 kg crate).
        tiny = {"name": "tiny", "size": [8.0, 6.0], "stations": [], "spawn": [0.5, 0.5, 1.5, 1.5],
                "statics": [_static("rack", [4, 4.5, 1], [1, 0.4, 1]), _static("obstacle", [2, 3, 0.1], [0.3, 0.3, 0.1]),
                            _static("wall", [6, 3.5, 0.12], [0.05, 0.5, 0.12])],
                "boxes": [{"pos": [2.0 + 0.06 * k, 1.0], "half": 0.02, "mass": 0.5} for k in range(5)]
                + [{"pos": [5.5, 1.5], "half": 0.8, "mass": 200.0}]}
        world_to_localmap(tiny, "tiny", data_root=root)
        back = localmap_to_world("tiny", data_root=root)

        def low(w):
            return sorted((s["kind"], round(s["pos"][2] + s["half"][2], 9)) for s in w["statics"]
                          if s["pos"][2] + s["half"][2] < PLAN_MIN_H)
        assert low(back) == low(tiny) == [("obstacle", 0.2), ("wall", 0.24)], low(back)
        assert sorted((b["half"], b["mass"]) for b in back["boxes"]) == sorted((b["half"], b["mass"]) for b in tiny["boxes"])
        # A 500 m diagonal wall beside a 2 m site: only cells near the site (not a 2830^2 meshgrid each).
        diag = {"name": "diag", "size": [2, 2], "stations": [], "spawn": [0.2, 0.2, 1.8, 1.8],
                "statics": [_static("wall", [-10, 6, 1], [500, 0.001, 1], math.pi / 4)] * 10}
        cells = _read(world_to_localmap(diag, "diag", data_root=root) / "occupancy.json")["cells"]
        assert cells and all(-PAD - 1 <= c[k] <= 2 + PAD + 1 for c in cells for k in "xy"), len(cells)

        # 5. MJCF bundle of a dressed scene: only the mesh and texture files in use (the visual
        #    layer's own meshes inline), reloads the same, export does not perturb the episode.
        env = K1WarehouseEnv(task="follow", visuals=True)
        ref = _rollout(env, 20, seed=2)
        o, _ = env.reset(seed=2)
        seq, xpos0 = [o], env.data.xpos.copy()
        for k in range(20):
            if k == 10:
                path = export_mjcf(env, root / "mjcf")
                blob = export_mjcf_zip(env)
            seq.append(env.step(heuristic(seq[-1]))[0])
        assert np.array_equal(ref, np.array(seq)), "export perturbed the episode"
        files = {t: sorted({pathlib.Path(x.file).name for x in getattr(env.mj_spec, t) if x.file})
                 for t in ("meshes", "textures")}
        assert files["textures"] and len(files["meshes"]) < env.model.nmesh, files  # PNGs + inline user meshes
        for t, names in files.items():
            assert sorted(p.name for p in (root / "mjcf" / t).iterdir()) == names, t
        assert "meshdir=\".\"" in path.read_text() and "texturedir=\".\"" in path.read_text()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            assert sorted(n for n in z.namelist() if not n.endswith("/")) == \
                sorted(["scene.xml"] + [f"{t}/{n}" for t in ("meshes", "textures") for n in files[t]]), z.namelist()
            z.extractall(root / "unzipped")
        xml = (root / "unzipped/scene.xml").read_text()
        assert ".asset_cache" not in xml and str(REPO) not in xml and REPO.as_posix() not in xml, "absolute path in the bundle"
        m = mujoco.MjModel.from_xml_path(str(root / "unzipped/scene.xml"))
        em = env.model
        assert (m.nbody, m.ngeom, m.nmesh, m.ntex) == (em.nbody, em.ngeom, em.nmesh, em.ntex)
        assert np.array_equal(m.geom_group, em.geom_group) and np.array_equal(m.tex_data, em.tex_data)
        # Keyframe "start" is the episode's start state: every body where reset() put it (to the
        # ~1e-7 m that to_xml's number formatting keeps of the K1 link offsets).
        d = mujoco.MjData(m)
        mujoco.mj_resetDataKeyframe(m, d, m.key("start").id)
        mujoco.mj_forward(m, d)
        assert env.forklift is not None and m.nmocap == 3, "want the forklift and a (dressed) worker in the export test"
        assert np.allclose(d.xpos, xpos0, rtol=0, atol=1e-6), np.abs(d.xpos - xpos0).max()
        # A cleared .asset_cache (git clean while web.py runs) does not break the export: the PNGs
        # come back from the compiled model. Simulated on copies, never on the real cache.
        cache2 = root / "cache2"
        cache2.mkdir()
        for t in env.mj_spec.textures:
            if t.file:
                t.file = str(shutil.copy(t.file, cache2))
        shutil.rmtree(cache2)
        m2 = mujoco.MjModel.from_xml_path(str(export_mjcf(env, root / "mjcf2")))
        assert np.array_equal(m2.tex_data, em.tex_data) and len(list(cache2.iterdir())) == len(files["textures"])

    assert (DEFAULT_DATA_ROOT / "domains.json").read_bytes() == repo_reg, "selftest touched the repo's Local Map data"
    print("SIM-IO-SELFTEST-OK")


def main():
    ap = argparse.ArgumentParser(description="K1 sim importers/exporters")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    p = sub.add_parser("list-domains")
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    p = sub.add_parser("import-localmap", help="Local Map domain -> scenario JSON")
    p.add_argument("domain")
    p.add_argument("--out", required=True)
    p.add_argument("--task", default="pick", choices=["pick", "follow"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    p = sub.add_parser("export-localmap", help="scenario's world -> Local Map domain")
    p.add_argument("scenario")
    p.add_argument("domain")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    p = sub.add_parser("export-mjcf", help="scenario -> scene.xml + meshes/ + textures/")
    p.add_argument("scenario")
    p.add_argument("out_dir")
    a = ap.parse_args()
    try:
        _cli(a)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise SystemExit(f"error: {e}") from None


def _cli(a):
    if a.cmd == "selftest":
        selftest()
    elif a.cmd == "list-domains":
        for d in list_localmap_domains(a.data_root):
            print(f"{d['id']:24s} {d['cells']:7d} cells {d['instances']:5d} instances  {d['name']}")
    elif a.cmd == "import-localmap":
        w = localmap_to_world(a.domain, a.data_root)
        save_scenario(K1WarehouseEnv(task=a.task, world=w).make_scenario(a.seed), a.out)
        print(f"wrote {a.out}: {w['size'][0]:.1f} x {w['size'][1]:.1f} m, {len(w['statics'])} statics, "
              f"{len(w['stations'])} stations, map_offset {w['source']['map_offset']}")
    elif a.cmd == "export-localmap":
        print("wrote", world_to_localmap(load_scenario(a.scenario)["world"], a.domain, a.data_root, a.overwrite))
    else:
        scn = load_scenario(a.scenario)
        env = K1WarehouseEnv(task=scn["task"], visuals=True)
        env.reset(options={"scenario": scn})
        print("wrote", export_mjcf(env, a.out_dir))


if __name__ == "__main__":
    main()
