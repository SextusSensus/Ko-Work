#!/usr/bin/env python3
"""Exact asset placer: labeled detections → domain instances with pose_map.

Placement policy (fail-closed):
  1. 3D label (xyz in camera/map) → transform_3d via T_map_cam / identity
  2. 2D box + depth → depth_backproject (K, depth median in ROI, T_map_cam)
  3. 2D only → ONLY if allow_ground_raycast=True or label needs_ground_raycast
  4. Idempotent merge key: run_id + detection_id

CLI:
  python eval/localmap_asset_placer.py --domain kitchen --run-id demo \\
      --labels labels.jsonl --poses poses.jsonl [--depth depth.npz] \\
      [--allow-ground-raycast] [--data-root desktop/localmap-data]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from localmap_frames import (  # noqa: E402
    T_map_base,
    T_map_cam,
    depth_backproject_box,
    ground_raycast,
    pose_map_dict,
    transform_labeled_3d,
    yaw_from_heading,
)

DEFAULT_K = {"fx": 205.8, "fy": 205.8, "cx": 247.3, "cy": 236.9, "width": 640, "height": 480}
DEFAULT_CAM_H = 1.35
DEFAULT_CAM_PITCH = 8.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detection_key(run_id: str, detection_id: str) -> str:
    return "%s::%s" % (run_id, detection_id)


def load_poses_jsonl(path: str) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[int(r["frame_idx"])] = r
    return out


def load_labels_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _resolve_K(label: Dict[str, Any], default_K: Dict[str, float]) -> Dict[str, float]:
    K = dict(default_K)
    if "intrinsics" in label and isinstance(label["intrinsics"], dict):
        K.update(label["intrinsics"])
    for k in ("fx", "fy", "cx", "cy"):
        if k in label:
            K[k] = float(label[k])
    if "cyv" in label:
        K["cy"] = float(label["cyv"])
    elif "cyv" in K:
        K["cy"] = float(K["cyv"])
    return K


def place_one(label: Dict[str, Any],
              poses: Dict[int, Dict[str, Any]],
              depth_by_frame: Optional[Dict[int, np.ndarray]] = None,
              *,
              default_K: Optional[Dict[str, float]] = None,
              default_cam_h: float = DEFAULT_CAM_H,
              default_cam_pitch: float = DEFAULT_CAM_PITCH,
              allow_ground_raycast: bool = False,
              T_source: str = "odom",
              run_id: str = "unknown") -> Dict[str, Any]:
    K0 = default_K or DEFAULT_K
    det_id = str(label.get("detection_id") or label.get("id") or label.get("track_id")
                 or "f%d_%s" % (label.get("frame_idx", -1),
                                label.get("cls") or label.get("label") or "obj"))
    rid = str(label.get("run_id") or run_id)
    cls = label.get("label") or label.get("cls") or label.get("class") or label.get("label_class")
    if not cls:
        raise ValueError("label missing class/label")

    frame_idx = label.get("frame_idx")
    pose = None
    if frame_idx is not None and int(frame_idx) in poses:
        pose = poses[int(frame_idx)]
    elif "pose" in label:
        pose = label["pose"]
    elif all(k in label for k in ("x_robot", "y_robot", "theta_robot")):
        pose = {"x": label["x_robot"], "y": label["y_robot"], "theta": label["theta_robot"],
                "cam_h": label.get("cam_h", default_cam_h),
                "cam_pitch_deg": label.get("cam_pitch_deg", default_cam_pitch)}

    cam_h = float((pose or {}).get("cam_h", label.get("cam_h", default_cam_h)))
    cam_pitch = float((pose or {}).get("cam_pitch_deg", label.get("cam_pitch_deg", default_cam_pitch)))
    rx = ry = rtheta = 0.0
    if pose:
        rx = float(pose["x"]); ry = float(pose["y"]); rtheta = float(pose.get("theta", 0.0))
    Tmc = T_map_cam(rx, ry, rtheta, cam_h, cam_pitch)
    K = _resolve_K(label, K0)
    t_ns = label.get("t_ns")
    if t_ns is None and pose is not None:
        t_ns = pose.get("t_ns")

    p_map = None
    meta: Dict[str, Any] = {}
    method = None

    xyz = label.get("xyz") or label.get("position") or label.get("xyz_cam") or label.get("xyz_map")
    src_frame = label.get("xyz_frame") or label.get("position_frame")
    if xyz is not None:
        if src_frame is None:
            src_frame = "map" if label.get("xyz_map") is not None else "camera"
        if src_frame in ("map", "odom", "world"):
            p_map, meta = transform_labeled_3d(xyz, np.eye(4), src_frame)
        elif src_frame == "base":
            p_map, meta = transform_labeled_3d(xyz, T_map_base(rx, ry, rtheta), "base")
        else:
            p_map, meta = transform_labeled_3d(xyz, Tmc, "camera")
        method = meta["placement_method"]

    box = label.get("box_xyxy") or label.get("bbox") or label.get("xyxy")
    if p_map is None and box is not None:
        depth = None
        if depth_by_frame is not None and frame_idx is not None:
            depth = depth_by_frame.get(int(frame_idx))
        if depth is None and label.get("depth_roi") is not None:
            depth = np.asarray(label["depth_roi"], dtype=np.float64)
            h, w = depth.shape[:2]
            box = [0, 0, w, h]
        depth_err = None
        if depth is not None:
            try:
                p_map, meta = depth_backproject_box(
                    depth, box, K, Tmc,
                    depth_scale=float(label.get("depth_scale", 1.0)),
                )
                method = meta["placement_method"]
            except ValueError as e:
                depth_err = str(e)
                p_map = None
        if p_map is None:
            if allow_ground_raycast or label.get("needs_ground_raycast"):
                x1, y1, x2, y2 = [float(v) for v in box]
                uc, vc = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                p_map, meta = ground_raycast(
                    uc, vc, K, Tmc, ground_z=float(label.get("ground_z", 0.0))
                )
                method = meta["placement_method"]
                if depth_err:
                    meta["depth_fallback"] = depth_err
            else:
                raise ValueError(
                    "2D box without usable depth — refuse silent guess "
                    "(pass depth or --allow-ground-raycast)%s"
                    % (("; depth: " + depth_err) if depth_err else "")
                )

    if p_map is None:
        raise ValueError("detection has neither 3D xyz nor 2D box")

    det_yaw = label.get("yaw") if label.get("yaw") is not None else label.get("detection_yaw")
    yaw = yaw_from_heading(rtheta, float(det_yaw) if det_yaw is not None else None)
    z = float(p_map[2])
    pose_map = pose_map_dict(float(p_map[0]), float(p_map[1]), z, yaw)

    conf = label.get("confidence", label.get("conf", meta.get("confidence")))
    cov = meta.get("covariance") or label.get("covariance")

    inst = {
        "id": detection_key(rid, det_id),
        "detection_id": det_id,
        "label": str(cls),
        "label_class": str(cls),
        "asset_id": label.get("asset_id"),
        "pose_map": pose_map,
        "x": pose_map["x"],
        "y": pose_map["y"],
        "z": pose_map["z"],
        "yaw": yaw,
        "T_source": T_source,
        "run_id": rid,
        "t_ns": t_ns,
        "frame_idx": int(frame_idx) if frame_idx is not None else None,
        "placement_method": method,
        "confidence": float(conf) if conf is not None else None,
        "covariance": cov,
        "source": "localmap_asset_placer",
    }
    if label.get("w") is not None:
        inst["w"] = label["w"]
    if label.get("h") is not None:
        inst["h"] = label["h"]
    return inst


def place_detections(labels: Sequence[Dict[str, Any]],
                     poses: Dict[int, Dict[str, Any]],
                     depth_by_frame: Optional[Dict[int, np.ndarray]] = None,
                     **kwargs) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    placed, errors = [], []
    for lab in labels:
        try:
            placed.append(place_one(lab, poses, depth_by_frame, **kwargs))
        except Exception as e:  # noqa: BLE001
            errors.append({
                "detection_id": lab.get("detection_id") or lab.get("id"),
                "frame_idx": lab.get("frame_idx"),
                "error": str(e),
            })
    return placed, errors


def merge_instances(existing: List[Dict[str, Any]],
                    new_ones: List[Dict[str, Any]],
                    *,
                    replace: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for inst in existing:
        key = inst.get("id") or detection_key(
            str(inst.get("run_id") or ""),
            str(inst.get("detection_id") or inst.get("id") or ""),
        )
        by_key[key] = inst
    stats = {"kept": 0, "added": 0, "replaced": 0}
    for inst in new_ones:
        key = inst.get("id") or detection_key(str(inst["run_id"]), str(inst["detection_id"]))
        if key in by_key:
            if replace:
                by_key[key] = inst
                stats["replaced"] += 1
            else:
                stats["kept"] += 1
        else:
            by_key[key] = inst
            stats["added"] += 1
    return list(by_key.values()), stats


def load_ontology_asset(data_root: str, label_class: str) -> Optional[str]:
    path = os.path.join(data_root, "asset-ontology.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        onto = json.load(f)
    for row in onto.get("label_classes") or []:
        aliases = [row.get("id")] + list(row.get("aliases") or [])
        if label_class in aliases or label_class == row.get("id"):
            return row.get("default_asset")
        if row.get("coco_id") is not None and str(row.get("coco_id")) == str(label_class):
            return row.get("default_asset")
    return None


def attach_assets(instances: List[Dict[str, Any]], data_root: str) -> None:
    for inst in instances:
        if not inst.get("asset_id"):
            aid = load_ontology_asset(data_root, inst["label_class"])
            if aid:
                inst["asset_id"] = aid


def write_domain_instances(data_root: str, domain_id: str,
                           instances: List[Dict[str, Any]],
                           notes: Optional[str] = None) -> str:
    ddir = os.path.join(data_root, "domains", domain_id)
    os.makedirs(ddir, exist_ok=True)
    path = os.path.join(ddir, "instances.json")
    payload = {
        "domain_id": domain_id,
        "updated": _utc_now(),
        "notes": notes or "Exact Rerun/label placer (pose_map FLU meters)",
        "instances": instances,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return path


def read_domain_instances(data_root: str, domain_id: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_root, "domains", domain_id, "instances.json")
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return list(data.get("instances") or [])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--poses", required=True)
    ap.add_argument("--depth", default=None)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--allow-ground-raycast", action="store_true")
    ap.add_argument("--T-source", default="odom", choices=["odom", "map"])
    ap.add_argument("--cam-h", type=float, default=DEFAULT_CAM_H)
    ap.add_argument("--cam-pitch", type=float, default=DEFAULT_CAM_PITCH)
    ap.add_argument("--intrinsics-json", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    data_root = a.data_root or os.path.join(repo, "desktop", "localmap-data")

    labels = load_labels_jsonl(a.labels)
    poses = load_poses_jsonl(a.poses)
    for p in poses.values():
        p.setdefault("cam_h", a.cam_h)
        p.setdefault("cam_pitch_deg", a.cam_pitch)

    depth_by_frame = None
    if a.depth:
        blob = np.load(a.depth, allow_pickle=True)
        depth_by_frame = {}
        for k in blob.files:
            if k.startswith("depth_f"):
                depth_by_frame[int(k.replace("depth_f", ""))] = blob[k]
            elif k.isdigit():
                depth_by_frame[int(k)] = blob[k]
        if "frames" in blob.files and "depths" in blob.files:
            for fi, d in zip(blob["frames"].tolist(), blob["depths"]):
                depth_by_frame[int(fi)] = d

    K = dict(DEFAULT_K)
    if a.intrinsics_json:
        with open(a.intrinsics_json) as f:
            K.update(json.load(f))

    placed, errors = place_detections(
        labels, poses, depth_by_frame,
        default_K=K,
        default_cam_h=a.cam_h,
        default_cam_pitch=a.cam_pitch,
        allow_ground_raycast=a.allow_ground_raycast,
        T_source=a.T_source,
        run_id=a.run_id,
    )
    attach_assets(placed, data_root)
    existing = read_domain_instances(data_root, a.domain)
    merged, stats = merge_instances(existing, placed, replace=True)

    print("PLACED %d  refused %d  merge +%d ~%d"
          % (len(placed), len(errors), stats["added"], stats["replaced"]))
    for e in errors:
        print("  REFUSE %s: %s" % (e.get("detection_id"), e["error"]))
    if a.dry_run:
        print(json.dumps(placed, indent=2))
        return 0 if placed or not labels else 2
    path = write_domain_instances(data_root, a.domain, merged)
    print("WROTE %s (%d instances)" % (path, len(merged)))
    return 0 if not (labels and not placed and errors) else 1


if __name__ == "__main__":
    sys.exit(main())
