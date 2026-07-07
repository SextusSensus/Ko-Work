#!/usr/bin/env python3
"""rrd_items.py -- Phase B: tracked detections (COCO and/or open-vocab) -> a STRUCTURED item record
per unique physical item, against a small taxonomy. The queryable artifact each capture becomes
(vs. loose per-frame boxes). Runs on obstacles/items .jsonl from rrd_label.py --track (no robot/models).

Pipeline:
  1. POOL detections from one or more jsonl (each tagged with its source file).
  2. per (source, track_id) stats; FILTER transient tracks (seen < --min-seen).
  3. MERGE into unique physical items -- CLASS-AGNOSTIC co-location (same 3D spot = same item, even
     across sources: "refrigerator"[coco] + "cooler"[open-vocab] -> one item) + same-source
     endpoint-stitch (a moving item handed to a new id).
  4. per item: vote the LABEL, map to a TAXONOMY category, estimate cheap ATTRIBUTES (physical size
     from box+depth, range-stability), collect the contributing sources.
  5. emit item_records.json (the structured artifact) + a scrubbable .rrd (items colored by category).

Attributes here are the cheap ones (size, stability); rich attributes (colour/state/text) are the
VLM/OCR tiers (Phase C/D, LABELING_INTELLIGENCE_PLAN.md). World-frame dedup drops in once the map
(k1-odometry-mapping) provides per-frame poses; egocentric until then.

Record schema (egocentric base frame x=right y=fwd z=up):
  Item = {id, label, category, all_labels{label:count}, sources[], attributes{...}, xyz, range_m,
          bearing_deg, persistence, first, last, range_stable}

Usage: python rrd_items.py <a.jsonl> [<b.jsonl> ...] [--out items.rrd] [--json items.json]
                          [--min-seen 8] [--merge-dist 0.55] [--merge-gap 45] [--static-thresh 0.35]
                          [--hfov 70]
"""
import argparse, collections, json, math, os, sys

# Minimal taxonomy (label -> category), keyed to deli/warehouse/retail/home. Extend freely.
TAXONOMY = {
    # furniture
    "chair": "furniture", "couch": "furniture", "bench": "furniture", "table": "furniture",
    "dining table": "furniture", "desk": "furniture", "cabinet": "furniture", "shelf": "furniture",
    "bookshelf": "furniture", "bed": "furniture", "stool": "furniture",
    # appliance
    "refrigerator": "appliance", "microwave": "appliance", "oven": "appliance", "toaster": "appliance",
    "sink": "appliance", "cooler": "appliance", "tv": "appliance", "freezer": "appliance",
    # container / carried
    "cardboard box": "container", "box": "container", "trash can": "container", "bin": "container",
    "suitcase": "container", "backpack": "container", "handbag": "container", "shopping cart": "container",
    "cart": "container", "basket": "container", "bottle": "container", "cup": "container",
    "bowl": "container", "crate": "container", "tray": "container",
    # structure
    "wall": "structure", "floor": "structure", "ceiling": "structure", "door": "structure",
    "doorway": "structure", "window": "structure", "stairs": "structure", "pallet": "structure",
    # equipment
    "forklift": "equipment", "ladder": "equipment", "fire extinguisher": "equipment",
    "laptop": "equipment", "keyboard": "equipment", "mouse": "equipment", "hand truck": "equipment",
    # person
    "person": "person",
}
_CAT_COLOR = {"furniture": [90, 150, 90], "appliance": [90, 130, 200], "container": [210, 160, 60],
              "structure": [150, 150, 150], "equipment": [200, 90, 200], "person": [220, 70, 70],
              "other": [130, 130, 130]}


def category(label):
    return TAXONOMY.get(label.lower(), "other")


def load(paths):
    """Pool detections from all jsonl; key by (source, track_id). Returns {(src,tid): [rows]}, frames."""
    dets = collections.defaultdict(list)
    frames = set()
    for p in paths:
        src = os.path.basename(p)
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            frames.add(r["frame_idx"])
            if r.get("track_id") is None or "xyz" not in r:
                continue
            r["_src"] = src
            dets[(src, r["track_id"])].append(r)
    return dets, sorted(frames)


def stats(rows):
    import numpy as np
    xyz = np.array([o["xyz"] for o in rows], dtype=float)
    order = sorted(range(len(rows)), key=lambda i: rows[i]["frame_idx"])
    fr = [rows[i]["frame_idx"] for i in order]
    return {"src": rows[0]["_src"], "med": np.median(xyz, axis=0), "spread": float(np.linalg.norm(xyz.std(0))),
            "first": fr[0], "last": fr[-1], "first_pos": np.array(rows[order[0]]["xyz"], float),
            "last_pos": np.array(rows[order[-1]]["xyz"], float), "seen": len(rows), "rows": rows}


def merge(S, merge_dist, merge_gap, static_thresh):
    """Union-find: CLASS-AGNOSTIC co-location (any source) + same-source endpoint-stitch."""
    import numpy as np
    ids = list(S)
    par = {i: i for i in ids}
    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = S[ids[i]], S[ids[j]]
            co = (np.linalg.norm(a["med"] - b["med"]) <= merge_dist
                  and a["spread"] < static_thresh and b["spread"] < static_thresh)
            e1, e2 = (a, b) if a["last"] <= b["last"] else (b, a)
            stitch = (a["src"] == b["src"] and 0 <= (e2["first"] - e1["last"]) <= merge_gap
                      and np.linalg.norm(e1["last_pos"] - e2["first_pos"]) <= max(merge_dist, 0.9))
            if co or stitch:
                par[find(ids[i])] = find(ids[j])
    groups = collections.defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return list(groups.values())


def build(dets, min_seen, merge_dist, merge_gap, static_thresh, hfov):
    import numpy as np
    S = {k: stats(v) for k, v in dets.items()}
    kept = {k: s for k, s in S.items() if s["seen"] >= min_seen}
    dropped = len(S) - len(kept)
    groups = merge(kept, merge_dist, merge_gap, static_thresh)
    items = []
    for gi, grp in enumerate(groups):
        rows = [o for k in grp for o in kept[k]["rows"]]
        xyz = np.array([o["xyz"] for o in rows], float)
        labs = collections.Counter(o["cls"] for o in rows)
        label = labs.most_common(1)[0][0]
        srcs = sorted({o["_src"] for o in rows})
        rng = np.array([o["range_m"] for o in rows])
        brg = np.array([o["bearing_deg"] for o in rows])
        fr = sorted(o["frame_idx"] for o in rows)
        spread = float(np.linalg.norm(xyz.std(0)))
        # cheap physical-size attribute from the median box + median range (pinhole)
        wpx = np.median([o["box"][2] - o["box"][0] for o in rows])
        hpx = np.median([o["box"][3] - o["box"][1] for o in rows])
        # need image width for focal; infer from a wide bbox is unreliable -> use hfov + assume 544 wide
        foc = (544 / 2.0) / math.tan(math.radians(hfov) / 2.0)
        med_r = float(np.median(rng))
        size_w = round(wpx * med_r / foc, 2)
        size_h = round(hpx * med_r / foc, 2)
        items.append({
            "id": gi, "label": label, "category": category(label),
            "all_labels": dict(labs), "sources": srcs,
            "attributes": {"width_m": size_w, "height_m": size_h,
                           "range_stable": bool(spread < static_thresh), "spread_m": round(spread, 2)},
            "xyz": [round(float(v), 2) for v in np.median(xyz, 0)],
            "range_m": round(med_r, 2), "bearing_deg": round(float(np.median(brg)), 1),
            "persistence": len(rows), "first": fr[0], "last": fr[-1],
        })
    items.sort(key=lambda x: (x["category"], x["range_m"]))
    return items, dropped


def write_rrd(path, items):
    import rerun as rr
    rr.init("k1_items", spawn=False)
    rr.save(path)
    try:
        rr.log("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
    except Exception:
        pass
    if items:
        rr.log("/world/items",
               rr.Points3D([it["xyz"] for it in items],
                           labels=["%s [%s] %.1fm" % (it["label"], it["category"], it["range_m"]) for it in items],
                           colors=[_CAT_COLOR.get(it["category"], [130, 130, 130]) for it in items],
                           radii=0.2), static=True)
    try:
        rec = rr.get_global_data_recording()
        if rec is not None:
            rec.flush()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jsonl", nargs="+")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-seen", type=int, default=8)
    ap.add_argument("--merge-dist", type=float, default=0.55)
    ap.add_argument("--merge-gap", type=int, default=45)
    ap.add_argument("--static-thresh", type=float, default=0.35)
    ap.add_argument("--hfov", type=float, default=70.0)
    a = ap.parse_args()

    dets, frames = load(a.jsonl)
    n_raw = sum(len(v) for v in dets.values())
    items, dropped = build(dets, a.min_seen, a.merge_dist, a.merge_gap, a.static_thresh, a.hfov)

    print("== STRUCTURED ITEM RECORDS ==")
    print("  sources: %s" % ", ".join(os.path.basename(p) for p in a.jsonl))
    print("  %d detections / %d raw tracks -> %d transient dropped -> %d UNIQUE ITEMS"
          % (n_raw, len(dets), dropped, len(items)))
    bycat = collections.Counter(it["category"] for it in items)
    print("  by category: %s" % ", ".join("%dx %s" % (v, k) for k, v in bycat.most_common()))
    print("\n  %-14s %-11s %-7s %-16s %s" % ("label", "category", "range", "size WxH", "sources"))
    for it in items:
        at = it["attributes"]
        print("  %-14s %-11s %5.1fm  %4.2fx%4.2fm      %s%s"
              % (it["label"], it["category"], it["range_m"], at["width_m"], at["height_m"],
                 ",".join(s.replace("items_", "").replace("obstacles", "obs").replace(".jsonl", "") for s in it["sources"]),
                 "" if len(it["all_labels"]) == 1 else "  (also: %s)" % ",".join(k for k in it["all_labels"] if k != it["label"])))

    if a.json:
        json.dump({"items": items, "taxonomy_categories": sorted(set(TAXONOMY.values()) | {"other"}),
                   "params": {"min_seen": a.min_seen, "merge_dist": a.merge_dist}},
                  open(a.json, "w"), indent=1)
        print("\nWROTE item records -> %s" % a.json)
    if a.out:
        write_rrd(a.out, items)
        print("WROTE item .rrd -> %s" % a.out)


if __name__ == "__main__":
    sys.exit(main())
