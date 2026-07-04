#!/usr/bin/env python3
"""rrd_obstacles.py -- Phase 2: raw tracked detections -> a TRUE egocentric obstacle set + the
avoidance signal the live reflex will consume. Runs on obstacles.jsonl from rrd_label.py --track
(no robot, no models).

Pipeline (autonomy-planner: the obstacle SET is the input to the behavior layer):
  1. group per-detection rows by track_id -> per-track observations
  2. FILTER transient tracks (seen < --min-seen frames = ID-split flicker / spurious)
  3. MERGE over-split tracks (same class + 3D-close + temporally overlapping/adjacent) -> one obstacle
  4. CLASSIFY static vs dynamic (3D position spread) -- a chair/wall is static, the operator moves
  5. build the ObstacleSet + the per-frame REFLEX signal: nearest in-corridor obstacle range (the
     scalar a graded-braking reflex acts on) -- inf when the corridor is clear
  6. write obstacle_set.json (the frozen representation) + a scrubbable clean .rrd

Also answers the "what actually matters" question: class mix of the true set, static/dynamic split,
and whether depth-clearance ALONE would catch the corridor obstacle (it does when the obstacle has a
depth return) -- i.e. the live reflex can be depth-first, with COCO adding class-awareness.

Usage: python rrd_obstacles.py <obstacles.jsonl> [--out clean.rrd] [--json obstacle_set.json]
                               [--min-seen 8] [--merge-dist 0.9] [--merge-gap 30] [--static-thresh 0.35]
                               [--brake-m 1.0] [--corridor-deg 30]

Representation (egocentric, base frame x=right y=forward z=up):
  Obstacle = {id, cls, static, xyz, range_m, bearing_deg, radius_m, persistence, first, last, source}
  ReflexSnapshot(per frame) = {frame_idx, nearest_corridor_m, nearest_class, n_in_corridor}
"""
import argparse, collections, json, math, sys


def load_tracks(path):
    tracks = collections.defaultdict(list)
    frames = set()
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        frames.add(r["frame_idx"])
        if r.get("track_id") is None or "xyz" not in r:
            continue
        tracks[r["track_id"]].append(r)
    return tracks, sorted(frames)


# Classes that DON'T move on their own -> a "furniture" prior. Furniture with a large egocentric
# range spread is almost certainly an ID-drift artifact, not real motion.
FURNITURE = {"chair", "couch", "dining table", "bed", "refrigerator", "oven", "tv", "potted plant",
             "sink", "toilet", "microwave", "bench", "desk", "cabinet", "bookshelf"}


def track_stats(obs):
    import numpy as np
    xyz = np.array([o["xyz"] for o in obs], dtype=float)
    cls = collections.Counter(o["cls"] for o in obs).most_common(1)[0][0]
    order = sorted(range(len(obs)), key=lambda i: obs[i]["frame_idx"])
    fr = [obs[i]["frame_idx"] for i in order]
    return {
        "cls": cls, "xyz": xyz, "med": np.median(xyz, axis=0),
        "frames": fr, "first": fr[0], "last": fr[-1], "seen": len(obs),
        "first_pos": np.array(obs[order[0]]["xyz"], dtype=float),
        "last_pos": np.array(obs[order[-1]]["xyz"], dtype=float),
        "ranges": np.array([o["range_m"] for o in obs]),
        "spread": float(np.linalg.norm(xyz.std(axis=0))),
        "obs": obs,
    }


def merge_tracks(stats, merge_dist, merge_gap, stitch_dist, static_thresh):
    """Two association rules (autonomy-planner: the SEAMS are where tracking breaks):
      (a) CO-LOCATION -- same class + median positions within merge_dist + BOTH range-stable
          -> one stationary object re-acquired (merge across ANY temporal gap; furniture doesn't move).
      (b) ENDPOINT-STITCH -- same class + one track's LAST obs ~ the other's FIRST obs (close in 3D
          within stitch_dist AND within merge_gap frames) -> the same MOVING object continuing under
          a new id (the operator split by ByteTrack as they change range)."""
    import numpy as np
    ids = list(stats)
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = stats[ids[i]], stats[ids[j]]
            if a["cls"] != b["cls"]:
                continue
            co = (np.linalg.norm(a["med"] - b["med"]) <= merge_dist
                  and a["spread"] < static_thresh and b["spread"] < static_thresh)
            # endpoint stitch (order the two by time)
            e1, e2 = (a, b) if a["last"] <= b["last"] else (b, a)
            stitch = (0 <= (e2["first"] - e1["last"]) <= merge_gap
                      and np.linalg.norm(e1["last_pos"] - e2["first_pos"]) <= stitch_dist)
            if co or stitch:
                parent[find(ids[i])] = find(ids[j])
    groups = collections.defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return list(groups.values())


def build_obstacles(tracks, min_seen, merge_dist, merge_gap, stitch_dist, static_thresh):
    import numpy as np
    stats = {tid: track_stats(obs) for tid, obs in tracks.items()}
    kept = {tid: s for tid, s in stats.items() if s["seen"] >= min_seen}
    dropped = {tid: s for tid, s in stats.items() if s["seen"] < min_seen}
    groups = merge_tracks(kept, merge_dist, merge_gap, stitch_dist, static_thresh)

    obstacles = []
    for gi, group in enumerate(groups):
        allobs = [o for tid in group for o in kept[tid]["obs"]]
        xyz = np.array([o["xyz"] for o in allobs], dtype=float)
        rng = np.array([o["range_m"] for o in allobs])
        brg = np.array([o["bearing_deg"] for o in allobs])
        fr = sorted(o["frame_idx"] for o in allobs)
        cls = collections.Counter(o["cls"] for o in allobs).most_common(1)[0][0]
        spread = float(np.linalg.norm(xyz.std(axis=0)))
        # HONEST framing (no odometry): "range-stable" is EGOCENTRIC, not world-static. The FOLLOWED
        # operator reads range-stable because the robot holds standoff -- egocentrically stationary
        # != world-stationary. So we report range-stability + a suspect flag, not a static/dynamic call.
        range_stable = spread < static_thresh
        suspect = (cls in FURNITURE and not range_stable)  # furniture that "moves" = ID-drift artifact
        med = np.median(xyz, axis=0)
        obstacles.append({
            "id": gi, "cls": cls, "range_stable": range_stable, "suspect_iddrift": suspect,
            "spread_m": round(spread, 2),
            "xyz": [round(float(v), 2) for v in med],
            "range_m": round(float(np.median(rng)), 2),
            "bearing_deg": round(float(np.median(brg)), 1),
            "range_min_m": round(float(rng.min()), 2),
            "persistence": len(allobs), "first": fr[0], "last": fr[-1],
            "merged_tracks": sorted(group),
            "source": "yolo+depth",
        })
    obstacles.sort(key=lambda o: o["range_min_m"])
    return obstacles, kept, dropped, stats


def reflex_signal(tracks, frames, corridor_deg, brake_m):
    """Per frame: nearest IN-corridor obstacle range (the graded-braking scalar) + its class."""
    corr = corridor_deg
    per_frame = {f: [] for f in frames}
    for tid, obs in tracks.items():
        for o in obs:
            if o.get("range_m") is None:
                continue
            if abs(o.get("bearing_deg", 999)) <= corr:
                per_frame[o["frame_idx"]].append((o["range_m"], o["cls"]))
    snaps = []
    for f in frames:
        lst = per_frame[f]
        if lst:
            nearest = min(lst, key=lambda x: x[0])
            snaps.append({"frame_idx": f, "nearest_corridor_m": round(nearest[0], 2),
                          "nearest_class": nearest[1], "n_in_corridor": len(lst),
                          "brake": nearest[0] <= brake_m})
        else:
            snaps.append({"frame_idx": f, "nearest_corridor_m": None,
                          "nearest_class": None, "n_in_corridor": 0, "brake": False})
    return snaps


def write_rrd(path, obstacles, snaps, allstats, brake_m):
    import numpy as np
    import rerun as rr
    rr.init("k1_obstacles", spawn=False)
    rr.save(path)
    try:
        rr.log("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
    except Exception:
        pass
    try:
        import rerun.blueprint as rrb
        rr.send_blueprint(rrb.Blueprint(rrb.Horizontal(
            rrb.Spatial3DView(origin="/world", name="TRUE obstacle set (base frame)"),
            rrb.TimeSeriesView(origin="/reflex", name="nearest corridor obstacle (m) + brake line"),
            column_shares=[1, 1]), collapse_panels=True))
    except Exception:
        pass

    # frozen range-stable set: one point per stable obstacle at its median position (timeless);
    # suspect (ID-drift) ones flagged red so they don't read as trustworthy geometry.
    stab = [o for o in obstacles if o["range_stable"] and not o["suspect_iddrift"]]
    sus = [o for o in obstacles if o["suspect_iddrift"]]
    if stab:
        rr.log("/world/stable_set",
               rr.Points3D([o["xyz"] for o in stab],
                           labels=["%s#%d %.1fm" % (o["cls"], o["id"], o["range_m"]) for o in stab],
                           colors=[[90, 150, 90]] * len(stab), radii=0.2), static=True)
    if sus:
        rr.log("/world/suspect",
               rr.Points3D([o["xyz"] for o in sus],
                           labels=["SUSPECT %s#%d" % (o["cls"], o["id"]) for o in sus],
                           colors=[[210, 60, 60]] * len(sus), radii=0.18), static=True)

    # per-frame: range-varying obstacles at their observed position + the reflex scalar
    tid2ob = {}
    for o in obstacles:
        for tid in o["merged_tracks"]:
            tid2ob[tid] = o
    for snap in snaps:
        f = snap["frame_idx"]
        rr.set_time("frame_idx", sequence=int(f))
        rr.log("/reflex/nearest_corridor_m",
               rr.Scalars(float(snap["nearest_corridor_m"]) if snap["nearest_corridor_m"] else 10.0))
        pts, labs, cols = [], [], []
        for tid, s in allstats.items():
            ob = tid2ob.get(tid)
            if ob is None or ob["range_stable"]:
                continue
            hit = [o for o in s["obs"] if o["frame_idx"] == f]
            if hit:
                pts.append(hit[0]["xyz"]); labs.append("%s#%d" % (ob["cls"], ob["id"]))
                cols.append([200, 120, 60])
        if pts:
            rr.log("/world/varying", rr.Points3D(pts, labels=labs, colors=cols, radii=0.15))
    rr.log("/reflex/brake_line", rr.Scalars(float(brake_m)), static=True)
    try:
        rec = rr.get_global_data_recording()
        if rec is not None:
            rec.flush()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jsonl")
    ap.add_argument("--out", default=None, help="write a scrubbable clean-obstacle .rrd")
    ap.add_argument("--json", default=None, help="write the frozen obstacle_set.json")
    ap.add_argument("--min-seen", type=int, default=8)
    ap.add_argument("--merge-dist", type=float, default=0.9, help="co-location merge radius (m)")
    ap.add_argument("--merge-gap", type=int, default=45, help="max frame gap for endpoint-stitch")
    ap.add_argument("--stitch-dist", type=float, default=1.2, help="endpoint-stitch 3D radius (m)")
    ap.add_argument("--static-thresh", type=float, default=0.35, help="egocentric range-stability (m)")
    ap.add_argument("--brake-m", type=float, default=1.0)
    ap.add_argument("--corridor-deg", type=float, default=30.0)
    a = ap.parse_args()

    import numpy as np
    tracks, frames = load_tracks(a.jsonl)
    n_raw = sum(len(v) for v in tracks.values())
    obstacles, kept, dropped, allstats = build_obstacles(
        tracks, a.min_seen, a.merge_dist, a.merge_gap, a.stitch_dist, a.static_thresh)
    snaps = reflex_signal(tracks, frames, a.corridor_deg, a.brake_m)

    print("== TRACKS -> TRUE OBSTACLE SET ==")
    print("  %d raw detections across %d frames, %d raw tracks" % (n_raw, len(frames), len(allstats)))
    print("  filtered %d transient tracks (seen < %d): %s"
          % (len(dropped), a.min_seen,
             ", ".join("%s#%d(%d)" % (s["cls"], t, s["seen"]) for t, s in dropped.items()) or "none"))
    print("  merged %d kept tracks -> %d unique obstacles" % (len(kept), len(obstacles)))
    by = collections.Counter(o["cls"] for o in obstacles)
    ns = sum(1 for o in obstacles if o["range_stable"]); nsus = sum(1 for o in obstacles if o["suspect_iddrift"])
    print("  classes: %s  | range-stable %d / range-varying %d  | %d SUSPECT (furniture that 'moves' = ID-drift)"
          % (", ".join("%dx %s" % (k, c) for c, k in by.most_common()), ns, len(obstacles) - ns, nsus))
    print("\n== THE OBSTACLE SET (nearest first) ==")
    for o in obstacles:
        flag = "SUSPECT" if o["suspect_iddrift"] else ("stable " if o["range_stable"] else "varying")
        print("  %-13s #%-2d  %s  %.1fm (min %.1f)  bearing %+.0f  spread=%.1fm  persist=%d  tracks=%s"
              % (o["cls"], o["id"], flag, o["range_m"], o["range_min_m"], o["bearing_deg"],
                 o["spread_m"], o["persistence"], o["merged_tracks"]))

    # what-matters: the reflex signal
    with_ob = [s for s in snaps if s["nearest_corridor_m"] is not None]
    braking = [s for s in snaps if s["brake"]]
    print("\n== REFLEX SIGNAL (what a graded-brake reflex would see) ==")
    print("  frames with an in-corridor obstacle: %d/%d" % (len(with_ob), len(snaps)))
    if with_ob:
        rs = np.array([s["nearest_corridor_m"] for s in with_ob])
        print("  nearest-corridor range  min/median/max: %.2f / %.2f / %.2f m"
              % (rs.min(), np.median(rs), rs.max()))
        print("  frames the reflex would BRAKE (nearest <= %.1fm): %d (%.0f%%)"
              % (a.brake_m, len(braking), 100.0 * len(braking) / len(snaps)))
        nc = collections.Counter(s["nearest_class"] for s in braking)
        print("  braking cause by class: %s" % (", ".join("%dx %s" % (k, c) for c, k in nc.most_common()) or "none"))
    print("\n== WHAT MATTERS (live-reflex guidance) ==")
    print("  * every corridor obstacle here has a depth return -> depth-CLEARANCE alone would brake;")
    print("    COCO adds the CLASS (person vs chair vs fridge) for class-aware braking, not the trigger.")
    print("  * so the live reflex = cheap depth forward-clearance (always) + unfiltered COCO (class),")
    print("    both off the control loop; semantics modulate, geometry triggers.")
    print("  * HONEST LIMIT (no odometry): range-stable != world-static -- the FOLLOWED operator reads")
    print("    range-stable because the robot holds standoff. You cannot cleanly split object motion")
    print("    from robot motion in the egocentric frame; the labels are descriptive, not a world model.")
    if any(o["suspect_iddrift"] for o in obstacles):
        print("  * SUSPECT obstacles above (furniture with a big range spread) are ID-drift/depth-glitch")
        print("    artifacts -- and a spurious close reading is exactly what would FALSE-BRAKE. The reflex")
        print("    must gate on an AGED-MEDIAN range (reuse the follow's F1 anti-glitch gate), not a raw min.")

    if a.json:
        json.dump({"obstacles": obstacles,
                   "reflex": snaps,
                   "params": {"min_seen": a.min_seen, "merge_dist": a.merge_dist,
                              "merge_gap": a.merge_gap, "static_thresh": a.static_thresh,
                              "brake_m": a.brake_m, "corridor_deg": a.corridor_deg}},
                  open(a.json, "w"), indent=1)
        print("\nWROTE representation -> %s" % a.json)
    if a.out:
        write_rrd(a.out, obstacles, snaps, allstats, a.brake_m)
        print("WROTE clean obstacle .rrd -> %s" % a.out)


if __name__ == "__main__":
    sys.exit(main())
