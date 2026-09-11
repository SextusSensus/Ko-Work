"""Map-change manager -- compare the live depth view against the prebuilt map-assist prior and
record where the world no longer matches the map.

MAP-ASSIST STAYS PRIMARY. _map_assist_confirm remains the only one of the two that touches a
decision, and its role is unchanged: the prior may REINFORCE a live obstacle it agrees with, via
min(), and nothing else. This manager sits DOWNSTREAM of that as a pure observer -- it reads the same
depth selection and writes findings about where the world and the map disagree. It is a confirmation
and documentation layer on top of map-assist, never a second opinion competing with it.

WHAT THIS IS. An OBSERVATION-ONLY sink. It reads the same height-banded, operator-excluded depth
selection the obstacle layer already computes, and writes findings. It returns no clearance, caps no
velocity, and nothing in the control path consults it. A bug in here produces a wrong REPORT; it
cannot produce a collision. See docs/MAP_CHANGE_PLAN.md section 2.

WHY IT CAN EXIST AT ALL. Change detection is normally blocked on registration -- two observations of
a room are useless until they share a frame, which is why cross-run alignment is DEFERRED in
docs/RECON_CONTRACT.md. Map-assist does not have that problem: the Aurora bridge publishes a
MAP-frame pose on /aurora_odom, so live returns and the prebuilt .ply are already in one frame.
Relocalization IS the registration, and the comparison is a subtraction on a shared grid.

THE PRECONDITION, and it is absolute: the pose handed to observe() must be MAP-frame. With
/odometer_state (robot frame, per-run origin) the prior and the live view are in different frames and
every comparison is garbage. The caller enforces this -- the same frame assertion the follow drive
gate already makes -- and this module simply does nothing useful without it.

THE ASYMMETRY. APPEARED (live return where the prior is empty) is cheap: an occupied cell is its own
evidence. VANISHED (the prior says obstacle, the world is now clear) needs evidence of ABSENCE, not
absence of evidence -- a prior cell with no live return usually just means we never looked there
(occlusion, height band, the stride subsample, outside the cone). So only cells a depth ray actually
passed THROUGH may vote VANISHED. Without that ray carve the class is a noise generator.

VANISHED is reported and NEVER acted on. It is exactly the forbidden direction: acting on it would
let a stale map RAISE clearance, breaking the invariant that memory may only ever reduce it.

No ROS, no rclpy, numpy only -- so it runs and is tested off-robot.
"""

import json
import math
import os
import time
from collections import deque

import numpy as np

# Grid keys are bit-packed ints, mirroring Follower._lm_key so this reads like the code it sits
# beside. 20 bits per axis at 0.10 m => +-52 km of addressable map, far past any real map extent.
_BIAS = 1 << 19
_MASK = (1 << 20) - 1

# Viewpoint bearings are quantised into sectors and stored as a bitmask per cell, so parallax costs
# one int per cell instead of a growing list of angles.
_N_SECTORS = 16
_SECTOR_DEG = 360.0 / _N_SECTORS


def _key(gx, gy):
    return ((gx.astype(np.int64) + _BIAS) << 20) + (gy.astype(np.int64) + _BIAS)


def _unkey(k):
    return ((k >> 20) - _BIAS, (k & _MASK) - _BIAS)


def _sector_spread_deg(mask):
    """Max angular separation among the viewpoint sectors recorded in `mask`.

    Cheap stand-in for real parallax: sectors are 22.5 deg wide, so this is quantised, but it only
    ever needs to answer "did we look at this cell from meaningfully different directions?"."""
    secs = [i for i in range(_N_SECTORS) if mask & (1 << i)]
    if len(secs) < 2:
        return 0.0
    best = 0.0
    for i in range(len(secs)):
        for j in range(i + 1, len(secs)):
            d = abs(secs[i] - secs[j]) * _SECTOR_DEG
            best = max(best, min(d, 360.0 - d))
    return best


class MapChangeManager:
    """Accumulates per-cell disagreement between the live view and the static prior.

    Typical use: construct once with the same (N,2) map-frame point array map-assist loaded, call
    observe() per frame with a MAP-frame pose and the depth selection, and read findings() whenever
    a report is wanted. Nothing else in the system reads this object."""

    APPEARED = "APPEARED"
    VANISHED = "VANISHED"

    def __init__(self, prior_xy, map_id="", res_m=0.10, range_m=3.0,
                 carve_margin_m=0.15, prior_dilate_cells=2,
                 min_votes=8, min_views=2, min_parallax_deg=25.0,
                 min_region_cells=3, min_prior_support=3, max_cells=200000):
        self.map_id = map_id
        self.res = max(0.02, float(res_m))
        self.range_m = max(0.5, float(range_m))
        self.carve_margin = max(0.0, float(carve_margin_m))
        self.min_votes = max(1, int(min_votes))
        self.min_views = max(1, int(min_views))
        self.min_parallax_deg = float(min_parallax_deg)
        self.min_region_cells = max(1, int(min_region_cells))
        self.max_cells = int(max_cells)

        # cell key -> [n_appeared, n_vanished, view_mask, first_t, last_t]
        self._cells = {}
        self._n_obs = 0
        self._dropped = 0

        self.min_prior_support = max(1, int(min_prior_support))
        prior = np.asarray(prior_xy, dtype=np.float64).reshape(-1, 2)
        self._prior_xy = prior
        self._prior_raw = set()
        self._prior_solid = set()
        self._prior = set()
        if len(prior):
            pk = _key(np.round(prior[:, 0] / self.res), np.round(prior[:, 1] / self.res))
            uk, cnt = np.unique(pk, return_counts=True)
            self._prior_raw = set(int(k) for k in uk)
            # SUPPORT GATE for the VANISHED class. An Aurora .ply carries a tail of stray landmarks
            # (~0.6% in drive_20260908_104304; force_map_global_optimization is device-blocked so
            # they are never pruned), and most occupied cells hold just ONE point. Walk through
            # where a floating landmark appears to be and the algorithm honestly reports it gone --
            # but it was never there, so that is a map artifact, not a change in the world. Only
            # cells with real multi-point support may vote VANISHED. Measured on 104304: this drops
            # single-point cells from 2103/3000+ eligible to the ~400 that are actual structure.
            self._prior_solid = set(int(k) for k, n in zip(uk, cnt)
                                    if int(n) >= self.min_prior_support)
            # DILATE the prior before asking "is this cell empty in the map?". A SLAM .ply is a
            # SPARSE landmark cloud, not a filled surface -- a real wall is scattered points with
            # gaps between them. Testing raw membership would call every gap a new obstacle and
            # bury the real findings in false APPEARED. Dilation makes the test "is there any prior
            # point within ~prior_dilate_cells*res of here", which is the question actually meant.
            self._prior = self._dilate(self._prior_raw, int(prior_dilate_cells))

    # ---------------------------------------------------------------- prior

    def _dilate(self, keys, n):
        if n <= 0:
            return set(keys)
        out = set(keys)
        for _ in range(n):
            cur = list(out)
            for k in cur:
                gx, gy = _unkey(k)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        out.add(int(((gx + dx + _BIAS) << 20) + (gy + dy + _BIAS)))
        return out

    def nearest_prior_m(self, x, y, max_r_m=3.0):
        """Distance to the nearest RAW prior point, or None past max_r_m. Report-time only -- an
        expanding ring search over the key set, so no KD-tree dependency on the robot."""
        gx0, gy0 = int(round(x / self.res)), int(round(y / self.res))
        for r in range(0, int(max_r_m / self.res) + 1):
            hit = False
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    if int(((gx0 + dx + _BIAS) << 20) + (gy0 + dy + _BIAS)) in self._prior_raw:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                return r * self.res
        return None

    # ---------------------------------------------------------------- observe

    def observe(self, pose, fwd, left, t=None):
        """Fold one depth frame's returns into the vote accumulator.

        pose : (x, y, theta) in the MAP frame. Anything else and the results are meaningless.
        fwd  : (n,) forward ranges, robot frame, metres (the depth selection's forward component).
        left : (n,) lateral offsets, robot frame, +LEFT (REP-103), metres.

        Returns the number of cells touched. Never raises into the caller's loop."""
        try:
            return self._observe(pose, fwd, left, t)
        except Exception:  # noqa: BLE001 -- an observation sink must never break the control loop
            return 0

    def _observe(self, pose, fwd, left, t):
        if pose is None:
            return 0
        fwd = np.asarray(fwd, dtype=np.float64).ravel()
        left = np.asarray(left, dtype=np.float64).ravel()
        if fwd.size == 0 or fwd.size != left.size:
            return 0
        t = time.time() if t is None else float(t)
        ox, oy, oth = float(pose[0]), float(pose[1]), float(pose[2])
        c, s = math.cos(oth), math.sin(oth)
        self._n_obs += 1

        rng = np.hypot(fwd, left)
        keep = np.isfinite(rng) & (rng > 1e-3) & (rng < self.range_m)
        if not keep.any():
            return 0
        fwd, left, rng = fwd[keep], left[keep], rng[keep]

        # live returns -> map frame
        wx = ox + fwd * c - left * s
        wy = oy + fwd * s + left * c
        occ = set(int(k) for k in np.unique(_key(np.round(wx / self.res),
                                                 np.round(wy / self.res))))

        # RAY CARVE: everything between the robot and (range - margin) along each ray was looked
        # THROUGH, so it is observed-empty. This is the only thing that licenses a VANISHED vote.
        step = self.res * 0.5
        n_steps = int(self.range_m / step) + 1
        ts = (np.arange(1, n_steps + 1) * step)[None, :]          # (1,K)
        stop = (rng - self.carve_margin)[:, None]
        valid = ts < stop                                          # (n,K)
        free = set()
        if valid.any():
            ux, uy = (fwd / rng)[:, None], (left / rng)[:, None]   # unit dir, robot frame
            rx, ry = ts * ux, ts * uy
            cx = ox + rx * c - ry * s
            cy = oy + rx * s + ry * c
            fk = _key(np.round(cx[valid] / self.res), np.round(cy[valid] / self.res))
            free = set(int(k) for k in np.unique(fk))
            free -= occ            # a cell with a return in it is occupied, not carved-through

        # viewpoint sector: the bearing FROM the cell TO the robot, in the MAP frame. A body-fixed
        # artifact keeps a constant ROBOT-relative position, so as the robot moves it smears across
        # many map cells and accumulates in none -- which is exactly why this gate rejects it.
        touched = 0
        for k in occ:
            if k in self._prior:
                continue                       # live and map agree -- the common case, not stored
            touched += self._vote(k, 0, ox, oy, t)
        for k in free:
            if k not in self._prior_solid:
                continue      # nothing WELL-SUPPORTED mapped here to have vanished (see __init__)
            touched += self._vote(k, 1, ox, oy, t)
        return touched

    def _vote(self, k, idx, ox, oy, t):
        rec = self._cells.get(k)
        if rec is None:
            if len(self._cells) >= self.max_cells:
                self._dropped += 1
                return 0
            rec = [0, 0, 0, t, t]
            self._cells[k] = rec
        gx, gy = _unkey(k)
        b = math.degrees(math.atan2(oy - gy * self.res, ox - gx * self.res)) % 360.0
        rec[idx] += 1
        rec[2] |= 1 << int(b / _SECTOR_DEG)
        rec[4] = t
        return 1

    # ---------------------------------------------------------------- findings

    def _passing(self):
        """Cells that clear persistence AND parallax, with their dominant class."""
        out = {}
        for k, (na, nv, mask, t0, t1) in self._cells.items():
            cls = self.APPEARED if na >= nv else self.VANISHED
            votes = na if cls == self.APPEARED else nv
            if votes < self.min_votes:
                continue
            nviews = bin(mask).count("1")
            if nviews < self.min_views:
                continue
            spread = _sector_spread_deg(mask)
            if spread < self.min_parallax_deg:
                continue
            out[k] = (cls, votes, nviews, spread, t0, t1)
        return out

    def findings(self):
        """Cluster passing cells into contiguous regions and describe each one.

        A change is a connected object, not N loose cells -- the same contiguity doctrine the
        obstacle layer already applies. Regions are 8-connected and must reach min_region_cells."""
        passing = self._passing()
        seen, regions = set(), []
        for k0 in passing:
            if k0 in seen:
                continue
            cls0 = passing[k0][0]
            comp, q = [], deque([k0])
            seen.add(k0)
            while q:
                k = q.popleft()
                comp.append(k)
                gx, gy = _unkey(k)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nk = int(((gx + dx + _BIAS) << 20) + (gy + dy + _BIAS))
                        if nk in seen or nk not in passing or passing[nk][0] != cls0:
                            continue
                        seen.add(nk)
                        q.append(nk)
            if len(comp) < self.min_region_cells:
                continue
            regions.append(self._describe(cls0, comp, passing))
        regions.sort(key=lambda r: (-r["conf"], r["map_xy"]))
        return regions

    def _describe(self, cls, comp, passing):
        xs = np.array([_unkey(k)[0] for k in comp], dtype=np.float64) * self.res
        ys = np.array([_unkey(k)[1] for k in comp], dtype=np.float64) * self.res
        votes = sum(passing[k][1] for k in comp)
        spread = max(passing[k][3] for k in comp)
        nviews = max(passing[k][2] for k in comp)
        t0 = min(passing[k][4] for k in comp)
        t1 = max(passing[k][5] for k in comp)
        cx, cy = float(xs.mean()), float(ys.mean())
        # CONFIDENCE is an ORDERING HEURISTIC, not a probability -- it exists so a report can put
        # the most-corroborated finding first. Saturates at 0.5 on the vote threshold and scales
        # with how much parallax backs it up. Do not read it as a calibrated likelihood.
        vote_term = votes / float(votes + self.min_votes)
        view_term = min(1.0, spread / 90.0)
        return {
            "cls": cls,
            "map_xy": [round(cx, 2), round(cy, 2)],
            "extent_m": [round(float(xs.max() - xs.min() + self.res), 2),
                         round(float(ys.max() - ys.min() + self.res), 2)],
            "cells": len(comp),
            "votes": int(votes),
            "n_views": int(nviews),
            "parallax_deg": round(float(spread), 1),
            "conf": round(vote_term * (0.5 + 0.5 * view_term), 3),
            "nearest_prior_m": self.nearest_prior_m(cx, cy),
            "first_t": round(t0, 3),
            "last_t": round(t1, 3),
            "map": self.map_id,
        }

    def stats(self):
        return {"observations": self._n_obs, "tracked_cells": len(self._cells),
                "dropped_cells": self._dropped, "prior_cells": len(self._prior_raw),
                "map": self.map_id}


# -------------------------------------------------------------------- ledger

def update_ledger(path, map_id, res_m, regions, run_id="", now=None):
    """Fold this run's regions into the persistent, cross-run change ledger.

    THIS is the "documented perception of changed space": findings are keyed by MAP LOCATION, so a
    spot that changes repeatedly accumulates history instead of being re-reported cold every run.

    status: new (first run that saw it) -> persistent (confirmed again later) -> resolved (recent
    runs stopped confirming it). Resolution is what keeps the ledger from growing forever."""
    now = time.time() if now is None else float(now)
    led = {"map_id": map_id, "res_m": res_m, "regions": []}
    if os.path.exists(path):
        try:
            with open(path) as f:
                led = json.load(f)
        except Exception:  # noqa: BLE001 -- a corrupt ledger must not lose this run's findings
            led = {"map_id": map_id, "res_m": res_m, "regions": []}
    led["map_id"], led["res_m"], led["updated"] = map_id, res_m, now

    by_id = {r["id"]: r for r in led.get("regions", [])}
    grid = max(0.02, float(res_m))
    hit = set()
    for reg in regions:
        rid = "r_%d_%d_%s" % (round(reg["map_xy"][0] / grid), round(reg["map_xy"][1] / grid),
                              reg["cls"][:3].lower())
        hit.add(rid)
        obs = {"run_id": run_id, "t": round(now, 3), "votes": reg["votes"],
               "conf": reg["conf"], "cells": reg["cells"]}
        cur = by_id.get(rid)
        if cur is None:
            by_id[rid] = {"id": rid, "map_xy": reg["map_xy"], "cls": reg["cls"],
                          "extent_m": reg["extent_m"], "nearest_prior_m": reg["nearest_prior_m"],
                          "first_seen": round(now, 3), "last_seen": round(now, 3),
                          "n_runs": 1, "status": "new", "observations": [obs]}
        else:
            cur["last_seen"] = round(now, 3)
            cur["n_runs"] = int(cur.get("n_runs", 0)) + 1
            cur["status"] = "persistent"
            cur["map_xy"], cur["extent_m"] = reg["map_xy"], reg["extent_m"]
            cur.setdefault("observations", []).append(obs)
            cur["observations"] = cur["observations"][-20:]

    if run_id:
        for rid, r in by_id.items():
            if rid not in hit and r.get("status") != "resolved":
                r["misses"] = int(r.get("misses", 0)) + 1
                if r["misses"] >= 2:
                    r["status"] = "resolved"
            elif rid in hit:
                r["misses"] = 0

    led["regions"] = sorted(by_id.values(), key=lambda r: (r["status"] != "new", -r["n_runs"]))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1)
    os.replace(tmp, path)          # atomic: a killed process cannot leave a half-written ledger
    return led
