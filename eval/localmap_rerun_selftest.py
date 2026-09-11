#!/usr/bin/env python3
"""Self-test: exact Local Map placement math (1e-6) + fixture ingest + idempotent merge.

No rerun SDK required. Run:
  python3 eval/localmap_rerun_selftest.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from localmap_frames import (  # noqa: E402
    T_base_optical,
    T_map_base,
    T_map_cam,
    backproject_pixel,
    depth_backproject_box,
    ground_raycast,
    invert_T,
    pose_map_dict,
    quat_from_yaw,
    transform_points,
)
from localmap_asset_placer import merge_instances, place_detections  # noqa: E402
from localmap_rerun_ingest import ingest_fixture  # noqa: E402

TOL = 1e-6
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILS.append(name)


def test_T_roundtrip():
    print("\n[T matrices]")
    T = T_map_cam(1.5, -0.3, 0.4, 1.35, 8.0)
    Ti = invert_T(T)
    I = T @ Ti
    check("T @ inv(T) ≈ I", np.allclose(I, np.eye(4), atol=TOL),
          str(np.max(np.abs(I - np.eye(4)))))
    T0 = T_map_cam(0, 0, 0, 1.2, 0.0)
    p = transform_points(T0, np.array([0.0, 0.0, 1.0]))
    check("level cam Z=1 → map (1,0,1.2)", np.allclose(p, [1.0, 0.0, 1.2], atol=TOL), str(p))
    T1 = T_map_cam(2.0, 1.0, math.pi / 2, 1.2, 0.0)
    p2 = transform_points(T1, np.array([0.0, 0.0, 1.0]))
    check("yaw π/2 optical Z=1 → map (2,2,1.2)", np.allclose(p2, [2.0, 2.0, 1.2], atol=TOL), str(p2))
    Twb = T_map_base(1.0, 2.0, 0.3)
    Tbo = T_base_optical(1.1, 5.0)
    check("T_map_cam == T_map_base @ T_base_optical",
          np.allclose(T_map_cam(1.0, 2.0, 0.3, 1.1, 5.0), Twb @ Tbo, atol=TOL))


def test_backproject_and_depth():
    print("\n[depth backproject]")
    K = {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0}
    p = backproject_pixel(160, 120, 3.0, **{k: K[k] for k in ("fx", "fy", "cx", "cy")})
    check("principal-point Z=3 → (0,0,3)", np.allclose(p, [0, 0, 3], atol=TOL), str(p))
    T = T_map_cam(0, 0, 0, 1.2, 0.0)
    depth = np.full((240, 320), np.nan, dtype=np.float64)
    depth[100:140, 140:180] = 3.0
    p_map, meta = depth_backproject_box(depth, [140, 100, 180, 140], K, T)
    check("method depth_backproject", meta["placement_method"] == "depth_backproject")
    check("p_map (3,0,1.2)", np.allclose(p_map, [3, 0, 1.2], atol=TOL), str(p_map))
    try:
        depth_backproject_box(depth, [0, 0, 10, 10], K, T)
        check("empty ROI refuses", False)
    except ValueError:
        check("empty ROI refuses", True)


def test_ground_raycast():
    print("\n[ground raycast]")
    K = {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0}
    T = T_map_cam(0, 0, 0, 1.2, 0.0)
    p, meta = ground_raycast(160.0, 160.0, K, T, ground_z=0.0)
    check("method ground_raycast", meta["placement_method"] == "ground_raycast")
    check("hits z=0", abs(p[2]) < TOL, str(p))
    check("x≈6 (analytic)", abs(p[0] - 6.0) < TOL, str(p))
    check("has covariance", meta.get("covariance") is not None)
    try:
        ground_raycast(160.0, 120.0, K, T, ground_z=0.0)
        check("horizon ray refuses", False)
    except ValueError:
        check("horizon ray refuses", True)


def test_quat_yaw():
    print("\n[quat / pose_map]")
    q = quat_from_yaw(math.pi / 2)
    check("qw≈qz≈√2/2",
          abs(q["qw"] - math.sqrt(0.5)) < 1e-9 and abs(q["qz"] - math.sqrt(0.5)) < 1e-9)
    pm = pose_map_dict(1, 2, 0.5, 0.0)
    check("pose_map keys", set(pm) >= {"x", "y", "z", "qw", "qx", "qy", "qz"})


def test_fixture_placer_and_merge():
    print("\n[fixture placer + merge]")
    fix = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "desktop", "localmap-data",
        "fixtures", "rerun_exact_place",
    ))
    with open(os.path.join(fix, "fixture.json")) as f:
        meta = json.load(f)
    labels = []
    with open(os.path.join(fix, "labels.jsonl")) as f:
        for line in f:
            if line.strip():
                labels.append(json.loads(line))
    poses = {}
    with open(os.path.join(fix, "poses.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            poses[int(r["frame_idx"])] = r
    depth_blob = np.load(os.path.join(fix, "depth.npz"))
    depth_by_frame = {0: depth_blob["depth_f0"]}

    labels_no_ray = []
    for lab in labels:
        lab2 = dict(lab)
        lab2.pop("needs_ground_raycast", None)
        labels_no_ray.append(lab2)
    placed, errors = place_detections(
        labels_no_ray, poses, depth_by_frame,
        default_K=meta["intrinsics"],
        default_cam_h=meta["cam_h_m"],
        default_cam_pitch=meta["cam_pitch_deg"],
        allow_ground_raycast=False,
        run_id="fixture-run",
    )
    ids = {p["detection_id"] for p in placed}
    err_ids = {e["detection_id"] for e in errors}
    check("A placed (depth)", "det-depth-A" in ids)
    check("B placed (3d)", "det-3d-B" in ids)
    check("C refused without raycast flag", "det-ray-C" in err_ids)
    check("D refused (no depth)", "det-refuse-D" in err_ids)

    for p in placed:
        exp = meta["expected"].get(p["detection_id"])
        if not exp or exp.get("refuse"):
            continue
        xyz = np.array([p["pose_map"]["x"], p["pose_map"]["y"], p["pose_map"]["z"]])
        check("%s xyz tol 1e-6" % p["detection_id"],
              np.allclose(xyz, exp["xyz"], atol=TOL),
              "got %s want %s" % (xyz.tolist(), exp["xyz"]))
        want_m = None
        for lab in labels:
            if lab["detection_id"] == p["detection_id"]:
                want_m = lab.get("expected_method")
        if want_m:
            check("%s method" % p["detection_id"], p["placement_method"] == want_m)

    placed2, errors2 = place_detections(
        labels, poses, depth_by_frame,
        default_K=meta["intrinsics"],
        default_cam_h=meta["cam_h_m"],
        default_cam_pitch=meta["cam_pitch_deg"],
        allow_ground_raycast=True,
        run_id="fixture-run",
    )
    by = {p["detection_id"]: p for p in placed2}
    check("C placed with raycast", "det-ray-C" in by)
    if "det-ray-C" in by:
        xyz = np.array([by["det-ray-C"]["pose_map"][k] for k in "xyz"])
        check("C xyz tol 1e-6",
              np.allclose(xyz, meta["expected"]["det-ray-C"]["xyz"], atol=TOL), str(xyz))
        check("C method ground_raycast", by["det-ray-C"]["placement_method"] == "ground_raycast")
    check("D still refused", any(e["detection_id"] == "det-refuse-D" for e in errors2))

    merged, stats = merge_instances(placed2, placed2, replace=True)
    check("idempotent merge size stable", len(merged) == len(placed2))
    check("idempotent replace count", stats["replaced"] == len(placed2))
    mutated = [dict(placed2[0])]
    mutated[0] = dict(mutated[0])
    mutated[0]["pose_map"] = dict(mutated[0]["pose_map"])
    mutated[0]["pose_map"]["x"] = 99.0
    merged2, stats2 = merge_instances(placed2, mutated, replace=True)
    hit = [m for m in merged2 if m["detection_id"] == mutated[0]["detection_id"]][0]
    check("replace updates pose", hit["pose_map"]["x"] == 99.0)
    check("replace stats", stats2["replaced"] == 1 and stats2["added"] == 0)


def test_ingest_into_temp_domain():
    print("\n[ingest fixture → domain]")
    fix = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "desktop", "localmap-data",
        "fixtures", "rerun_exact_place",
    ))
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "asset-ontology.json"), "w") as f:
            json.dump({"label_classes": [
                {"id": "chair", "default_asset": "kitchen/dining-chair"},
                {"id": "dining_table", "default_asset": "kitchen/dining-table"},
                {"id": "safety_cone", "default_asset": "warehouse/safety-cone"},
            ]}, f)
        os.makedirs(os.path.join(td, "domains", "exact-lab"))
        result = ingest_fixture(fix, "exact-lab", td, run_id="golden-v1",
                                place=True, allow_ground_raycast=True)
        check("manifest written", os.path.isfile(result["manifest"]))
        with open(result["manifest"]) as f:
            man = json.load(f)
        check("manifest frame odom", man["frame"] == "odom")
        check("manifest axis FLU", man["axis"] == "FLU")
        check("manifest units meters", man["units"] == "meters")
        inst_path = os.path.join(td, "domains", "exact-lab", "instances.json")
        check("instances written", os.path.isfile(inst_path))
        with open(inst_path) as f:
            data = json.load(f)
        check("3 instances", len(data["instances"]) == 3, str(len(data["instances"])))
        ingest_fixture(fix, "exact-lab", td, run_id="golden-v1",
                       place=True, allow_ground_raycast=True)
        with open(inst_path) as f:
            data2 = json.load(f)
        check("re-place no dupes", len(data2["instances"]) == 3)


def main():
    print("localmap_rerun_selftest (tol=%g)" % TOL)
    test_T_roundtrip()
    test_backproject_and_depth()
    test_ground_raycast()
    test_quat_yaw()
    test_fixture_placer_and_merge()
    test_ingest_into_temp_domain()
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
