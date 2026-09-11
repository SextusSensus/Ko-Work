#!/usr/bin/env python3
"""Rerun → Local Map domain ingest.

Imports a capture .rrd (or a run folder containing one) into
  desktop/localmap-data/domains/<domain>/runs/<run_id>/manifest.json
plus poses.jsonl, and optionally runs the exact asset placer on labels.

Frame preference: /map/{x,y,theta} if present, else /odom/{x,y,theta}.
Works without rerun SDK when given --fixture. Real .rrd decode reuses
eval/rrd_to_lerobot.read_rrd when available.

Usage:
  python eval/localmap_rerun_ingest.py --domain kitchen --rrd path/to/capture.rrd
  python eval/localmap_rerun_ingest.py --domain kitchen --run-dir runs/2026-...
  python eval/localmap_rerun_ingest.py --domain exact-lab \\
      --fixture desktop/localmap-data/fixtures/rerun_exact_place --allow-ground-raycast
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CAM_H = 1.35
DEFAULT_CAM_PITCH = 8.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def default_data_root() -> str:
    return os.path.join(_repo_root(), "desktop", "localmap-data")


def find_rrd(path: str) -> Optional[str]:
    if os.path.isfile(path) and path.endswith(".rrd"):
        return path
    if os.path.isdir(path):
        cands = []
        for root, _dirs, files in os.walk(path):
            for n in files:
                if n.endswith(".rrd"):
                    cands.append(os.path.join(root, n))
        if not cands:
            return None
        cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return cands[0]
    return None


def run_id_from_path(path: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    base = os.path.basename(path.rstrip("/"))
    if base.endswith(".rrd"):
        base = base[:-4]
    h = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)[:48]
    return "%s-%s" % (safe, h)


def extract_poses_from_scalars(scalars: Dict[str, Dict[int, float]],
                               cam_h: float, cam_pitch: float
                               ) -> Tuple[List[Dict[str, Any]], str]:
    map_x = scalars.get("/map/x") or scalars.get("/slam/x")
    map_y = scalars.get("/map/y") or scalars.get("/slam/y")
    map_t = scalars.get("/map/theta") or scalars.get("/slam/theta")
    if map_x and map_y and map_t:
        frames = sorted(set(map_x) & set(map_y) & set(map_t))
        rows = [{"frame_idx": int(fi), "x": map_x[fi], "y": map_y[fi], "theta": map_t[fi],
                 "cam_h": cam_h, "cam_pitch_deg": cam_pitch} for fi in frames]
        return rows, "map"
    ox = scalars.get("/odom/x") or {}
    oy = scalars.get("/odom/y") or {}
    ot = scalars.get("/odom/theta") or {}
    frames = sorted(set(ox) & set(oy) & set(ot))
    rows = [{"frame_idx": int(fi), "x": ox[fi], "y": oy[fi], "theta": ot[fi],
             "cam_h": cam_h, "cam_pitch_deg": cam_pitch} for fi in frames]
    return rows, "odom"


def try_read_rrd(rrd_path: str, depth_only: bool = False):
    from rrd_to_lerobot import read_rrd
    return read_rrd(rrd_path, depth_only=depth_only)


def load_intrinsics_near(rrd_path: str) -> Optional[Dict[str, Any]]:
    folder = os.path.dirname(rrd_path)
    for name in ("intrinsics.json", "camera_info.json"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            with open(p) as f:
                return json.load(f)
    return None


def write_run_bundle(data_root: str, domain_id: str, run_id: str,
                     *,
                     source_path: str,
                     frame: str,
                     poses: List[Dict[str, Any]],
                     intrinsics: Optional[Dict[str, Any]] = None,
                     cam_h: float = DEFAULT_CAM_H,
                     cam_pitch: float = DEFAULT_CAM_PITCH,
                     extra: Optional[Dict[str, Any]] = None,
                     copy_labels: Optional[str] = None,
                     copy_depth: Optional[str] = None) -> str:
    run_dir = os.path.join(data_root, "domains", domain_id, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    poses_path = os.path.join(run_dir, "poses.jsonl")
    with open(poses_path, "w") as f:
        for r in poses:
            f.write(json.dumps(r) + "\n")
    if copy_labels and os.path.isfile(copy_labels):
        shutil.copy2(copy_labels, os.path.join(run_dir, "labels.jsonl"))
    if copy_depth and os.path.isfile(copy_depth):
        shutil.copy2(copy_depth, os.path.join(run_dir, os.path.basename(copy_depth)))

    manifest = {
        "run_id": run_id,
        "domain_id": domain_id,
        "source_path": os.path.abspath(source_path),
        "imported_at": _utc_now(),
        "frame": frame,
        "frame_note": (
            "map preferred when /map/{x,y,theta} present; else odom from /odom/{x,y,theta}. "
            "Planar SE(2) on flat floor — no loop closure."
        ),
        "units": "meters",
        "axis": "FLU",
        "camera_optical_axis": "RDF",
        "cam_h_m": cam_h,
        "cam_pitch_deg": cam_pitch,
        "pose_count": len(poses),
        "poses_path": "poses.jsonl",
        "intrinsics": intrinsics,
        "T_base_optical": "eval/localmap_frames.T_base_optical(cam_h, cam_pitch_deg)",
    }
    if extra:
        manifest.update(extra)
    man_path = os.path.join(run_dir, "manifest.json")
    with open(man_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    dman = os.path.join(data_root, "domains", domain_id, "manifest.json")
    if os.path.isfile(dman):
        with open(dman) as f:
            dm = json.load(f)
        runs = dm.get("runs") or []
        if run_id not in runs:
            runs.append(run_id)
        dm["runs"] = runs
        dm["run_count"] = max(int(dm.get("run_count") or 0), len(runs))
        dm["updated"] = _utc_now()
        with open(dman, "w") as f:
            json.dump(dm, f, indent=2)
            f.write("\n")

    dj = os.path.join(data_root, "domains.json")
    if os.path.isfile(dj):
        with open(dj) as f:
            catalog = json.load(f)
        runs_dir = os.path.join(data_root, "domains", domain_id, "runs")
        n_runs = len(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else 0
        for d in catalog.get("domains") or []:
            if d.get("id") == domain_id:
                d["updated"] = _utc_now()
                d["run_count"] = max(int(d.get("run_count") or 0), n_runs)
        with open(dj, "w") as f:
            json.dump(catalog, f, indent=2)
            f.write("\n")

    return man_path


def ingest_fixture(fixture_dir: str, domain_id: str, data_root: str,
                   run_id: Optional[str] = None,
                   place: bool = True,
                   allow_ground_raycast: bool = False) -> Dict[str, Any]:
    fixture_dir = os.path.abspath(fixture_dir)
    rid = run_id_from_path(fixture_dir, run_id)
    poses_src = os.path.join(fixture_dir, "poses.jsonl")
    labels_src = os.path.join(fixture_dir, "labels.jsonl")
    depth_src = os.path.join(fixture_dir, "depth.npz")
    meta_src = os.path.join(fixture_dir, "fixture.json")
    meta = {}
    if os.path.isfile(meta_src):
        with open(meta_src) as f:
            meta = json.load(f)
    poses = []
    with open(poses_src) as f:
        for line in f:
            if line.strip():
                poses.append(json.loads(line))
    frame = meta.get("frame", "odom")
    cam_h = float(meta.get("cam_h_m", DEFAULT_CAM_H))
    cam_pitch = float(meta.get("cam_pitch_deg", DEFAULT_CAM_PITCH))
    man = write_run_bundle(
        data_root, domain_id, rid,
        source_path=fixture_dir,
        frame=frame,
        poses=poses,
        intrinsics=meta.get("intrinsics"),
        cam_h=cam_h,
        cam_pitch=cam_pitch,
        extra={"fixture": True, "fixture_id": meta.get("id", "synthetic")},
        copy_labels=labels_src if os.path.isfile(labels_src) else None,
        copy_depth=depth_src if os.path.isfile(depth_src) else None,
    )
    result = {"manifest": man, "run_id": rid, "frame": frame, "pose_count": len(poses)}
    if place and os.path.isfile(labels_src):
        from localmap_asset_placer import main as placer_main
        args = [
            "--domain", domain_id,
            "--run-id", rid,
            "--labels", os.path.join(data_root, "domains", domain_id, "runs", rid, "labels.jsonl"),
            "--poses", os.path.join(data_root, "domains", domain_id, "runs", rid, "poses.jsonl"),
            "--data-root", data_root,
            "--T-source", frame,
            "--cam-h", str(cam_h),
            "--cam-pitch", str(cam_pitch),
        ]
        depth_dst = os.path.join(data_root, "domains", domain_id, "runs", rid, "depth.npz")
        if os.path.isfile(depth_dst):
            args += ["--depth", depth_dst]
        if allow_ground_raycast or meta.get("allow_ground_raycast"):
            args.append("--allow-ground-raycast")
        if meta.get("intrinsics"):
            ip = os.path.join(data_root, "domains", domain_id, "runs", rid, "intrinsics.json")
            with open(ip, "w") as f:
                json.dump(meta["intrinsics"], f)
            args += ["--intrinsics-json", ip]
        rc = placer_main(args)
        result["placer_rc"] = rc
    return result


def ingest_rrd(rrd_path: str, domain_id: str, data_root: str,
               run_id: Optional[str] = None,
               cam_h: float = DEFAULT_CAM_H,
               cam_pitch: float = DEFAULT_CAM_PITCH,
               labels: Optional[str] = None,
               place: bool = False,
               allow_ground_raycast: bool = False) -> Dict[str, Any]:
    rrd_path = os.path.abspath(rrd_path)
    rid = run_id_from_path(rrd_path, run_id)
    try:
        scalars, _images, depth = try_read_rrd(rrd_path)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            "Failed to read .rrd (%s). Install rerun-sdk matching the recording, "
            "or use --fixture / pre-extracted --poses. Error: %s" % (rrd_path, e)
        )
    poses, frame = extract_poses_from_scalars(scalars, cam_h, cam_pitch)
    if not poses:
        raise SystemExit(
            "No /map/* or /odom/* in %s — re-capture with --odom-topic /odometer_state "
            "(see eval/rrd_poses.py)." % rrd_path
        )
    intr = load_intrinsics_near(rrd_path)
    extra = {
        "depth_frame_count": len(depth) if depth else 0,
        "rrd_basename": os.path.basename(rrd_path),
    }
    man = write_run_bundle(
        data_root, domain_id, rid,
        source_path=rrd_path,
        frame=frame,
        poses=poses,
        intrinsics=intr,
        cam_h=cam_h,
        cam_pitch=cam_pitch,
        extra=extra,
        copy_labels=labels,
    )
    result = {"manifest": man, "run_id": rid, "frame": frame, "pose_count": len(poses)}
    if place and labels:
        import numpy as np
        from localmap_asset_placer import main as placer_main
        run_dir = os.path.join(data_root, "domains", domain_id, "runs", rid)
        depth_path = None
        if depth:
            depth_path = os.path.join(run_dir, "depth.npz")
            kw = {"frames": np.array(sorted(depth.keys()), dtype=np.int64)}
            for fi in sorted(depth.keys()):
                kw["depth_f%d" % fi] = depth[fi]
            np.savez_compressed(depth_path, **kw)
        args = [
            "--domain", domain_id, "--run-id", rid,
            "--labels", os.path.join(run_dir, "labels.jsonl"),
            "--poses", os.path.join(run_dir, "poses.jsonl"),
            "--data-root", data_root, "--T-source", frame,
            "--cam-h", str(cam_h), "--cam-pitch", str(cam_pitch),
        ]
        if depth_path:
            args += ["--depth", depth_path]
        if allow_ground_raycast:
            args.append("--allow-ground-raycast")
        if intr:
            ip = os.path.join(run_dir, "intrinsics.json")
            with open(ip, "w") as f:
                json.dump(intr, f)
            args += ["--intrinsics-json", ip]
        result["placer_rc"] = placer_main(args)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rrd", help="path to .rrd")
    src.add_argument("--run-dir", help="run folder containing a .rrd")
    src.add_argument("--fixture", help="synthetic fixture dir (no rerun needed)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--cam-h", type=float, default=DEFAULT_CAM_H)
    ap.add_argument("--cam-pitch", type=float, default=DEFAULT_CAM_PITCH)
    ap.add_argument("--labels", default=None, help="labels.jsonl to copy + place")
    ap.add_argument("--place", action="store_true", help="run exact asset placer after ingest")
    ap.add_argument("--no-place", action="store_true",
                    help="skip placer for --fixture (fixtures place by default)")
    ap.add_argument("--allow-ground-raycast", action="store_true")
    a = ap.parse_args(argv)

    data_root = a.data_root or default_data_root()
    os.makedirs(os.path.join(data_root, "domains", a.domain), exist_ok=True)

    if a.fixture:
        do_place = not a.no_place
        result = ingest_fixture(
            a.fixture, a.domain, data_root,
            run_id=a.run_id,
            place=do_place,
            allow_ground_raycast=a.allow_ground_raycast,
        )
    elif a.rrd or a.run_dir:
        path = a.rrd or a.run_dir
        rrd = find_rrd(path)
        if not rrd:
            print("No .rrd found at %s" % path)
            return 2
        result = ingest_rrd(
            rrd, a.domain, data_root,
            run_id=a.run_id,
            cam_h=a.cam_h,
            cam_pitch=a.cam_pitch,
            labels=a.labels,
            place=a.place,
            allow_ground_raycast=a.allow_ground_raycast,
        )
    else:
        return 2

    print("INGEST-OK domain=%s run_id=%s frame=%s poses=%d → %s"
          % (a.domain, result["run_id"], result["frame"], result["pose_count"], result["manifest"]))
    return int(result.get("placer_rc") or 0)


if __name__ == "__main__":
    sys.exit(main())
