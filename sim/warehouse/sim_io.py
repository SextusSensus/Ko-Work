"""Importers / exporters for the K1 warehouse sim: scenarios, Local Map sites, MJCF.

  scenario JSON  validate_scenario / validate_world (hostile-input safe: the web UI feeds uploads
                 straight in), save_scenario / load_scenario
  Local Map      localmap_to_world: a robot-mapped site (desktop/localmap-data/domains/<id>) -> sim
                 world; world_to_localmap: the inverse, so a sim world opens in the Local Map viewer
  MJCF           export_mjcf / export_mjcf_zip: the built scene as scene.xml + meshes/, loadable by
                 any MuJoCo with no repo checkout

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
import argparse, copy, io, json, math, numbers, os, pathlib, re, shutil, tempfile, time, zipfile

import mujoco
import numpy as np

from k1_warehouse import (GRID_RES, PHYS_DT, PLAN_MIN_H, REPO, SCENARIO_FORMAT, SCENARIO_VERSION, VX_MAX,
                          K1WarehouseEnv, _Grid, _static, _yaw_quat, generate_world, heuristic)

DEFAULT_DATA_ROOT = REPO / "desktop/localmap-data"

# ---------------------------------------------------------------------- validation
# Caps on everything a web upload can grow: no memory blowups, no absurd values.
KINDS = ("wall", "rack", "pallet", "obstacle")
MAX_STATICS, MAX_STATIONS, MAX_WORKERS, MAX_BOXES, MAX_GOALS = 20000, 5000, 50, 500, 100
MAX_SIZE_M = 500.0
# Longest t_max sample_episode can generate (3 x pick route / VX_MAX + 30) on a world we accept.
MAX_T = 3.0 * (MAX_GOALS + 1) * math.hypot(MAX_SIZE_M, MAX_SIZE_M) / VX_MAX + 30.0
# The planner grid (k1_warehouse._Grid) costs ~15 ns per grid cell x tall static to build
# (measured) and 8 B per cell for each of up to 257 cached distance fields. Without these a
# valid 500 m upload hangs the web UI's sim thread for minutes. ponytail: drop MAX_GRID_WORK
# once _Grid rasterizes each static over its own bounding box only.
MAX_GRID_CELLS, MAX_GRID_WORK = 1_000_000, 1e9
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
    return out


def validate_world(world):
    """Strict, normalized deep copy of a world dict (schema in k1_warehouse.py). ValueError if bad."""
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
    cells = math.ceil(L / GRID_RES) * math.ceil(W / GRID_RES)
    tall = sum(st["pos"][2] + st["half"][2] >= PLAN_MIN_H for st in statics)
    if cells > MAX_GRID_CELLS or cells * tall > MAX_GRID_WORK:
        raise ValueError(f"world: too big to plan on ({cells} grid cells x {tall} tall statics; limits "
                         f"{MAX_GRID_CELLS} cells, {MAX_GRID_WORK:.0e} cells x statics)")
    # Same check as sample_episode (a scenario replay skips it): the env's _free_point crashes on a full grid.
    if not len(_Grid({"size": [L, W], "statics": statics}).free_cells):
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
    if "source" in w:
        out["source"] = _source(w["source"])
    return out


def validate_scenario(scn):
    """Strict, normalized deep copy of a k1sim.scenario v1. Raises ValueError with what is wrong.
    Values pass through float()/int() unchanged, so a validated scenario replays bit for bit."""
    s = _obj(scn, "scenario", ("format", "version", "task", "seed", "world", "episode"))
    if s["format"] != SCENARIO_FORMAT:
        raise ValueError(f"scenario.format: expected {SCENARIO_FORMAT!r}")
    _int(s["version"], "scenario.version", SCENARIO_VERSION, SCENARIO_VERSION)
    task = _str(s["task"], "scenario.task", choices=("pick", "follow"))
    world = validate_world(s["world"])
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
          # unstable at PHYS_DT (yaw kv > ~100) or sway pushes the robot with zero command, which
          # would drive it well past the action_to_cmd clamps.
          "robot": {"payload": _num(rb["payload"], "episode.robot.payload", 0, 50),
                    "force": _num(rb["force"], "episode.robot.force", 1, 200),
                    "kv": [_num(v, "episode.robot.kv", 1, hi)
                           for v, hi in zip(_list(rb["kv"], "episode.robot.kv", 3, 3), (1000, 1000, 100))],
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
        ep["workers"].append({"pos": _xy(wk["pos"], what + ".pos", L, W), "speed": _num(wk["speed"], what + ".speed", 0, 5),
                              "yields": _bool(wk["yields"], what + ".yields"),
                              "role": _str(wk["role"], what + ".role", choices=("target", "worker"))})
    ep["goals"] = [_xy(g, f"episode.goals[{i}]", L, W)
                   for i, g in enumerate(_list(e["goals"], "episode.goals", 1 if task == "pick" else 0, MAX_GOALS))]
    ep["t_max"] = _num(e["t_max"], "episode.t_max", 0.1, MAX_T)
    return {"format": SCENARIO_FORMAT, "version": SCENARIO_VERSION, "task": task,
            "seed": _int(s["seed"], "scenario.seed", 0, 2 ** 63), "world": world, "episode": ep}


def save_scenario(scn, path):
    pathlib.Path(path).write_text(json.dumps(validate_scenario(scn), indent=1, allow_nan=False), encoding="utf-8")


def load_scenario(path):
    return validate_scenario(json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig")))


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
    """JSON object from disk, {} if the file is missing. utf-8-sig: PowerShell 5 writes a BOM."""
    path = pathlib.Path(path)
    if not path.is_file():
        return {}
    d = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(d, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return d


def _write(path, obj):
    """Write via a temp file + rename so a crash never leaves half a domains.json."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
    names = {d.get("id"): d.get("name") for d in _read(root / "domains.json").get("domains") or [] if isinstance(d, dict)}
    dirs = sorted(p.name for p in (root / "domains").glob("*") if p.is_dir())
    out = []
    for did in dict.fromkeys(list(names) + dirs):
        if not isinstance(did, str) or not DOMAIN_RE.fullmatch(did):
            continue
        d = root / "domains" / did
        try:
            n_cells, n_inst = len(_read(d / "occupancy.json").get("cells") or []), len(_read(d / "instances.json").get("instances") or [])
        except (OSError, ValueError):
            n_cells = n_inst = 0                            # corrupt file: import says why
        out.append({"id": did, "name": names.get(did) or did, "cells": n_cells, "instances": n_inst})
    return out


def _extent(hx, hy, yaw):
    """Axis-aligned half extents of a yawed box footprint."""
    c, s = abs(math.cos(yaw)), abs(math.sin(yaw))
    return c * hx + s * hy, s * hx + c * hy


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
                yaw = math.atan2(2 * (pm["qw"] * pm.get("qz", 0) + pm.get("qx", 0) * pm.get("qy", 0)),
                                 1 - 2 * (pm.get("qy", 0) ** 2 + pm.get("qz", 0) ** 2))
            yaw = _num(yaw or 0.0, "yaw", -ANG, ANG)
            scale = inst.get("scale_m") or row.get("scale_m")
            sx, sy = (inst["w"], inst["h"]) if inst.get("w") and inst.get("h") else (scale[0], scale[2])
            sx, sy = _num(sx, "w", 0.01, MAX_SIZE_M), _num(sy, "h", 0.01, MAX_SIZE_M)
            sz = _num(scale[1], "height", 0.01, 50) if scale else wall_height
        except (AttributeError, KeyError, TypeError, IndexError, ValueError):
            skipped += 1                    # unsized / unreadable instance
            continue
        kind = ("rack" if cid in RACK_CLASSES else "wall" if cid in WALL_CLASSES else "pallet" if cid == "pallet"
                else "box" if cid in BOX_MASS else "obstacle")
        h = min(max((sx + sy + sz) / 6, 0.05), 0.5)           # a loose box is a cube of this half size
        items.append((kind, x, y, yaw, sx, sy, sz, BOX_MASS.get(cid), h))
        if kind == "box":                   # the box replaces the cells the depth camera saw on it,
            for i in range(round((x - h) / res), round((x + h) / res) + 1):     # else MuJoCo ejects it
                for j in range(round((y - h) / res), round((y + h) / res) + 1):
                    cells.discard((i, j))

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

    statics = [_static("wall", [L / 2, -0.05, 1], [L / 2, 0.05, 1]), _static("wall", [L / 2, W + 0.05, 1], [L / 2, 0.05, 1]),
               _static("wall", [-0.05, W / 2, 1], [0.05, W / 2, 1]), _static("wall", [L + 0.05, W / 2, 1], [0.05, W / 2, 1])]
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


def _raster(statics, res, ox=0.0, oy=0.0):
    """Map-frame cells (i, j) whose centers (i*res, j*res) lie inside a planner-visible static."""
    out = set()
    for st in statics:
        (px, py, pz), (hx, hy, hz), yaw = st["pos"], st["half"], st["yaw"]
        if pz + hz < PLAN_MIN_H:
            continue
        hx, hy = max(hx, res / 2), max(hy, res / 2)               # a thin wall still leaves one cell
        ex, ey = _extent(hx, hy, yaw)
        mx, my = px - ox, py - oy
        I, J = np.meshgrid(np.arange(math.floor((mx - ex) / res), math.ceil((mx + ex) / res) + 1),
                           np.arange(math.floor((my - ey) / res), math.ceil((my + ey) / res) + 1), indexing="ij")
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
    if (ddir / "manifest.json").exists():
        if not overwrite:
            raise FileExistsError(f"Local Map domain {domain_id!r} already exists (overwrite=True replaces it)")
        old = _read(ddir / "manifest.json")             # K1Finder bumps run_count when it merges a robot run
        if old.get("source") != "k1sim" or old.get("run_count"):
            raise FileExistsError(f"refusing to overwrite robot-mapped Local Map domain {domain_id!r}")
    src = world.get("source") or {}
    ox, oy = src["map_offset"] if src.get("kind") == "localmap" and len(src.get("map_offset") or ()) == 2 else (0.0, 0.0)
    onto = _ontology(root)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    cells = [{"x": i * EXPORT_RES, "y": j * EXPORT_RES, "hits": 1} for i, j in sorted(_raster(world["statics"], EXPORT_RES, ox, oy))]
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
        if st["kind"] in ("rack", "pallet"):
            (px, py, _), (hx, hy, hz) = st["pos"], st["half"]
            inst("pallet_rack" if st["kind"] == "rack" else "pallet", px - ox, py - oy, st["yaw"], 2 * hx, 2 * hy, 2 * hz)
    for b in world.get("boxes", []):
        inst("cardboard_box", b["pos"][0] - ox, b["pos"][1] - oy, 0.0, 2 * b["half"], 2 * b["half"], 2 * b["half"])

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
    reg = _read(root / "domains.json")
    doms = reg["domains"] = [d for d in reg.get("domains") or [] if isinstance(d, dict)]
    meta = {"id": domain_id, "name": man["name"], "updated": now, "run_count": man["run_count"], "cell_count": len(cells)}
    k = next((k for k, d in enumerate(doms) if d.get("id") == domain_id), None)
    if k is None:
        doms.append(meta)
    else:
        doms[k] = meta
    _write(root / "domains.json", reg)
    return ddir


# ---------------------------------------------------------------------- MJCF
def export_mjcf(env, out_dir):
    """The env's scene as a portable MJCF bundle: out_dir/scene.xml + out_dir/meshes/ (only the
    meshes it uses). This is the scene as BUILT at the last reset: keyframe "start" holds the
    episode's start state (robot pose, workers, forklift, loose boxes), not wherever the episode has
    moved them. A plain viewer opens at qpos0 (robot and actors at the origin) until that key is
    loaded. Verified to reload, and the key to place the robot, before returning."""
    import xml.etree.ElementTree as ET
    if env.spec is None:
        raise ValueError("env has no scene yet: call env.reset() first")
    out = pathlib.Path(out_dir)
    (out / "meshes").mkdir(parents=True, exist_ok=True)
    root = ET.fromstring(env.spec.to_xml())
    comp = root.find("compiler")
    meshdir = pathlib.Path(comp.get("meshdir", "") if comp is not None else "")
    seen = {}
    for m in root.iter("mesh"):
        if m.get("file"):
            src = meshdir / m.get("file")
            if seen.setdefault(src.name, src) != src:
                raise ValueError(f"two meshes named {src.name} from different folders")
            shutil.copyfile(src, out / "meshes" / src.name)
            m.set("file", "meshes/" + src.name)
    if comp is not None:
        comp.set("meshdir", ".")                                  # relative to scene.xml

    # The spec only has build-time poses (robot and actors at the origin); reset() places them in
    # env.data, which has moved on since. So the start state goes in as a keyframe, from the scenario.
    ep, em = env.scenario["episode"], env.model
    qpos = em.qpos0.copy()                                        # loose boxes: qpos0 is their start pose
    qpos[:3] = ep["start"]
    mpos, mquat = np.zeros((em.nmocap, 3)), np.tile([1.0, 0.0, 0.0, 0.0], (em.nmocap, 1))
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
    if (m.nbody, m.ngeom) != (em.nbody, em.ngeom):
        raise RuntimeError(f"exported scene reloads as {m.nbody} bodies / {m.ngeom} geoms, "
                           f"expected {em.nbody} / {em.ngeom}")
    d = mujoco.MjData(m)
    mujoco.mj_resetDataKeyframe(m, d, m.key("start").id)
    mujoco.mj_forward(m, d)
    if not np.allclose(d.xpos[m.body("k1").id][:2], ep["start"][:2]):
        raise RuntimeError(f"exported keyframe puts the robot at {d.xpos[m.body('k1').id][:2]}, "
                           f"expected {ep['start'][:2]}")
    return path


def export_mjcf_zip(env):
    """export_mjcf as zip bytes (scene.xml + meshes/ at the archive root)."""
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
        "grid work": lambda s: s["world"].update(size=[150, 150], statics=[s["world"]["statics"][0]] * 2000),
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
    }
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

        # 5. MJCF bundle: only the meshes in use, reloads the same, export does not perturb the episode.
        env = K1WarehouseEnv(task="follow")
        ref = _rollout(env, 20, seed=2)
        o, _ = env.reset(seed=2)
        seq, xpos0 = [o], env.data.xpos.copy()
        for k in range(20):
            if k == 10:
                path = export_mjcf(env, root / "mjcf")
                blob = export_mjcf_zip(env)
            seq.append(env.step(heuristic(seq[-1]))[0])
        assert np.array_equal(ref, np.array(seq)), "export perturbed the episode"
        meshes = list((root / "mjcf/meshes").iterdir())
        assert len(meshes) == env.model.nmesh and "meshdir=\".\"" in path.read_text(), len(meshes)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            assert "scene.xml" in z.namelist() and len([n for n in z.namelist() if not n.endswith("/")]) == 1 + env.model.nmesh
            z.extractall(root / "unzipped")
        m = mujoco.MjModel.from_xml_path(str(root / "unzipped/scene.xml"))
        assert (m.nbody, m.ngeom, m.nmesh) == (env.model.nbody, env.model.ngeom, env.model.nmesh)
        # Keyframe "start" is the episode's start state: every body where reset() put it (to the
        # ~1e-7 m that to_xml's number formatting keeps of the K1 link offsets).
        d = mujoco.MjData(m)
        mujoco.mj_resetDataKeyframe(m, d, m.key("start").id)
        mujoco.mj_forward(m, d)
        assert env.forklift is not None and m.nmocap == 2, "want the forklift and a worker in the export test"
        assert np.allclose(d.xpos, xpos0, rtol=0, atol=1e-6), np.abs(d.xpos - xpos0).max()

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
    p = sub.add_parser("export-mjcf", help="scenario -> scene.xml + meshes/")
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
        env = K1WarehouseEnv(task=scn["task"])
        env.reset(options={"scenario": scn})
        print("wrote", export_mjcf(env, a.out_dir))


if __name__ == "__main__":
    main()
