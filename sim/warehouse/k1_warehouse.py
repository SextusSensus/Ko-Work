"""Booster K1 warehouse training simulation (MuJoCo + gymnasium).

The K1 is commanded exactly like the real robot: a (vx, vyaw) velocity stream into
Booster's onboard loco controller, through the SAME hard clamps and staleness
watchdog as robot/loco_follow_bridge.cpp. The gait itself is abstracted (the real
stack never touches it either): the official K1 URDF rides a planar x/y/yaw base
driven by force-limited velocity actuators, so it still gets physically blocked by
racks, shoves loose boxes, and slows under a carried tote.

Every reset re-randomizes (domain randomization for sim-to-real robustness):
  layout   rack rows, bay gaps (cross aisles), aisle width, rack depth/height,
           pallets sticking out at floor level (below the upper scan)
  actors   walking workers (some do not yield), dropped boxes of random mass,
           a forklift patrolling an end lane that never yields
  robot    tote payload, actuator force/gain, accel limit, loco lag, gait-start delay,
           gait sway, trip sensitivity
  link     command latency + jitter, random drops, burst dropouts (camera stalls)
  sensing  depth scan with range-growing long bias (c ~ 0.1 1/m), noise, dropouts;
           person detector with FOV gate, occlusion, misses, range glitches and
           ReID identity swaps when people cross; noisy odometry

Failure modes taken from the robot's own incident history:
  fall     foot-level contact at speed, a velocity lunge, or fast yaw while walking
           can trip the K1 (docs/HARDENING_2026-07.md); the forklift always does.
           A fall ends the episode as a failure.
  prepare  after the bridge's STALE_PREP tier the loco is in kPrepare until commands resume,
           then needs 1.5-3 s to walk again -- a stalled command stream costs real time.
  reacq    the follow target is only known through the detector: behind a rack it
           is gone, and a crossing worker can steal the lock (S_REACQUIRE class).

Tasks:  pick   -- visit N pick stations in the aisles, then return to the dock
        follow -- follow a picker through the aisles, hold a 1.0-2.0 m standoff

Usage:  python k1_warehouse.py selftest      -> SIM-SELFTEST-OK
        python k1_warehouse.py view [--task follow] [--model runs/x/model.zip]
"""
import argparse, math, pathlib, sys, time

import gymnasium as gym
import mujoco
import numpy as np

import assets

REPO = pathlib.Path(__file__).resolve().parents[2]
K1_DIR = REPO / "desktop/localmap-viewer/assets/library/robot/k1"

# Mirrors robot/loco_follow_bridge.cpp. Duplicated ON PURPOSE (defense-in-depth, CLAUDE.md
# invariant): a policy trained here must never learn to rely on speeds the robot refuses.
VX_MIN, VX_MAX = -0.10, 0.30
VYAW_MIN, VYAW_MAX = -0.40, 0.40
STALE_S, STALE_PREP_S = 0.400, 1.000

CTRL_DT, PHYS_DT = 0.1, 0.005          # 10 Hz policy (the follow loop's rate), 200 Hz physics
N_RAYS, RAY_FOV, RAY_MAX = 16, math.radians(90), 6.0
ENV_GROUPS = np.array([1, 1, 0, 0, 0, 0], np.uint8)   # rays see racks/walls (0) + boxes/people (1)
RACK_GROUPS = np.array([1, 0, 0, 0, 0, 0], np.uint8)  # route planner sees static structure only
REACH_M, BAND = 0.5, (1.0, 2.0)
# Observation layout. sub/goal = range + sin/cos bearing to the planner subgoal and the goal
# (for follow: the BELIEVED target, i.e. last detection). Then body odometry, previous action,
# watchdog-stale flag, standing (kPrepare) flag, nearest-person range + bearing, target-seen
# flag, seconds since last seen, then the low (0.10 m) and high (0.8 m) depth scans.
I_SUB, I_GOAL, I_ODO, I_APREV, I_STALE, I_STAND, I_PR, I_PB, I_SEEN, I_SINCE, I_SCAN = \
    0, 3, 6, 8, 10, 11, 12, 13, 14, 15, 16
OBS_DIM = I_SCAN + 2 * N_RAYS


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def action_to_cmd(a):
    """Policy action in [-1,1]^2 -> clamped (vx, vyaw). vy is identically 0 (TRAIN_CONTRACT)."""
    a = np.clip(np.nan_to_num(np.asarray(a, np.float64)), -1, 1)
    if a.shape != (2,):       # before step() touches any state; a bridge-style [vx, vy, vyaw] is not this
        raise ValueError(f"action must have shape (2,) [vx, vyaw], got {a.shape}")
    vx = a[0] * (VX_MAX if a[0] >= 0 else -VX_MIN)
    return float(np.clip(vx, VX_MIN, VX_MAX)), float(np.clip(a[1] * VYAW_MAX, VYAW_MIN, VYAW_MAX))


def _k1_spec():
    txt = (K1_DIR / "K1_22dof.urdf").read_text()
    i = txt.index(">", txt.index("<robot")) + 1
    ext = (f'<mujoco><compiler meshdir="{K1_DIR.as_posix()}" balanceinertia="true" '
           'discardvisual="false" strippath="false"/></mujoco>')
    spec = mujoco.MjSpec.from_string(txt[:i] + ext + txt[i:])
    # ponytail: rigid stand pose -- the onboard loco controller owns the legs on the real
    # robot too. Whole-body gait RL is a different sim (booster_gym), not this one.
    # URDF zero pose is arms-out (0.82 m wide); bake arms-down into the link frames first.
    stand = {"left_shoulder_roll_joint": -1.3, "right_shoulder_roll_joint": 1.3}
    for j in list(spec.joints):
        if j.name in stand:
            rot, q = np.zeros(4), np.zeros(4)
            mujoco.mju_axisAngle2Quat(rot, np.array(j.axis, float), stand[j.name])
            mujoco.mju_mulQuat(q, np.array(j.parent.quat, float), rot)
            j.parent.quat = q
        spec.delete(j)
    for g in spec.geoms:
        g.group = 2                                   # visible, invisible to the depth rays
        if g.contype:
            g.contype, g.conaffinity = 2, 1           # hits the world, never itself
    return spec


# ---------------------------------------------------------------------- scenarios
# A scenario is plain JSON-able data that fully determines an episode:
#   {"format": "k1sim.scenario", "version": 1, "task", "seed", "world": {...}, "episode": {...}}
# world   = static map: size, statics (oriented boxes), pick stations, spawn rect, optional
#           forklift lane, provenance. Generated here or imported (sim_io.py: Local Map, JSON).
# episode = everything randomized per run ON a world: robot/link/sensing params, actors,
#           loose boxes, start pose, goals, time limit.
# seed    = runtime noise seed (sensor noise, drops, worker choices) -- same scenario + same
#           actions = same episode, bit for bit.
SCENARIO_FORMAT, SCENARIO_VERSION = "k1sim.scenario", 1
GRID_RES, INFLATE = 0.2, 0.35           # router grid (m) and robot clearance radius (m)
PLAN_MIN_H = 0.3                        # statics lower than this are the policy's problem, not the planner's
PLAN_CLEAR_H = 1.3                      # underside above this = overhead (beam, mezzanine): the K1 + tote walk under
# Planner line of sight: rays across the robot-wide corridor, at every height the grid plans for.
# ponytail: 0.05 m apart, so a static narrower than that (validate_world allows half 1e-3) can still
# slip between two rays; the upgrade is checking the inflated grid along the segment instead.
CLEAR_OFFSETS, CLEAR_Z = np.linspace(-INFLATE, INFLATE, 15), (PLAN_MIN_H, 1.0)
START_KEEP = 1.0                        # generated loose boxes / other people start this far from the robot
# = sim_io.MAX_WORKERS / MAX_GOALS / MAX_T (selftest checks). sim_io imports this module, not the reverse.
MAX_WORKERS, MAX_GOALS = 50, 100
T_MAX_CAP = 3.0 * (MAX_GOALS + 1) * math.hypot(500.0, 500.0) / VX_MAX + 30.0
FIELD_CACHE_MIN = 64                    # distance fields kept: > MAX_WORKERS goals + the robot's own
STATIC_RGBA = {"wall": [0.45, 0.45, 0.5, 1], "rack": [0.15, 0.3, 0.65, 1], "pallet": [0.6, 0.45, 0.2, 1],
               "obstacle": [0.3, 0.3, 0.32, 1]}


def _static(kind, pos, half, yaw=0.0):
    return {"kind": kind, "pos": [float(v) for v in pos], "half": [float(v) for v in half], "yaw": float(yaw)}


def _plan_visible(st):
    """The planner sees this static: top at least PLAN_MIN_H, underside below PLAN_CLEAR_H."""
    return st["pos"][2] + st["half"][2] >= PLAN_MIN_H and st["pos"][2] - st["half"][2] < PLAN_CLEAR_H


def generate_world(rng, randomize=True):
    """Random warehouse: rack rows along x, missing bays (cross aisles), pallets in the aisles."""
    def u(lo, hi):
        return float(rng.uniform(lo, hi)) if randomize else (lo + hi) / 2
    n_rows = int(rng.integers(3, 5)) if randomize else 3
    aisle, depth, height = u(1.3, 2.0), u(0.6, 1.0), u(1.8, 2.4)
    n_bays, bay = int(rng.integers(6, 9)) if randomize else 7, 1.2
    x0 = 3.0
    x1 = x0 + n_bays * bay
    L, W = x1 + 3.0, n_rows * (depth + aisle) + aisle
    statics = [_static("wall", [L / 2, -0.05, 1], [L / 2, 0.05, 1]), _static("wall", [L / 2, W + 0.05, 1], [L / 2, 0.05, 1]),
               _static("wall", [-0.05, W / 2, 1], [0.05, W / 2, 1]), _static("wall", [L + 0.05, W / 2, 1], [0.05, W / 2, 1])]
    stations, aisles_y = [], []
    y = aisle
    for _ in range(n_rows):
        aisles_y.append(y - aisle / 2)
        for b in range(n_bays):
            if randomize and rng.random() < 0.15:
                continue
            cx = x0 + (b + 0.5) * bay
            statics.append(_static("rack", [cx, y + depth / 2, height / 2], [bay / 2 - 0.02, depth / 2, height / 2]))
            stations += [[cx, y - 0.45], [cx, y + depth + 0.45]]
            # Pallet left sticking out of the bay at floor level: only the LOW scan sees it.
            if randomize and rng.random() < 0.2:
                side = float(rng.choice([-1, 1]))
                statics.append(_static("pallet", [cx, y + depth / 2 + side * (depth / 2 + 0.05), 0.07], [0.5, 0.25, 0.07]))
        y += depth + aisle
    aisles_y.append(y - aisle / 2)
    return {"name": "generated warehouse", "size": [L, W], "statics": statics, "stations": stations,
            "spawn": [0.8, 0.8, 2.0, W - 0.8],
            # Forklift lane: the far-wall lane, 0.65 m from the pedestrian corner the router uses.
            "forklift_lane": [[L - 0.7, 0.8], [L - 0.7, W - 0.8]],
            "hints": {"x0": x0, "x1": x1, "aisles_y": aisles_y},
            "source": {"kind": "generated"}}


class _Grid:
    """Occupancy grid of a world's tall statics + geodesic distance fields (the Nav2-ish layer)."""

    def __init__(self, world):
        from scipy.sparse import coo_matrix
        L, W = world["size"]
        self.nx, self.ny = max(1, int(math.ceil(L / GRID_RES))), max(1, int(math.ceil(W / GRID_RES)))
        blocked = np.zeros((self.nx, self.ny), bool)
        for st in world["statics"]:
            if not _plan_visible(st):
                continue
            (px, py, _), (hx, hy, _) = st["pos"], st["half"]
            # Only the cells under the static's (inflated) bounding circle: O(footprint), not O(grid).
            r = math.hypot(hx + INFLATE, hy + INFLATE)
            i0, i1 = max(0, int((px - r) / GRID_RES)), min(self.nx, int((px + r) / GRID_RES) + 1)
            j0, j1 = max(0, int((py - r) / GRID_RES)), min(self.ny, int((py + r) / GRID_RES) + 1)
            if i0 >= i1 or j0 >= j1:
                continue
            X, Y = np.meshgrid((np.arange(i0, i1) + 0.5) * GRID_RES, (np.arange(j0, j1) + 0.5) * GRID_RES, indexing="ij")
            c, s_ = math.cos(st["yaw"]), math.sin(st["yaw"])
            dx, dy = X - px, Y - py
            blocked[i0:i1, j0:j1] |= (np.abs(c * dx + s_ * dy) <= hx + INFLATE) & (np.abs(-s_ * dx + c * dy) <= hy + INFLATE)
        self.blocked = blocked
        free = ~blocked
        nx, ny = self.nx, self.ny
        idx = np.arange(nx * ny).reshape(nx, ny)
        rows, cols, wts = [], [], []
        # 8-connected edges between free cells; a diagonal also needs both orthogonal cells free
        # (no corner cutting past an obstacle).
        for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
            i0, i1 = 0, nx - di
            j0, j1 = max(0, -dj), ny - max(0, dj)
            a = free[i0:i1, j0:j1]
            b = free[i0 + di:i1 + di, j0 + dj:j1 + dj]
            ok = a & b
            if di and dj:
                ok &= free[i0 + di:i1 + di, j0:j1] & free[i0:i1, j0 + dj:j1 + dj]
            ia = idx[i0:i1, j0:j1][ok]
            rows.append(ia)
            cols.append(ia + di * ny + dj)
            wts.append(np.full(ia.size, math.hypot(di, dj) * GRID_RES))
        n = nx * ny
        self.graph = coo_matrix((np.concatenate(wts), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)).tocsr()
        self.fields = {}
        fi, fj = np.nonzero(free)
        self.free_cells = np.stack([fi, fj], 1)

    def cell(self, p):
        return (min(max(int(p[0] / GRID_RES), 0), self.nx - 1), min(max(int(p[1] / GRID_RES), 0), self.ny - 1))

    def center(self, c):
        return ((int(c[0]) + 0.5) * GRID_RES, (int(c[1]) + 0.5) * GRID_RES)

    def nearest_free(self, c, r=6):
        if not self.blocked[c]:
            return c
        i0, j0 = max(0, c[0] - r), max(0, c[1] - r)
        win = ~self.blocked[i0:c[0] + r + 1, j0:c[1] + r + 1]
        if not win.any():
            return None
        fi, fj = np.nonzero(win)
        k = int(np.argmin((fi + i0 - c[0]) ** 2 + (fj + j0 - c[1]) ** 2))
        return (int(fi[k] + i0), int(fj[k] + j0))

    def field(self, goal):
        """Geodesic distance (m) from every cell to the goal's cell; cached per goal cell."""
        from scipy.sparse.csgraph import dijkstra
        gc = self.nearest_free(self.cell(goal))
        if gc is None:
            return None, None
        # LRU (dict order, re-inserted on every use) holding at least every live worker goal: a
        # clear-all here recomputed each field every step once the goals outnumbered the cap.
        # Worst case FIELD_CACHE_MIN x 8 MB at sim_io.MAX_GRID_CELLS.
        f = self.fields.pop(gc, None)
        if f is None:
            if len(self.fields) >= max(FIELD_CACHE_MIN, int(256e6 // (8 * self.nx * self.ny))):
                self.fields.pop(next(iter(self.fields)))
            f = dijkstra(self.graph, directed=False, indices=gc[0] * self.ny + gc[1]).reshape(self.nx, self.ny)
        self.fields[gc] = f
        return f, gc

    def path(self, p, goal, n=15):
        """Up to n cells of steepest descent from p toward goal, or None if unreachable."""
        f, gc = self.field(goal)
        c = self.nearest_free(self.cell(p))
        if f is None or c is None or not np.isfinite(f[c]):
            return None
        cells = [c]
        while len(cells) < n and c != gc:
            i0, j0 = max(0, c[0] - 1), max(0, c[1] - 1)
            win = f[i0:c[0] + 2, j0:c[1] + 2]
            k = np.unravel_index(int(np.argmin(win)), win.shape)
            nxt = (int(k[0] + i0), int(k[1] + j0))
            if not f[nxt] < f[c]:
                break
            c = nxt
            cells.append(c)
        return cells


def _reachable(grid, start):
    """(free cells connected to start, predicate: point p's nearest free cell is one of them).
    Generated maps are one connected floor, so this is every free cell, in the same order."""
    f, _ = grid.field(start)
    if f is None:
        return grid.free_cells, lambda p: True
    fc = grid.free_cells

    def ok(p):
        c = grid.nearest_free(grid.cell(p))
        return c is not None and bool(np.isfinite(f[c]))
    return fc[np.isfinite(f[fc[:, 0], fc[:, 1]])], ok


def sample_episode(world, rng, task="pick", n_picks=2, n_workers=(1, 3), n_boxes=(0, 6), randomize=True, grid=None):
    """Randomize everything that is not the static map: robot, link, sensing, actors, goals."""
    def u(lo, hi):
        return float(rng.uniform(lo, hi)) if randomize else (lo + hi) / 2
    grid = grid or _Grid(world)
    if not len(grid.free_cells):
        raise ValueError("world has no free floor for the robot")
    # The planner grid holds tall statics only. Spawns also have to clear what it leaves out:
    # low statics (pallets) and loose boxes. Overlap at t=0 = MuJoCo ejects the body at m/s.
    lows = [st for st in world["statics"] if st["pos"][2] + st["half"][2] < PLAN_MIN_H]

    def floor_clear(p, r, boxes):
        """p is more than r outside every low static's and loose box's footprint."""
        for st in lows:
            (px, py, _), (hx, hy, _), c, s_ = st["pos"], st["half"], math.cos(st["yaw"]), math.sin(st["yaw"])
            dx, dy = p[0] - px, p[1] - py
            if abs(c * dx + s_ * dy) <= hx + r and abs(-s_ * dx + c * dy) <= hy + r:
                return False
        return all(max(abs(p[0] - b["pos"][0]), abs(p[1] - b["pos"][1])) > b["half"] + r for b in boxes)

    def start_ok(p):
        return not grid.blocked[grid.cell(p)] and floor_clear(p, INFLATE, world.get("boxes", []))

    reach = grid.free_cells                 # until the start is known: then only floor it connects to

    def free_point():
        return [float(v) for v in grid.center(reach[rng.integers(len(reach))])]

    def nudge(p, ok):
        """p, or the reachable cell centre nearest it that passes ok. No rng: a seed whose actors
        never conflict keeps its exact episode, and a conflict moves only that actor."""
        if ok(p):
            return p
        d = np.hypot((reach[:, 0] + 0.5) * GRID_RES - p[0], (reach[:, 1] + 0.5) * GRID_RES - p[1])
        for i in np.argsort(d, kind="stable"):
            q = [float(v) for v in grid.center(reach[i])]
            if ok(q):
                return q
        return p                            # ponytail: a map with no clear cell keeps the draw

    def free_in_rect(rect, ok, tries=60):
        for _ in range(tries):
            p = [u(rect[0], rect[2]), u(rect[1], rect[3])]
            if ok(p):
                return p
        for i in rng.permutation(len(reach)):          # spawn rect full: any clear cell, random order
            p = [float(v) for v in grid.center(reach[i])]
            if ok(p):
                return p
        raise ValueError("world has no clear start for the robot")

    sx, sy = free_in_rect(world["spawn"], start_ok)
    reach, reachable = _reachable(grid, (sx, sy))
    syaw = u(-math.pi, math.pi)
    ep = {"start": [sx, sy, syaw], "floor_friction": u(0.5, 1.0),
          "robot": {"payload": u(0.0, 3.0), "force": u(40, 90), "kv": [u(150, 400), u(150, 400), u(20, 60)],
                    "accel": [u(0.3, 0.8), u(0.6, 1.5)], "tau": u(0.15, 0.40), "gait_start": u(0.3, 0.7),
                    "sway": u(0.0, 0.04), "trip_gain": u(0.5, 2.0)},
          "link": {"latency": u(0.02, 0.15), "drop_p": u(0.0, 0.08), "burst_p": u(0.0, 0.006)},
          "sensing": {"depth_c": u(0.0, 0.12)}}
    ep["boxes"] = []
    for _ in range(int(rng.integers(n_boxes[0], n_boxes[1] + 1))):
        ep["boxes"].append({"pos": free_point(), "half": u(0.1, 0.22), "mass": u(1, 8)})
    lane = world.get("forklift_lane")
    ep["forklift"] = None
    if lane and (not randomize or rng.random() < 0.5):
        near_end = math.dist((sx, sy), lane[1]) < math.dist((sx, sy), lane[0])
        ep["forklift"] = {"path": lane, "s": 0.0 if near_end else 1.0, "dir": 1.0 if near_end else -1.0,
                          "speed": u(0.8, 1.8)}
    nw = 1 if task == "follow" else int(rng.integers(n_workers[0], n_workers[1] + 1))
    if task == "follow":
        nw += int(rng.integers(n_workers[0], n_workers[1] + 1))       # distractors
    ep["workers"] = []
    for k in range(nw):
        w = {"pos": free_point(), "speed": u(0.4, 1.1), "yields": bool(rng.random() < 0.5), "role": "worker"}
        if k == 0 and task == "follow":
            pos = None
            for da in (0, 0.5, -0.5, 1.0, -1.0, 1.6, -1.6, math.pi):   # 1.5 m ahead, else nearby
                q = [sx + 1.5 * math.cos(syaw + da), sy + 1.5 * math.sin(syaw + da)]
                if 0 < q[0] < world["size"][0] and 0 < q[1] < world["size"][1] and not grid.blocked[grid.cell(q)] \
                        and reachable(q):
                    pos = q
                    break
            w.update(pos=pos or free_point(), speed=u(0.12, 0.22), yields=True, role="target")  # briefed: walk slowly
        ep["workers"].append(w)
    # Clearances (no rng, see nudge). A box overlapping anything at t=0 is ejected at m/s: keep it
    # off the robot, other boxes, pallets and the forklift's start. People keep off the robot (the
    # capsules never touch boxes: contype 0).
    fk = ep["forklift"] and [a + ep["forklift"]["s"] * (b - a) for a, b in zip(*ep["forklift"]["path"])]
    placed = list(world.get("boxes", []))
    for b in ep["boxes"]:
        r = b["half"] * math.sqrt(2) + 0.02
        b["pos"] = nudge(b["pos"], lambda p: math.dist(p, (sx, sy)) >= START_KEEP and floor_clear(p, r, placed)
                         and not (fk and math.dist(p, fk) < 0.8 + r))
        placed.append(b)
    for w in ep["workers"]:
        w["pos"] = nudge(w["pos"], lambda p: math.dist(p, (sx, sy)) >= START_KEEP)
    if task == "pick":
        st = [p for p in world["stations"] if reachable(p)] or [free_point() for _ in range(max(1, n_picks))]
        idx = rng.choice(len(st), size=min(n_picks, len(st)), replace=False)
        ep["goals"] = [[float(v) for v in st[i]] for i in idx] + \
            [free_in_rect(world["spawn"], lambda p: start_ok(p) and reachable(p))]

        def leg(a, b):    # along the router grid (a long rack is walked around), never under straight-line
            f, _ = grid.field(b)
            c = grid.nearest_free(grid.cell(a))
            d = float(f[c]) if f is not None and c is not None else math.inf
            return max(math.dist(a, b), d) if math.isfinite(d) else math.dist(a, b)
        path = sum(leg(a, b) for a, b in zip([(sx, sy)] + ep["goals"], ep["goals"]))
        ep["t_max"] = min(3.0 * path / VX_MAX + 30.0, T_MAX_CAP)
    else:
        ep["goals"] = []
        ep["t_max"] = u(60.0, 120.0)
    return ep


_LIFT = None


def _robot_lift():
    """Base height that puts the lowest K1 point 1 cm over the floor (computed once)."""
    global _LIFT
    if _LIFT is None:
        s = mujoco.MjSpec()
        s.attach(_k1_spec(), frame=s.worldbody.add_body(name="k1").add_frame(), prefix="k1/")
        m = s.compile()
        d = mujoco.MjData(m)
        mujoco.mj_forward(m, d)
        low = min(d.geom_xpos[g, 2] + d.geom_xmat[g].reshape(3, 3)[2] @ m.geom_aabb[g, :3]
                  - np.abs(d.geom_xmat[g].reshape(3, 3)[2]) @ m.geom_aabb[g, 3:] for g in range(m.ngeom))
        _LIFT = 0.01 - float(low)
    return _LIFT


def _yaw_quat(yaw):
    return [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]


def render_option(head=False, sensor=False):
    """Scene option for every renderer. Detailed (default): the visual layer (groups 3 and 4) and
    the robot (group 2). sensor=True: the proxies the depth rays, detector and planner actually see
    (groups 0/1) instead of the visual layer. head=True hides the robot's own body (head camera),
    and with it group 4, the visual parts that ride on the robot."""
    opt = mujoco.MjvOption()
    opt.geomgroup[:5] = [sensor, sensor, not head, not sensor, not (sensor or head)]
    return opt


class K1WarehouseEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, task="pick", n_picks=2, n_workers=None, n_boxes=(0, 6), randomize=True, world=None,
                 visuals=False):
        """world=None generates a new warehouse every reset; pass a world dict (e.g. an imported
        Local Map) to randomize episodes on that fixed map instead. visuals=True adds the
        render-only layer (assets.py) for UIs and exports; it never changes the episode.
        n_workers = (min, max) people besides the robot; for follow, distractors besides the
        target. Default (1, 3) for pick, (0, 3) for follow."""
        assert task in ("pick", "follow")
        n_workers = n_workers or ((0, 3) if task == "follow" else (1, 3))
        # Generated scenarios must pass sim_io.validate_scenario: follow adds the target to the
        # distractors, pick adds the return-to-dock goal to the stations.
        if n_workers[1] + (task == "follow") > MAX_WORKERS:
            raise ValueError(f"n_workers upper bound {n_workers[1]} yields more than {MAX_WORKERS} workers "
                             f"for task {task!r} (follow adds the target)")
        if not 0 <= n_picks < MAX_GOALS:
            raise ValueError(f"n_picks must be 0..{MAX_GOALS - 1} (the dock goal makes {MAX_GOALS})")
        # base_task: what seeded resets generate. self.task is the CURRENT episode's task (a replayed
        # scenario brings its own), so a replay never changes what reset(seed) builds afterwards.
        self.task = self.base_task = task
        self.n_picks, self.randomize, self.visuals = n_picks, randomize, visuals
        self.n_workers, self.n_boxes = n_workers, n_boxes
        self.fixed_world = world
        self._fixed_grid = _Grid(world) if world is not None else None
        self.action_space = gym.spaces.Box(-1, 1, (2,), np.float32)
        self.observation_space = gym.spaces.Box(-np.inf, np.inf, (OBS_DIM,), np.float32)
        self.model = self.data = self.mj_spec = self.scenario = None   # not .spec: that is gym's EnvSpec

    # ------------------------------------------------------------------ world build
    def _build(self, world, ep):
        L, W = world["size"]
        s = mujoco.MjSpec()
        s.modelname = "k1_warehouse"
        s.meshdir = K1_DIR.as_posix()           # survives to_xml(): the attached URDF meshes resolve
        s.option.timestep = PHYS_DT
        s.visual.global_.offwidth, s.visual.global_.offheight = 1280, 960
        s.worldbody.add_light(pos=[L / 2, W / 2, 6], dir=[0, 0, -1], diffuse=[0.8, 0.8, 0.8])
        s.worldbody.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[L, W, 0.1],
                             pos=[L / 2, W / 2, 0], rgba=[0.55, 0.55, 0.52, 1],
                             friction=[ep["floor_friction"], 0.005, 0.0001])

        # K1 on a planar base, built FIRST so its x/y/yaw joints are qpos[0:3].
        rb = ep["robot"]
        base = s.worldbody.add_body(name="k1", pos=[0, 0, _robot_lift()])
        for name, typ, ax in (("bx", mujoco.mjtJoint.mjJNT_SLIDE, [1, 0, 0]),
                              ("by", mujoco.mjtJoint.mjJNT_SLIDE, [0, 1, 0]),
                              ("byaw", mujoco.mjtJoint.mjJNT_HINGE, [0, 0, 1])):
            base.add_joint(name=name, type=typ, axis=ax, damping=1.0)
        base.add_geom(name="tote", type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.12, 0.15, 0.1], pos=[0.2, 0, 0.1],
                      mass=rb["payload"], rgba=[0.2, 0.6, 0.3, 1], group=2, contype=2, conaffinity=1)
        s.attach(_k1_spec(), frame=base.add_frame(), prefix="k1/")
        f = rb["force"]
        for name, kv, fl in (("bx", rb["kv"][0], f), ("by", rb["kv"][1], f), ("byaw", rb["kv"][2], f / 3)):
            s.add_actuator(name=name, target=name, trntype=mujoco.mjtTrn.mjTRN_JOINT, gaintype=mujoco.mjtGain.mjGAIN_FIXED,
                           biastype=mujoco.mjtBias.mjBIAS_AFFINE, gainprm=[kv] + [0] * 9,
                           biasprm=[0, 0, -kv] + [0] * 7, forcelimited=True, forcerange=[-fl, fl])

        for st in world["statics"]:
            s.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_BOX, pos=st["pos"], size=st["half"],
                                 quat=_yaw_quat(st["yaw"]), rgba=STATIC_RGBA.get(st["kind"], STATIC_RGBA["obstacle"]),
                                 group=0, contype=1, conaffinity=3)

        # Forklift: kinematic block shuttling along its lane; never yields; contact = fall.
        if ep["forklift"]:
            fb = s.worldbody.add_body(name="forklift", mocap=True, pos=[0, 0, 1.0])
            fb.add_geom(type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.6, 0.5, 1.0], rgba=[0.9, 0.6, 0.05, 1],
                        group=1, contype=1, conaffinity=2)

        for k, b in enumerate(list(world.get("boxes", [])) + ep["boxes"]):       # loose free bodies
            h = b["half"]
            body = s.worldbody.add_body(name=f"box{k}", pos=[b["pos"][0], b["pos"][1], h])
            body.add_freejoint()
            body.add_geom(type=mujoco.mjtGeom.mjGEOM_BOX, size=[h, h, h], mass=b["mass"],
                          rgba=[0.7, 0.5, 0.25, 1], group=1, contype=1, conaffinity=3)

        # Workers: mocap capsules (people are kinematic; they do not get pushed). contype 0: they
        # touch the robot (its contype 2) but step around cartons; a kinematic capsule walking
        # into a loose box would fling it at m/s.
        for k, w in enumerate(ep["workers"]):
            wb = s.worldbody.add_body(name=f"worker{k}", mocap=True, pos=[0, 0, 0.85])
            wb.add_geom(type=mujoco.mjtGeom.mjGEOM_CAPSULE, size=[0.22, 0.6],
                        rgba=[0.9, 0.35, 0.1, 1] if w["role"] == "target" else [0.95, 0.8, 0.1, 1],
                        group=1, contype=0, conaffinity=2)

        if self.visuals:
            assets.add_visuals(s, world, ep)   # render-only geoms on top of the proxies above
        m = s.compile()
        d = mujoco.MjData(m)
        k1 = m.body("k1").id
        assert m.jnt_qposadr[m.joint("byaw").id] == 2 and m.jnt_dofadr[m.joint("byaw").id] == 2
        self.mj_spec, self.model, self.data, self.k1 = s, m, d, k1
        self.payload = rb["payload"]
        self.floor = m.geom("floor").id
        nw = len(ep["workers"])
        self.worker_mocap = [m.body_mocapid[m.body(f"worker{k}").id] for k in range(nw)]
        self.worker_bodies = {m.body(f"worker{k}").id for k in range(nw)}
        # Dressed people (render only) ride a twin mocap body each; see _place_workers.
        self.worker_vis = [m.body_mocapid[m.body(f"worker{k}_vis").id] for k in range(nw)] if self.visuals else []
        self.forklift_body = m.body("forklift").id if ep["forklift"] else -1
        # What each geom is to a robot contact: 0 static, 1 loose box (jointed body), 2 person,
        # 3 forklift; -1 the robot itself, -2 the floor (never counts).
        kind = (m.body_jntnum > 0).astype(int)
        kind[list(self.worker_bodies)] = 2
        if ep["forklift"]:
            kind[self.forklift_body] = 3
        kind = kind[m.geom_bodyid]
        kind[m.body_rootid[m.geom_bodyid] == k1] = -1
        kind[self.floor] = -2
        self._geom_kind = kind.tolist()

    # ------------------------------------------------------------------ geometry helpers
    def _contact_flags(self):
        """[static, box, human, vehicle, low]: what the robot touches in this physics substep;
        low = below 0.3 m (foot level) on anything but a person. Runs every substep: plain ints."""
        d, f = self.data, [False] * 5
        if not d.ncon:
            return f
        c, gk = d.contact, self._geom_kind
        for i, (a, b) in enumerate(zip(c.geom1.tolist(), c.geom2.tolist())):
            k = gk[b] if gk[a] == -1 else gk[a] if gk[b] == -1 else -2
            if k >= 0:
                f[k] = True
                f[4] = f[4] or (k != 2 and bool(c.pos[i, 2] < 0.3))
        return f

    def _pose(self):
        q = self.data.qpos
        return q[0], q[1], q[2]

    def _clear(self, p, g):
        """A robot-wide (2 x INFLATE) corridor p->g is free of static structure at every height the
        planner grid blocks (PLAN_MIN_H and up): racks, walls, and posts/obstacles 0.3-1.0 m tall."""
        v = np.array([g[0] - p[0], g[1] - p[1], 0.0])
        dist = np.linalg.norm(v)
        if dist < 1e-6:
            return True
        v /= dist
        gid = np.zeros(1, np.int32)
        side = CLEAR_OFFSETS[:, None] * [-v[1], v[0], 0.0]
        for z in self._clear_z:
            for o in side + [p[0], p[1], z]:
                hit = mujoco.mj_ray(self.model, self.data, o, v, RACK_GROUPS, 1, -1, gid)
                if 0 <= hit < dist:
                    return False
        return True

    def _route(self, p, g):
        """Global planner: next subgoal toward g -- the farthest point of the grid path the robot
        can see straight to. Knows the static map only; boxes, pallets and people are the policy's."""
        if self._clear(p, g):
            return tuple(g)
        cells = self.grid.path(p, g)
        if not cells:
            return tuple(g)                                # unreachable: aim straight, let the scan cope
        for c in reversed(cells[1:]):
            q = self.grid.center(c)
            if self._clear(p, q):
                return q
        return self.grid.center(cells[min(1, len(cells) - 1)])

    def _geo_dist(self, p, g):
        """Path length p->g along the route (straight when clear)."""
        sub = self._route(p, g)
        if math.dist(sub, g) < 1e-6:
            return math.dist(p, g)
        f, _ = self.grid.field(g)
        v = f[self.grid.cell(sub)] if f is not None else np.inf
        return math.dist(p, sub) + (float(v) if np.isfinite(v) else math.dist(sub, g))

    def _free_point(self):
        st = self._stations
        if st and self.rng.random() < 0.5:
            return tuple(st[self.rng.integers(len(st))])   # walk to a rack face
        c = self._reach[self.rng.integers(len(self._reach))]
        return self.grid.center(c)

    # ------------------------------------------------------------------ gym API
    def make_scenario(self, seed):
        """The scenario reset(seed=seed) would run, as JSON-able data."""
        rng = np.random.default_rng(np.random.SeedSequence([int(seed), 0]))
        world = self.fixed_world if self.fixed_world is not None else generate_world(rng, self.randomize)
        ep = sample_episode(world, rng, self.base_task, self.n_picks, self.n_workers, self.n_boxes, self.randomize,
                            self._fixed_grid if self.fixed_world is not None else None)
        return {"format": SCENARIO_FORMAT, "version": SCENARIO_VERSION, "task": self.base_task, "seed": int(seed),
                "world": world, "episode": ep}

    def reset(self, seed=None, options=None):
        """options={"scenario": dict} replays that exact scenario (its own task and seed)."""
        super().reset(seed=seed)
        scn = (options or {}).get("scenario")
        if scn is None:
            if seed is None:
                seed = int(self.np_random.integers(2 ** 31))
            scn = self.make_scenario(seed)
        if scn.get("format") != SCENARIO_FORMAT or scn.get("version") != SCENARIO_VERSION:
            raise ValueError(f"not a {SCENARIO_FORMAT} v{SCENARIO_VERSION} scenario")
        if scn["task"] not in ("pick", "follow"):
            raise ValueError(f"unknown task {scn['task']!r}")
        world, ep = scn["world"], scn["episode"]
        grid = self._fixed_grid if world is self.fixed_world and self._fixed_grid else _Grid(world)
        self._build(world, ep)          # can raise (visual layer): commit nothing before it succeeded
        self.scenario, self.task, self.world, self.grid = scn, scn["task"], world, grid
        self.rng = np.random.default_rng(np.random.SeedSequence([int(scn["seed"]), 1]))
        # People only ever head for floor the robot's start connects to (a split imported map
        # would otherwise send them straight through a wall: the router's "aim straight").
        self._reach, reachable = _reachable(grid, ep["start"][:2])
        self._stations = [p for p in world["stations"] if reachable(p)]
        # A static standing on the floor is hit by the PLAN_MIN_H ray wherever the 1.0 m one hits
        # it; the 1.0 m rays are only cast for statics raised off the floor (half the ray cost).
        self._clear_z = CLEAR_Z if any(_plan_visible(st) and st["pos"][2] - st["half"][2] > PLAN_MIN_H - 0.01
                                       for st in world["statics"]) else CLEAR_Z[:1]
        hints = world.get("hints") or {}
        self.x0, self.x1, self.aisles_y = hints.get("x0"), hints.get("x1"), hints.get("aisles_y")
        self.L, self.W = world["size"]
        self.stations = world["stations"]

        rb, lk = ep["robot"], ep["link"]
        self.latency, self.drop_p, self.burst_p = lk["latency"], lk["drop_p"], lk["burst_p"]
        self.accel = tuple(rb["accel"])
        self.tau = rb["tau"]                    # loco velocity response lag (s)
        self.gait_start = rb["gait_start"]      # standstill -> first step delay (s)
        self.sway = rb["sway"]
        self.trip_gain = rb["trip_gain"]        # how easily this shift's robot trips
        self.depth_c = ep["sensing"]["depth_c"]
        self.t = 0.0
        self.pending = []                       # (arrival_time, vx, vyaw)
        self.burst_until = -1.0
        self.last_rx = 0.0
        self.cmd = np.zeros(2)                  # what the loco controller was last told
        self.vel_ref = np.zeros(2)              # accel-limited reference
        self.vel_tgt = np.zeros(2)              # lagged velocity the base actually tracks
        self.walk_ready_t = 0.0                 # loco can walk again after this time (kPrepare)
        self.moving_since = None                # None while stood still (gait start delay)
        self.a_prev = np.zeros(2)
        self.stats = dict(collisions=0, box_hits=0, human_contacts=0, near_human=0, falls=0,
                          stale_events=0, prep_events=0, id_swaps=0, in_band=0, steps=0)
        self._stale = self._prepped = False
        self._armed = False                     # bridge watchdog disarmed until the first 'v' arrives
        self.belief = None                      # follow: last detected target position
        self.belief_k = 0                       # which worker the detector THINKS is the target
        self.seen_t, self.seen_now = 0.0, False

        x, y, yaw = ep["start"]
        self.data.qpos[:3] = [x, y, yaw]
        self.forklift = None
        if ep["forklift"]:
            fk = ep["forklift"]
            self.forklift = dict(path=np.array(fk["path"], float), s=float(fk["s"]), dir=float(fk["dir"]),
                                 speed=float(fk["speed"]), pause=0.0)
            self._place_forklift()
        self.workers = [dict(pos=np.array(w["pos"], float), goal=None, speed=w["speed"], yields=w["yields"],
                             pause=0.0) for w in ep["workers"]]
        for w in self.workers:
            w["goal"] = self._free_point()
        if self.task == "follow":
            self.belief = np.array(self.workers[0]["pos"])
        self._place_workers()
        self.goals = [tuple(g) for g in ep["goals"]]
        self.t_max = ep["t_max"]
        mujoco.mj_forward(self.model, self.data)
        self.dist_prev = self._goal_dist()
        return self._obs(), {}

    def _goal(self):
        if self.task == "follow":
            return tuple(self.workers[0]["pos"])
        return self.goals[0]

    def _goal_dist(self):
        x, y, _ = self._pose()
        return self._geo_dist((x, y), self._goal())

    def _place_workers(self):
        d = self.data
        for w, mid in zip(self.workers, self.worker_mocap):
            d.mocap_pos[mid] = [w["pos"][0], w["pos"][1], 0.85]
        # The dressed person turns (at most 0.6 rad a tick) to face their last step, and keeps that
        # heading while stood. Positions only, never env.rng; the proxy capsule is not rotated.
        for w, mid in zip(self.workers, self.worker_vis):
            dx, dy = w["pos"] - d.mocap_pos[mid][:2]
            if dx * dx + dy * dy > 1e-10:
                yaw = 2 * math.atan2(d.mocap_quat[mid][3], d.mocap_quat[mid][0])
                d.mocap_quat[mid] = _yaw_quat(yaw + float(np.clip(wrap(math.atan2(dy, dx) - yaw), -0.6, 0.6)))
            d.mocap_pos[mid] = [w["pos"][0], w["pos"][1], 0.85]

    def forklift_pos(self):
        f = self.forklift
        return f["path"][0] + f["s"] * (f["path"][1] - f["path"][0])

    def _place_forklift(self):
        f = self.forklift
        mid = self.model.body_mocapid[self.forklift_body]
        p = self.forklift_pos()
        dv = f["path"][1] - f["path"][0]
        self.data.mocap_pos[mid] = [p[0], p[1], 1.0]
        self.data.mocap_quat[mid] = _yaw_quat(math.atan2(dv[1], dv[0]))

    def _move_workers(self, dt):
        x, y, _ = self._pose()
        if self.forklift:
            f = self.forklift
            if f["pause"] > 0:
                f["pause"] -= dt
            else:
                f["s"] += f["dir"] * f["speed"] * dt / max(1e-6, float(np.linalg.norm(f["path"][1] - f["path"][0])))
                if not 0.0 <= f["s"] <= 1.0:
                    f["s"] = float(np.clip(f["s"], 0.0, 1.0))
                    f["dir"] *= -1
                    f["pause"] = self.rng.uniform(0, 6)     # loading at the lane end
            self._place_forklift()
        for k, w in enumerate(self.workers):
            if w["pause"] > 0:
                w["pause"] -= dt
                continue
            d_robot = math.dist(w["pos"], (x, y))
            if k == 0 and self.task == "follow" and d_robot > 3.0:
                continue                                    # the picker glances back and waits
            if self.grid.path(tuple(w["pos"]), w["goal"], n=1) is None:
                w["goal"] = self._free_point()              # no route from here: never walk through structure
                continue
            sub = np.array(self._route(tuple(w["pos"]), w["goal"]))
            v = sub - w["pos"]
            n = np.linalg.norm(v)
            # Robot in their way: polite workers wait at 1 m, the rest brush past at 0.7 m
            # (capsule 0.22 + robot half-width 0.21 = 0.43 m is contact). Nobody walks INTO a
            # robot; if the robot turns into a stood worker outside its FOV that's on the robot.
            if d_robot < (1.0 if w["yields"] else 0.7) and v @ (np.array([x, y]) - w["pos"]) > 0:
                if self.rng.random() < 0.03:
                    w["goal"] = self._free_point()          # blocked a while: go elsewhere
                continue
            if n < 0.3 and math.dist(sub, w["goal"]) < 0.3:
                w["goal"] = self._free_point()
                w["pause"] = self.rng.uniform(0, 4)         # stop at a shelf to pick
                continue
            w["pos"] = w["pos"] + v / max(n, 1e-6) * min(n, w["speed"] * dt)
        self._place_workers()

    def _deliver(self, vx, vyaw):
        """Driver -> bridge link: latency, jitter, random drops, burst stalls."""
        if self.rng.random() < self.burst_p:
            self.burst_until = max(self.burst_until, self.t + self.rng.uniform(0.3, 1.5))   # extend, never cut a stall
        if self.t < self.burst_until or self.rng.random() < self.drop_p:
            return
        self.pending.append((self.t + self.latency + self.rng.uniform(0, 0.03), vx, vyaw))

    def step(self, action):
        vx, vyaw = action_to_cmd(action)
        self._deliver(vx, vyaw)
        n_sub = round(CTRL_DT / PHYS_DT)
        m, d = self.model, self.data
        speed_in = float(np.hypot(d.qvel[0], d.qvel[1]))  # impact speed: a hit stops the base within the tick
        hits = [False] * 5                                  # static, box, human, vehicle, low: any substep
        for _ in range(n_sub):
            while self.pending and self.pending[0][0] <= self.t:
                _, cvx, cvyaw = self.pending.pop(0)
                self.cmd[:] = cvx, cvyaw                    # bridge clamps already applied
                self.last_rx = self.t
                if self._prepped:                           # bridge: first 'v' after kPrepare -> kWalking,
                    self.walk_ready_t = self.t + self._rewalk_s   # and the loco needs its settle from HERE
                self._stale = self._prepped = False
                self._armed = True                          # bridge g_v_active: armed by any 'v', zero too
            age = self.t - self.last_rx
            if age > STALE_S and self._armed and not self._stale:
                self._stale = True
                self.stats["stale_events"] += 1
                self.cmd[:] = 0                             # watchdog: zero velocity
            if age > STALE_PREP_S and not self._prepped and self._stale:
                self._prepped = True
                self.stats["prep_events"] += 1              # bridge kPrepare: stand until commands resume
                self._rewalk_s = self.rng.uniform(1.5, 3.0)
                self.walk_ready_t = math.inf
            # Loco controller: kPrepare lockout, gait-start delay, accel limit, first-order lag,
            # gait sway (humanoids never walk straight).
            cmd = self.cmd if self.t >= self.walk_ready_t else np.zeros(2)
            if not np.any(cmd) and not np.any(np.abs(self.vel_tgt) > 0.01):
                self.moving_since = None
            elif self.moving_since is None:
                self.moving_since = self.t
            if self.moving_since is not None and self.t - self.moving_since < self.gait_start:
                cmd = np.zeros(2)                           # feet not moving yet
            dv = np.clip(cmd - self.vel_ref, -np.array(self.accel) * PHYS_DT, np.array(self.accel) * PHYS_DT)
            self.vel_ref += dv
            self.vel_tgt += (self.vel_ref - self.vel_tgt) * (PHYS_DT / self.tau)
            yaw = d.qpos[2]
            walking = self.moving_since is not None and self.t - self.moving_since >= self.gait_start
            sway = self.sway * math.sin(2 * math.pi * 1.6 * self.t) if walking else 0.0   # a stood K1 stands still
            c, s_ = math.cos(yaw), math.sin(yaw)
            d.ctrl[:] = [self.vel_tgt[0] * c - sway * s_, self.vel_tgt[0] * s_ + sway * c, self.vel_tgt[1]]
            mujoco.mj_step(m, d)
            self.t += PHYS_DT
            # Every substep: a shove (mocap forklift/person) or a stopping impact resolves within
            # the tick, so the last substep alone misses it.
            hits = [h or f for h, f in zip(hits, self._contact_flags())]
        hit_static, hit_box, hit_human, hit_vehicle, hit_low = hits
        self._move_workers(CTRL_DT)
        # Mocap actors moved after the last substep: refresh geom poses so this tick's rays and
        # occlusion checks see people/forklift where the detector math says they are.
        mujoco.mj_kinematics(m, d)

        x, y, yaw = self._pose()
        d_human = min((math.dist(w["pos"], (x, y)) for w in self.workers), default=99)
        speed = float(np.hypot(d.qvel[0], d.qvel[1]))

        # Trip model (docs/HARDENING_2026-07.md: the robot fell on relock lunges and on
        # foot-level contact while walking). Probabilities per control step, scaled per shift.
        # The lunge is the DRIVER's step (stop -> near-max in one tick), not the bridge's cmd:
        # a watchdog zero followed by the link resuming is no lunge of the policy's.
        lunge = vx - action_to_cmd(self.a_prev)[0] > 0.25
        v_hit = max(speed, speed_in)
        p_trip = self.trip_gain * (1 + self.payload / 3) * (
            0.15 * (v_hit / VX_MAX) * (hit_low and v_hit > 0.12)
            + 0.01 * lunge
            + 0.01 * (abs(d.qvel[2]) > 0.3 and speed > 0.2))
        fell = bool(hit_vehicle or self.rng.random() < p_trip)

        st = self.stats
        st["steps"] += 1
        st["collisions"] += hit_static
        st["box_hits"] += hit_box
        st["human_contacts"] += hit_human
        st["falls"] += fell
        near = d_human < (0.8 if self.task == "pick" else BAND[0] * 0.8)
        st["near_human"] += near

        a = np.clip(np.nan_to_num(np.asarray(action, np.float64)), -1, 1)   # = action_to_cmd's: NaN -> the 0 sent
        rew = -0.01 - 0.05 * float(np.sum((a - self.a_prev) ** 2))
        rew -= 1.0 * hit_static + 0.5 * hit_box + 0.5 * near
        self.a_prev = a
        terminated, success = False, False
        dist = self._goal_dist()
        if self.task == "pick":
            rew += 2.0 * (self.dist_prev - dist)
            if math.dist((x, y), self.goals[0]) < REACH_M:
                rew += 10.0
                if len(self.goals) == 1:
                    terminated = success = True             # keep the last goal for _obs()
                else:
                    self.goals.pop(0)
                    dist = self._goal_dist()
        else:
            # In band by line AND by path: across a rack from an unseen target is not following.
            inb = BAND[0] <= math.dist((x, y), self._goal()) <= BAND[1] and dist <= BAND[1]
            st["in_band"] += inb
            rew += 0.1 * inb + 0.5 * (self.dist_prev - dist) * (dist > BAND[1])
        self.dist_prev = dist
        if hit_human or fell:
            rew -= 50.0
            terminated, success = True, False               # touching a person / falling: hard fail
        truncated = self.t >= self.t_max and not terminated
        if truncated and self.task == "follow":
            success = st["in_band"] / st["steps"] >= 0.6 and st["human_contacts"] == 0
        info = dict(st, success=success, t=self.t)
        return self._obs(), float(rew), terminated, truncated, info

    # ------------------------------------------------------------------ sensing
    def _scan(self, z):
        m, d = self.model, self.data
        x, y, yaw = self._pose()
        out = np.empty(N_RAYS)
        gid = np.zeros(1, np.int32)
        for i, a in enumerate(np.linspace(-RAY_FOV / 2, RAY_FOV / 2, N_RAYS)):
            ang = yaw + a
            # From the body centre: the robot (group 2) and the visual layer (3/4) are not in
            # ENV_GROUPS. A ray started ahead of it would begin INSIDE a pallet the feet touch and
            # read its far side.
            dist = mujoco.mj_ray(m, d, np.array([x, y, z]), np.array([math.cos(ang), math.sin(ang), 0.0]),
                                 ENV_GROUPS, 1, -1, gid)
            dist = RAY_MAX if dist < 0 else dist
            dist += self.depth_c * dist * dist                 # depth reads long with range
            dist *= 1 + self.rng.normal(0, 0.01)
            if self.rng.random() < 0.05:
                dist = RAY_MAX                                  # dropout reads as free: worst case
            out[i] = min(dist, RAY_MAX)
        return out / RAY_MAX

    def _visible(self, p):
        """Camera sees point p: inside the FOV, within 6 m, not occluded by racks/boxes/people."""
        x, y, yaw = self._pose()
        rng_, b = math.dist(p, (x, y)), wrap(math.atan2(p[1] - y, p[0] - x) - yaw)
        if abs(b) > RAY_FOV / 2 or rng_ > 6.0 or rng_ < 1e-6:
            return None
        v = np.array([p[0] - x, p[1] - y, 0.0]) / rng_
        hit = mujoco.mj_ray(self.model, self.data, np.array([x, y, 1.0]), v, ENV_GROUPS, 1, -1,
                            np.zeros(1, np.int32))
        return (rng_, b) if hit < 0 or hit > rng_ - 0.3 else None

    def _detect(self):
        """Person detector + ReID. Returns (range/6, bearing) of the nearest detection, and for
        follow updates the believed target: 10% misses, 2% range glitches (the depth-glitch
        lunge class), and a lock that can jump to a worker crossing within 0.6 m of the target."""
        dets = {}
        for k, w in enumerate(self.workers):
            vis = self._visible(w["pos"])
            if vis and self.rng.random() > 0.1:
                r_, b = vis
                r_ *= self.rng.uniform(1.5, 3.0) if self.rng.random() < 0.02 else 1 + self.rng.normal(0, 0.03)
                dets[k] = (min(r_, 6.0) / 6, b)
        if self.task == "follow":
            tgt = self.workers[self.belief_k]["pos"]
            for k, w in enumerate(self.workers):
                if k != self.belief_k and k in dets and math.dist(w["pos"], tgt) < 0.6 \
                        and self.rng.random() < 0.15:
                    self.belief_k = k                       # ReID confusion: the lock jumps
                    self.stats["id_swaps"] += k != 0
                    break
            if self.belief_k != 0 and 0 in dets and math.dist(self.workers[0]["pos"], tgt) > 1.0 \
                    and self.rng.random() < 0.3:
                self.belief_k = 0                           # gallery re-match: lock returns
            self.seen_now = self.belief_k in dets
            if self.seen_now:                           # the detection, noise and glitches included
                x, y, yaw = self._pose()
                r6, b = dets[self.belief_k]
                self.belief = np.array([x + 6 * r6 * math.cos(yaw + b), y + 6 * r6 * math.sin(yaw + b)])
                self.seen_t = self.t
        if not dets:
            return 1.0, 0.0
        return min(dets.values())

    def _obs(self):
        x, y, yaw = self._pose()
        pr, pb = self._detect()
        g = tuple(self.belief) if self.task == "follow" else self._goal()
        sub = self._route((x, y), g)
        sr, sb = math.dist((x, y), sub), wrap(math.atan2(sub[1] - y, sub[0] - x) - yaw)
        gr, gb = math.dist((x, y), g), wrap(math.atan2(g[1] - y, g[0] - x) - yaw)
        qv = self.data.qvel
        vx_body = qv[0] * math.cos(yaw) + qv[1] * math.sin(yaw)
        odo = np.array([vx_body / VX_MAX, qv[2] / VYAW_MAX]) + self.rng.normal(0, 0.05, 2)
        stale = float(self.t - self.last_rx > STALE_S)
        standing = float(self.t < self.walk_ready_t)
        seen = float(self.task == "follow" and self.seen_now)
        since = min(self.t - self.seen_t, 5.0) / 5.0 if self.task == "follow" else 0.0
        o = np.concatenate([[min(sr, 10) / 10, math.sin(sb), math.cos(sb),
                             min(gr, 20) / 20, math.sin(gb), math.cos(gb)],
                            odo, self.a_prev, [stale, standing, pr, math.sin(pb), seen, since],
                            self._scan(0.10), self._scan(0.8)])
        return o.astype(np.float32)


def heuristic(obs):
    """Scripted baseline: steer to the subgoal, stop + turn away when the scan is short.
    Forward command is slew-limited like the real driver (the relock-lunge hardening)."""
    sb = math.atan2(obs[I_SUB + 1], obs[I_SUB + 2])
    low, high = obs[I_SCAN:I_SCAN + N_RAYS], obs[I_SCAN + N_RAYS:]
    scan = np.minimum(low, high) * RAY_MAX
    mid = N_RAYS // 2
    front = scan[mid - 3: mid + 3].min()
    turn = float(np.clip(sb / 0.6, -1, 1))
    fwd = 1.0 if abs(sb) < 0.5 else 0.2
    if abs(sb) > 0.8:
        return np.array([0.0, turn], np.float32)      # face the subgoal in place first
    if front < 0.7:
        fwd = -0.5
        turn = 1.0 if scan[mid:].sum() > scan[:mid].sum() else -1.0
    elif obs[I_PR] < 1.7 / 6:              # person close ahead: yield / hold ~1.5 m standoff
        fwd = -1.0 if obs[I_PR] < 1.3 / 6 else 0.0
    base = 0.0 if obs[I_STALE] or obs[I_STAND] else obs[I_APREV]   # robot zeroed: ramp from 0
    fwd = float(np.clip(fwd, base - 0.3, base + 0.3))
    return np.array([fwd, turn], np.float32)


def wilson(k, n, z=1.96):
    """95% Wilson interval (same math as eval/replay_eval.py:wilson)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    dd = 1 + z * z / n
    c = (p + z * z / (2 * n)) / dd
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / dd
    return (max(0.0, c - h), min(1.0, c + h))


def evaluate(env, policy, episodes, seed=1000):
    """Success rate + Wilson CI + safety counters. Seeds are fixed so runs compare 1:1."""
    if episodes < 1:
        raise ValueError("evaluate needs episodes >= 1")
    ok, tot = 0, {}
    for e in range(episodes):
        obs, _ = env.reset(seed=seed + e)
        done = False
        while not done:
            obs, _, term, trunc, info = env.step(policy(obs))
            done = term or trunc
        ok += info["success"]
        for k in ("collisions", "box_hits", "human_contacts", "falls", "stale_events", "prep_events", "id_swaps"):
            tot[k] = tot.get(k, 0) + info[k]
    lo, hi = wilson(ok, episodes)
    return dict(success=ok / episodes, ci95=(round(lo, 3), round(hi, 3)), episodes=episodes, **tot)


def selftest():
    assert action_to_cmd([5, -5]) == (VX_MAX, VYAW_MIN)
    assert action_to_cmd([-1, 1]) == (VX_MIN, VYAW_MAX)
    assert action_to_cmd([float("nan"), 0]) == (0.0, 0.0)
    env = K1WarehouseEnv()
    env.reset(seed=1)
    o, r, *_ = env.step([float("nan"), 0])                 # a NaN action is a stop, not NaN reward/obs
    assert np.isfinite(r) and np.isfinite(o).all() and np.isfinite(env.step([0.5, 0])[1])
    env.reset(seed=2)
    try:                                                    # wrong shape: refused before any state moves
        env.step(np.zeros(3))
        raise AssertionError("a (3,) action was accepted")
    except ValueError:
        assert env.t == 0 and env.stats["steps"] == 0
    # The env's own scenarios stay inside sim_io's caps (follow adds a target, pick a dock goal).
    import sim_io
    assert (sim_io.MAX_WORKERS, sim_io.MAX_GOALS, sim_io.MAX_T) == (MAX_WORKERS, MAX_GOALS, T_MAX_CAP)
    for kw in (dict(task="follow", n_workers=(0, MAX_WORKERS)), dict(n_picks=MAX_GOALS)):
        try:
            K1WarehouseEnv(**kw)
            raise AssertionError(f"accepted {kw}")
        except ValueError:
            pass
    try:
        evaluate(env, heuristic, 0)
        raise AssertionError("evaluate ran 0 episodes")
    except ValueError:
        pass
    assert {len(K1WarehouseEnv(task="follow", n_workers=(3, 3)).make_scenario(s)["episode"]["workers"])
            for s in range(5)} == {4}                     # the lower bound holds for follow's distractors too

    # Determinism: same seed + same actions -> identical observations.
    runs = []
    for _ in range(2):
        env = K1WarehouseEnv()
        o, _ = env.reset(seed=7)
        seq = [o]
        for _ in range(30):
            seq.append(env.step(heuristic(seq[-1]))[0])
        runs.append(np.array(seq))
    assert np.array_equal(*runs), "non-deterministic under a fixed seed"
    assert np.isfinite(runs[0]).all() and runs[0].shape[1] == OBS_DIM
    # gym's Env.spec stays an EnvSpec slot (wrappers read it); the MuJoCo spec lives on mj_spec.
    assert not isinstance(env.spec, mujoco.MjSpec) and isinstance(env.mj_spec, mujoco.MjSpec)
    # After a step, people's geoms are where the detector math puts them (rays see this tick).
    for w, mid in zip(env.workers, env.worker_mocap):
        body = int(np.nonzero(env.model.body_mocapid == mid)[0][0])
        assert np.allclose(env.data.xpos[body][:2], w["pos"]), "geoms lag the mocap actors"

    # Visual layer (assets.py) is render-only: same seed + actions -> bit-identical obs, rewards,
    # infos and qpos with it on or off, on generated maps (both tasks) and on a Local-Map-style
    # world; the proxies are untouched (the layer only adds massless mocap bodies, after them);
    # every visual geom is group 3 or 4, contype/conaffinity 0, mass 0; no depth ray can hit one;
    # and the dressed people stand where their proxies are, facing their last step.
    lm = {"name": "local map fixture", "size": [10.0, 7.0], "stations": [], "spawn": [0.8, 0.8, 2.5, 6.2],
          "forklift_lane": None, "boxes": [{"pos": [8.6, 3.5], "half": 0.2, "mass": 3.0}], "source": {"kind": "localmap"},
          "statics": [_static("wall", [5, -0.05, 1], [5, 0.05, 1]), _static("wall", [5, 7.05, 1], [5, 0.05, 1]),
                      _static("wall", [-0.05, 3.5, 1], [0.05, 3.5, 1]), _static("wall", [10.05, 3.5, 1], [0.05, 3.5, 1]),
                      _static("obstacle", [4.0, 2.0, 0.75], [1.5, 0.1, 0.75]),           # an occupancy run
                      _static("obstacle", [7.0, 5.0, 0.4], [0.3, 0.3, 0.4], 0.5),
                      _static("rack", [6.5, 2.5, 1.0], [0.4, 1.6, 1.0], 0.3),            # long, along its y
                      _static("pallet", [3.0, 5.0, 0.07], [0.6, 0.4, 0.07], 1.2)]}

    def stream(env, seed):
        """200 heuristic steps, 60 of them ramming loose box 0 from 0.8 m (contact with a dressed body)."""
        o, _ = env.reset(seed=seed)
        out, rammed = [o.tobytes()], False
        for i in range(200):
            if i == 100:
                bx, by = env.data.xpos[env.model.body("box0").id][:2]
                env.data.qpos[:3] = [bx - 0.8, by, 0.0]
            o, r, term, trunc, info = env.step([1, 0] if 100 <= i < 160 else heuristic(o))
            out.append((o.tobytes(), r, term, trunc, sorted(info.items()), env.data.qpos.tobytes()))
            rammed |= 100 <= i < 160 and info["box_hits"] > 0
            if term or trunc:
                o, _ = env.reset(seed=seed + 1 + i)
                out.append(o.tobytes())
        assert rammed, "stream never touched a loose box"
        return out

    def proxies(m, nbody):
        p = m.geom_group < 3
        return [a[p].tobytes() for a in (m.geom_type, m.geom_bodyid, m.geom_size, m.geom_pos, m.geom_quat, m.geom_group,
                                         m.geom_contype, m.geom_conaffinity, m.geom_friction)] + \
               [a[:nbody].tobytes() for a in (m.body_mass, m.body_inertia, m.body_ipos, m.body_mocapid)]
    gid = np.zeros(1, np.int32)
    for kw in (dict(task="pick"), dict(task="follow"), dict(task="follow", world=lm)):
        off, on = K1WarehouseEnv(**kw), K1WarehouseEnv(visuals=True, **kw)
        assert stream(off, 1) == stream(on, 1), f"the visual layer changed the episode: {kw}"
        for w, mid in zip(on.workers, on.worker_vis):   # dressed twins stand on the proxies...
            q, (x, y, _) = on.data.mocap_quat[mid], on.data.mocap_pos[mid]
            assert (x, y) == tuple(w["pos"]) and q[1] == q[2] == 0, (q, w["pos"], kw)
        w, mid = on.workers[0], on.worker_vis[0]        # ...and turn to a new heading, 0.6 rad a tick
        for _ in range(6):
            w["pos"] = w["pos"] + [0.0, 0.05]
            on._place_workers()
        q = on.data.mocap_quat[mid]
        assert abs(wrap(2 * math.atan2(q[3], q[0]) - math.pi / 2)) < 1e-9, (q, kw)
        off.reset(seed=1)
        on.reset(seed=1)
        m, vis = on.model, on.model.geom_group >= 3
        nb = off.model.nbody
        assert proxies(off.model, nb) == proxies(m, nb) and not (off.model.geom_group >= 3).any(), kw
        assert all(m.body(b).name.endswith("_vis") and m.body_mocapid[b] >= 0 and m.body_mass[b] == 0
                   for b in range(nb, m.nbody)), kw
        assert vis.sum() > 50 and not (m.geom_contype[vis] | m.geom_conaffinity[vis]).any(), kw
        assert all(g.mass == 0 for g in on.mj_spec.geoms if g.group >= 3), kw
        hit_vis = 0
        for x in np.linspace(0.3, on.L - 0.3, 9):
            for y in np.linspace(0.3, on.W - 0.3, 7):
                for z in (0.05, 0.1, 0.8, 1.0, 1.6):
                    for a in np.linspace(0, 2 * math.pi, 8, endpoint=False):
                        p, v = np.array([x, y, z]), np.array([math.cos(a), math.sin(a), -0.2 * (z > 1)])
                        mujoco.mj_ray(m, on.data, p, v, ENV_GROUPS, 1, -1, gid)
                        assert gid[0] < 0 or m.geom_group[gid[0]] in (0, 1), ("depth ray hit a visual geom", kw)
                        mujoco.mj_ray(m, on.data, p, v, None, 1, -1, gid)
                        hit_vis += gid[0] >= 0 and m.geom_group[gid[0]] == 3
        assert hit_vis > 100, hit_vis                   # the same rays, all groups on, do hit the layer

    # Watchdog: every command lost while walking -> velocity zeroes near STALE_S, then the
    # kPrepare tier stands the loco until commands resume, plus 1.5-3 s from THERE (a 4.7 s
    # stall still costs the full re-walk).
    env = K1WarehouseEnv(n_workers=(0, 0), n_boxes=(0, 0))
    env.reset(seed=3)
    env.forklift = None
    env.latency, env.drop_p, env.burst_p, env.accel = 0.0, 0.0, 0.0, (5.0, 5.0)
    env.tau, env.gait_start, env.trip_gain = 0.02, 0.0, 0.0
    env.data.qpos[:3] = [1.5, env.aisles_y[0], math.pi]   # facing the dock wall, clear floor ahead
    for _ in range(10):
        env.step([1, 0])
    env.drop_p = 1.0
    for _ in range(int(STALE_S / CTRL_DT) + 3):
        _, _, _, _, info = env.step([1, 0])
    assert info["stale_events"] == 1 and abs(env.data.qvel[0]) < 0.02, (info, env.data.qvel[:3])
    for _ in range(int(STALE_PREP_S / CTRL_DT) + 30):
        o, _, _, _, info = env.step([1, 0])
    assert info["prep_events"] == 1 and o[I_STAND] == 1.0
    env.drop_p, t_resume = 0.0, env.t
    o = env.step([1, 0])[0]
    assert env.walk_ready_t >= t_resume + 1.5, (env.walk_ready_t, t_resume)
    while env.t < env.walk_ready_t - CTRL_DT:
        o, _, _, _, _ = env.step([1, 0])
    assert o[I_STAND] == 1.0 and abs(env.data.qvel[0]) < 0.02, ("moved during kPrepare", env.data.qvel[:3])

    # Collision: drive into the dock wall -> counted, robot physically blocked, no fall at gain 0.
    for _ in range(80):
        _, _, term, _, info = env.step([1, 0])
    assert info["collisions"] > 0 and env.data.qpos[0] > -0.1 and not term, (info, env.data.qpos[:3])

    # A small open hall (+ extra statics) for the constructed cases below.
    def hall(*extra):
        return {"name": "test hall", "size": [10.0, 7.0], "stations": [], "spawn": [0.8, 0.8, 2.0, 6.2],
                "forklift_lane": None, "source": {"kind": "localmap"},
                "statics": [_static("wall", [5, -0.05, 1], [5, 0.05, 1]), _static("wall", [5, 7.05, 1], [5, 0.05, 1]),
                            _static("wall", [-0.05, 3.5, 1], [0.05, 3.5, 1]),
                            _static("wall", [10.05, 3.5, 1], [0.05, 3.5, 1]), *extra]}

    def stood(world, gain, at=(3.0, 3.5, 0.0)):
        env = K1WarehouseEnv(world=world, n_workers=(0, 0), n_boxes=(0, 0))
        env.reset(seed=0)
        env.latency = env.drop_p = env.burst_p = env.depth_c = 0.0
        env.trip_gain = gain
        env.data.qpos[:3] = at
        mujoco.mj_forward(env.model, env.data)
        return env

    # Watchdog armed by ANY delivered command, a zero one too (bridge g_v_active): a stall while
    # holding still still zeroes and then kPrepares.
    env = stood(hall(), 0.0)
    for _ in range(10):
        env.step([0, 0])
    env.drop_p = 1.0
    for _ in range(15):
        _, _, _, _, info = env.step([0, 0])
    assert info["stale_events"] == 1 and info["prep_events"] == 1 and env.walk_ready_t == math.inf, info

    # Trip on a low static: ramp (no lunge) into a 0.14 m pallet. The base stops inside the contact
    # tick, so only the impact speed sees the hit: a clumsy robot falls, a gain-0 one is blocked
    # with the low scan reading the pallet at the feet (rays start at the body centre).
    pallet = hall(_static("pallet", [5.5, 3.5, 0.07], [0.5, 0.25, 0.07]))
    for gain in (0.0, 100.0):
        env = stood(pallet, gain)
        for i in range(120):
            o, rew, term, _, info = env.step([min(1.0, 0.05 * (i + 1)), 0])
            if term:
                break
        if gain:
            assert term and info["falls"] == 1 and info["collisions"] > 0 and not info["success"] and rew < -40, info
        else:
            front = o[I_SCAN + N_RAYS // 2 - 2:I_SCAN + N_RAYS // 2 + 2].min() * RAY_MAX
            assert not term and info["collisions"] > 0 and env.data.qpos[0] < 5.0 and front < 0.2, (info, front)

    # Lunge = the driver's own stop -> max step. A 0.5 s link stall under a steady command (the
    # watchdog zeroes, the link resumes) is not one.
    env = stood(hall(), 100.0)
    env.gait_start = 0.0
    env.step([0, 0])
    _, _, term, _, info = env.step([1, 0])
    assert term and info["falls"] == 1, info
    env = stood(hall(), 100.0)
    env.gait_start = 0.0
    for i in range(25):
        env.step([min(1.0, 0.05 * (i + 1)), 0])
    env.drop_p = 1.0
    for _ in range(5):
        env.step([1, 0])
    env.drop_p = 0.0
    for _ in range(8):
        _, _, term, _, info = env.step([1, 0])
    assert not term and info["falls"] == 0 and info["stale_events"] == 1, info

    # Bursts only ever extend a pending stall (the web UI's injected 1.5 s stall survives).
    env = stood(hall(), 0.0)
    env.burst_until, env.burst_p = 1.5, 1.0
    for _ in range(10):
        until = env.burst_until
        env.step([1, 0])
        assert env.burst_until >= until, (env.burst_until, until)

    # Planner line of sight sees every static the grid blocks: a 0.8 m barrier (under the old
    # 1.0 m rays), one raised 0.5-1.5 m (over the PLAN_MIN_H rays), and a 0.25 m post between the
    # old 0.35 m-spaced rays. Overhead structure (a beam at 2.5 m) blocks nothing: the robot walks under.
    for extra, a, b in ((_static("obstacle", [5.0, 3.5, 0.4], [0.3, 2.5, 0.4]), (3.0, 3.5), (7.0, 3.5)),
                        (_static("obstacle", [5.0, 3.5, 1.0], [0.3, 2.5, 0.5]), (3.0, 3.5), (7.0, 3.5)),
                        (_static("obstacle", [5.0, 3.67, 1.0], [0.125, 0.125, 1.0]), (2.0, 3.5), (8.0, 3.5))):
        env = stood(hall(extra), 0.0)
        assert not env._clear(a, b) and env._route(a, b) != b and env._geo_dist(a, b) > math.dist(a, b) + 0.05, extra
    g = _Grid(hall(_static("obstacle", [5.0, 3.5, 2.6], [0.15, 3.5, 0.1])))
    assert g.path((2.0, 3.5), (8.0, 3.5)) is not None and not g.blocked[g.cell((5.0, 3.5))]
    g.field((2.0, 3.5)), g.field((8.0, 3.5)), g.field((2.0, 3.5))
    assert list(g.fields)[-1] == g.cell((2.0, 3.5))      # LRU: the field just used is the last evicted

    # Follow: the believed target is the detection (noise, glitches), not ground truth; and
    # standing across a rack from the target is not "in band".
    env = K1WarehouseEnv(task="follow")
    o, _ = env.reset(seed=1000)
    errs = []
    for _ in range(40):
        o = env.step(heuristic(o))[0]
        if env.seen_now:
            errs.append(math.dist(env.belief, env.workers[env.belief_k]["pos"]))
    assert errs and 0 < np.median(errs) < 0.2, errs
    env = K1WarehouseEnv(task="follow", n_workers=(0, 0), n_boxes=(0, 0))
    scn = env.make_scenario(5)
    (px, py, _), (_, hy, _) = next((s["pos"], s["half"]) for s in scn["world"]["statics"] if s["kind"] == "rack")
    ep = scn["episode"]
    ep.update(start=[px, py - hy - 0.26, 0.0], boxes=[], forklift=None, t_max=3.0,
              workers=[{"pos": [px, py + hy + 0.45], "speed": 0.0, "yields": True, "role": "target"}])
    ep["robot"]["trip_gain"] = 0.0
    env.reset(options={"scenario": scn})
    done = False
    while not done:
        _, _, term, trunc, info = env.step([0, 0])
        done = term or trunc
    assert info["in_band"] == 0 and not info["success"], info

    # Spawns: generated people and loose boxes start clear of the robot, boxes clear of each other,
    # pallets and the forklift: step 1 with a zero command touches nothing and nothing flies
    # (these seeds used to start inside a person or a box, or launch a box at 2-7 m/s).
    for task, seeds in (("pick", (38, 51, 79, 117, 181, 234, 284, 1016)), ("follow", (203, 251, 1016))):
        env = K1WarehouseEnv(task=task)
        for s in seeds:
            env.reset(seed=s)
            ep, m = env.scenario["episode"], env.model
            assert all(math.dist(p["pos"], ep["start"][:2]) >= START_KEEP
                       for p in ep["boxes"] + ep["workers"][task == "follow":]), (task, s)
            _, _, term, _, info = env.step([0, 0])
            v = [np.linalg.norm(env.data.qvel[m.jnt_dofadr[m.body_jntadr[m.body(f"box{k}").id]]:][:3])
                 for k in range(len(ep["boxes"]))]
            assert not term and info["human_contacts"] == 0 and np.hypot(*env.data.qvel[:2]) < 0.5 \
                and max(v, default=0.0) < 0.5, (task, s, info, v)
    # ...and on a map: the start avoids a mapped pallet and loose box (the grid only holds tall statics).
    w = hall(_static("pallet", [1.4, 2.0, 0.07], [0.6, 0.5, 0.07]))
    w["boxes"] = [{"pos": [1.4, 4.5], "half": 0.3, "mass": 3.0}]
    env = K1WarehouseEnv(world=w, n_workers=(0, 0), n_boxes=(0, 0))
    for s in range(20):
        sx, sy, _ = env.make_scenario(s)["episode"]["start"]
        assert not (abs(sx - 1.4) <= 0.6 + INFLATE and abs(sy - 2.0) <= 0.5 + INFLATE) \
            and max(abs(sx - 1.4), abs(sy - 4.5)) > 0.3 + INFLATE, (s, sx, sy)

    # People step around loose boxes (no worker-box contact): a walker through a box leaves it put.
    w = hall()
    w["boxes"] = [{"pos": [5.0, 3.5], "half": 0.2, "mass": 3.0}]
    env = K1WarehouseEnv(world=w, n_workers=(1, 1), n_boxes=(0, 0))
    env.reset(seed=0)
    env.data.qpos[:2] = [1.0, 1.0]
    wk = env.workers[0]
    wk.update(pos=np.array([4.0, 3.5]), goal=(6.5, 3.5), speed=1.1, pause=0.0)
    box = env.model.body("box0").id
    for _ in range(30):
        env.step([0, 0])
    assert wk["pos"][0] > 5.5 and np.allclose(env.data.xpos[box][:2], [5.0, 3.5], atol=0.02), (wk["pos"], env.data.xpos[box])

    # Split floor (two rooms, no door): every goal, person and person's goal is on the robot's side.
    rooms = hall(_static("wall", [5.0, 3.5, 1.0], [0.05, 3.5, 1.0]))
    rooms["stations"] = [[8.0, 3.5]]                                       # behind the wall: unreachable
    env = K1WarehouseEnv(task="pick", world=rooms, n_picks=1, n_workers=(2, 2), n_boxes=(0, 0))
    for s in range(5):
        env.reset(seed=s)
        for _ in range(20):
            env.step([0, 0])
        start = env.scenario["episode"]["start"][:2]
        assert all(env.grid.path(start, p) is not None for p in env.goals + [w["goal"] for w in env.workers]) \
            and all(w["pos"][0] < 5.0 for w in env.workers), s
    # Pick time limits use the router's path: stations behind a 20 m rack stay winnable.
    long = {"name": "long rack", "size": [26.0, 8.0], "spawn": [11.0, 1.0, 15.0, 2.2], "forklift_lane": None,
            "stations": [[13.0, 5.6], [10.0, 5.6], [16.0, 5.6]],
            "statics": [_static("wall", [13, -0.05, 1], [13, 0.05, 1]), _static("wall", [13, 8.05, 1], [13, 0.05, 1]),
                        _static("wall", [-0.05, 4, 1], [0.05, 4, 1]), _static("wall", [26.05, 4, 1], [0.05, 4, 1]),
                        _static("rack", [13.0, 4.15, 1.1], [10.0, 0.5, 1.1])]}
    env = K1WarehouseEnv(task="pick", n_picks=1, world=long)
    for s in range(10):
        ep = env.make_scenario(s)["episode"]
        f, _ = env._fixed_grid.field(ep["goals"][0])
        assert ep["t_max"] >= f[env._fixed_grid.cell(ep["start"][:2])] / VX_MAX, (s, ep["t_max"])

    # Occlusion: the follow target behind a rack is not detected; seen flag drops, belief holds.
    env = K1WarehouseEnv(task="follow", n_workers=(0, 0), n_boxes=(0, 0))
    env.reset(seed=5)
    env.data.qpos[:3] = [env.x0 + 2.0, env.aisles_y[0], math.pi / 2]     # looking across row 0
    env.workers[0]["pos"][:] = [env.x0 + 2.0, env.aisles_y[1]]           # in the next aisle
    env.workers[0]["goal"] = tuple(env.workers[0]["pos"])
    env._place_workers()
    mujoco.mj_forward(env.model, env.data)
    assert env._visible(env.workers[0]["pos"]) is None
    o = env._obs()
    assert o[I_SEEN] == 0.0 and not np.allclose(env.belief, env.workers[0]["pos"])

    # Forklift: contact is always a fall.
    env = K1WarehouseEnv(n_workers=(0, 0), n_boxes=(0, 0))
    for seed in range(50):
        env.reset(seed=seed)
        if env.forklift:
            break
    assert env.forklift, "no forklift in 50 seeds"
    env.latency, env.drop_p = 0.0, 0.0
    env.forklift.update(speed=0.0, pause=1e9)
    fp = env.forklift_pos()
    lane = env.forklift["path"][1] - env.forklift["path"][0]
    ux, uy = lane / np.linalg.norm(lane)
    side = 1.0 if env.forklift["s"] < 0.5 else -1.0                       # stay inside the lane
    env.data.qpos[:3] = [fp[0] + side * 1.2 * ux, fp[1] + side * 1.2 * uy, math.atan2(-side * uy, -side * ux)]
    env.tau, env.gait_start, env.trip_gain, env.accel = 0.02, 0.0, 0.0, (5.0, 5.0)
    for _ in range(80):
        _, _, term, _, info = env.step([1, 0])
        if term:
            break
    assert term and info["falls"] == 1, info
    # ...also when it drives INTO a stood robot: the shove clears within a tick, so contacts are
    # read every physics substep, not after the last one.
    env.reset(seed=seed)
    env.latency = env.drop_p = env.burst_p = env.trip_gain = 0.0
    f = env.forklift
    f.update(speed=max(f["speed"], 1.0), pause=0.0)
    p = env.forklift_pos() + 3.0 * f["dir"] * (f["path"][1] - f["path"][0]) / np.linalg.norm(f["path"][1] - f["path"][0])
    env.data.qpos[:3] = [p[0], p[1], 0.3]
    mujoco.mj_forward(env.model, env.data)
    for _ in range(80):
        _, _, term, _, info = env.step([0, 0])
        if term:
            break
    assert term and info["falls"] == 1, info

    # Scenario round trip: make_scenario -> JSON -> reset(options) replays bit for bit, and
    # reset(seed) is the same episode as replaying make_scenario(seed).
    import json
    env = K1WarehouseEnv(task="follow")
    scn = json.loads(json.dumps(env.make_scenario(11)))
    runs = []
    for opts in ({"scenario": scn}, None):
        o, _ = env.reset(seed=None if opts else 11, options=opts)
        seq = [o]
        for _ in range(40):
            seq.append(env.step(heuristic(seq[-1]))[0])
        runs.append(np.array(seq))
    assert np.array_equal(*runs), "scenario replay diverged from seeded reset"
    # Replaying another task's scenario runs under ITS task, and changes nothing for later seeds.
    env = K1WarehouseEnv(task="pick")
    env.reset(options={"scenario": scn})
    assert env.task == "follow"
    o, _ = env.reset(seed=5)
    assert env.task == "pick" and np.array_equal(o, K1WarehouseEnv(task="pick").reset(seed=5)[0])
    # A reset whose build fails (e.g. the visual layer) leaves the previous episode whole.
    old, build = (env.scenario, env.model, env.world, env.grid), env._build
    env._build = lambda *a: (_ for _ in ()).throw(RuntimeError("build failed"))
    try:
        env.reset(seed=6)
        raise AssertionError("reset survived a failed build")
    except RuntimeError:
        assert all(a is b for a, b in zip((env.scenario, env.model, env.world, env.grid), old))
    env._build = build

    # Fixed world: a world dict stays put across resets; episodes still randomize on it.
    world = scn["world"]
    env = K1WarehouseEnv(world=world)
    env.reset(seed=1)
    a = (env.scenario["episode"]["start"], env.scenario["world"] is world)
    env.reset(seed=2)
    assert a[1] and env.scenario["world"] is world and env.scenario["episode"]["start"] != a[0]

    # Router: a subgoal never lies inside a rack, and paths exist from the dock to every station.
    env = K1WarehouseEnv(n_workers=(0, 0), n_boxes=(0, 0))
    env.reset(seed=4)
    for st in env.stations:
        cells = env.grid.path(env.scenario["episode"]["start"][:2], st, n=10 ** 6)
        assert cells and cells[-1] == env.grid.nearest_free(env.grid.cell(st)), st
        sub = env._route(env.scenario["episode"]["start"][:2], st)
        assert not env.grid.blocked[env.grid.cell(sub)], (st, sub)

    # Solvable: the scripted baseline finishes some easy pick routes.
    r = evaluate(K1WarehouseEnv(n_picks=1, n_workers=(0, 1), n_boxes=(0, 2)), heuristic, 10)
    print("heuristic pick x1:", r)
    assert r["success"] >= 0.5, r                                          # solvable, not perfect
    r = evaluate(K1WarehouseEnv(task="follow"), heuristic, 10)
    print("heuristic follow:", r)
    print("SIM-SELFTEST-OK")


def view(task, model_path, seed):
    import mujoco.viewer
    policy = heuristic
    if model_path:
        from stable_baselines3 import PPO
        ppo = PPO.load(model_path, device="cpu")
        policy = lambda o: ppo.predict(o, deterministic=True)[0]
    env = K1WarehouseEnv(task=task, visuals=True)
    ep = 0
    while True:
        obs, _ = env.reset(seed=seed + ep)
        with mujoco.viewer.launch_passive(env.model, env.data) as v:
            v.opt.geomgroup[:] = render_option().geomgroup   # viewer group toggles 0/1 vs 3 = sensor view
            v.cam.lookat[:] = [env.L / 2, env.W / 2, 0]
            v.cam.distance, v.cam.elevation = max(env.L, env.W) * 1.1, -60
            done = False
            while v.is_running() and not done:
                t0 = time.time()
                obs, _, term, trunc, info = env.step(policy(obs))
                done = term or trunc
                v.sync()
                time.sleep(max(0.0, CTRL_DT - (time.time() - t0)))
            if not v.is_running():
                return
        print(f"episode {ep}: success={info['success']} t={info['t']:.0f}s falls={info['falls']} "
              f"collisions={info['collisions']} human_contacts={info['human_contacts']} "
              f"prep={info['prep_events']} id_swaps={info['id_swaps']}")
        ep += 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "view"])
    ap.add_argument("--task", default="pick", choices=["pick", "follow"])
    ap.add_argument("--model", help="PPO .zip from train.py (default: scripted heuristic)")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    selftest() if a.cmd == "selftest" else view(a.task, a.model, a.seed)
