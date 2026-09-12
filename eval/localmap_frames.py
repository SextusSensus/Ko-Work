#!/usr/bin/env python3
"""Exact rigid-body / pinhole math for Local Map ↔ Rerun placement.

Frame conventions (match eval/rrd_map.py, ROS REP-103):
  * map / odom / base: FLU — X forward, Y left, Z up. Units: meters. Yaw about +Z.
  * camera optical: RDF — X right, Y down, Z forward (pinhole).
  * Viewer (Three.js Y-up): map (x, y) → Three (x, z); yaw_three = -yaw_map.

Homogeneous transforms are 4x4 row-major numpy arrays. Composition is left-to-right
application on column vectors: p_map = T_map_cam @ p_cam.

This module has NO rerun dependency — pure math so unit tests run on cloud VMs.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


def T_map_base(x: float, y: float, theta: float, z: float = 0.0) -> np.ndarray:
    """Planar SE(2) pose of base in map/odom → SE(3) (REP-103 FLU)."""
    c, s = math.cos(theta), math.sin(theta)
    return np.array(
        [[c, -s, 0.0, x],
         [s,  c, 0.0, y],
         [0.0, 0.0, 1.0, z],
         [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


T_world_base = T_map_base


def T_base_optical(cam_h: float, pitch_deg: float) -> np.ndarray:
    """Static mount: optical → base. Identical to eval/rrd_map.T_base_optical."""
    R0 = np.array([[0.0, 0.0, 1.0],
                   [-1.0, 0.0, 0.0],
                   [0.0, -1.0, 0.0]], dtype=np.float64)
    p = math.radians(pitch_deg)
    cp, sp = math.cos(p), math.sin(p)
    Rp = np.array([[cp, 0.0, sp],
                   [0.0, 1.0, 0.0],
                   [-sp, 0.0, cp]], dtype=np.float64)
    R = Rp @ R0
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = [0.0, 0.0, float(cam_h)]
    return T


def T_map_cam(x: float, y: float, theta: float, cam_h: float, pitch_deg: float,
              z_base: float = 0.0) -> np.ndarray:
    return T_map_base(x, y, theta, z_base) @ T_base_optical(cam_h, pitch_deg)


def invert_T(T: np.ndarray) -> np.ndarray:
    R = T[:3, :3]
    t = T[:3, 3]
    Ti = np.eye(4, dtype=np.float64)
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ t
    return Ti


def transform_points(T: np.ndarray, pts: np.ndarray) -> np.ndarray:
    single = pts.ndim == 1
    p = np.atleast_2d(pts).astype(np.float64)
    ones = np.ones((p.shape[0], 1), dtype=np.float64)
    out = (T @ np.hstack([p, ones]).T)[:3].T
    return out[0] if single else out


def backproject_pixel(u: float, v: float, Z: float,
                      fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    X = (u - cx) * Z / fx
    Y = (v - cy) * Z / fy
    return np.array([X, Y, Z], dtype=np.float64)


def depth_roi_median(depth: np.ndarray, u0: float, v0: float, u1: float, v1: float,
                     z_min: float = 0.1, z_max: float = 12.0) -> Optional[float]:
    h, w = depth.shape[:2]
    x0 = int(max(0, min(w - 1, math.floor(min(u0, u1)))))
    x1 = int(max(0, min(w, math.ceil(max(u0, u1)))))
    y0 = int(max(0, min(h - 1, math.floor(min(v0, v1)))))
    y1 = int(max(0, min(h, math.ceil(max(v0, v1)))))
    if x1 <= x0 or y1 <= y0:
        return None
    patch = depth[y0:y1, x0:x1]
    valid = patch[(patch > z_min) & (patch < z_max) & np.isfinite(patch)]
    if valid.size < 8:
        return None
    return float(np.median(valid))


def depth_backproject_box(depth: np.ndarray, box_xyxy: Sequence[float],
                          K: Dict[str, float],
                          T_map_camera: np.ndarray,
                          depth_scale: float = 1.0,
                          use_roi_median: bool = True) -> Tuple[np.ndarray, Dict[str, Any]]:
    x1, y1, x2, y2 = [float(v) for v in box_xyxy]
    uc, vc = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
    d = depth.astype(np.float64) * float(depth_scale)
    if use_roi_median:
        Z = depth_roi_median(d, x1, y1, x2, y2)
    else:
        h, w = d.shape[:2]
        ui, vi = int(round(uc)), int(round(vc))
        if not (0 <= ui < w and 0 <= vi < h):
            raise ValueError("box center outside depth image")
        Z = float(d[vi, ui])
        if not (0.1 < Z < 12.0) or not math.isfinite(Z):
            Z = None
    if Z is None:
        raise ValueError("no valid depth in ROI (refuse silent guess)")
    fx, fy, cx, cy = float(K["fx"]), float(K["fy"]), float(K["cx"]), float(K["cy"])
    p_cam = backproject_pixel(uc, vc, Z, fx, fy, cx, cy)
    p_map = transform_points(T_map_camera, p_cam)
    meta = {
        "placement_method": "depth_backproject",
        "u": uc, "v": vc, "Z": Z,
        "p_cam": p_cam.tolist(),
    }
    return p_map, meta


def transform_labeled_3d(p: Sequence[float], T: np.ndarray,
                         source_frame: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    src = source_frame.lower()
    p3 = np.asarray(p, dtype=np.float64).reshape(3)
    if src in ("map", "odom", "world"):
        return p3.copy(), {"placement_method": "transform_3d", "T_source": src, "applied": "I"}
    if src in ("camera", "cam", "optical", "base"):
        out = transform_points(T, p3)
        return out, {"placement_method": "transform_3d", "T_source": src, "applied": "T"}
    raise ValueError("unknown source_frame %r (expected camera|map|odom|base)" % source_frame)


def ground_raycast(u: float, v: float, K: Dict[str, float],
                   T_map_camera: np.ndarray,
                   ground_z: float = 0.0) -> Tuple[np.ndarray, Dict[str, Any]]:
    fx, fy, cx, cy = float(K["fx"]), float(K["fy"]), float(K["cx"]), float(K["cy"])
    d_cam = backproject_pixel(u, v, 1.0, fx, fy, cx, cy)
    d_cam = d_cam / np.linalg.norm(d_cam)
    R = T_map_camera[:3, :3]
    t = T_map_camera[:3, 3]
    d_map = R @ d_cam
    dz = d_map[2]
    if abs(dz) < 1e-9:
        raise ValueError("ground_raycast: ray parallel to ground (refuse)")
    s = (ground_z - t[2]) / dz
    if s <= 0.0:
        raise ValueError("ground_raycast: intersection behind camera (refuse)")
    p = t + s * d_map
    cos_inc = abs(dz)
    if cos_inc < 0.08:
        raise ValueError("ground_raycast: grazing incidence (refuse)")
    sigma_xy = 0.05 + 0.25 * (1.0 / cos_inc - 1.0)
    cov = [
        [float(sigma_xy ** 2), 0.0, 0.0],
        [0.0, float(sigma_xy ** 2), 0.0],
        [0.0, 0.0, 0.0004],
    ]
    meta = {
        "placement_method": "ground_raycast",
        "u": u, "v": v, "s": float(s),
        "residual": {"incidence_cos": float(cos_inc), "range_m": float(s)},
        "covariance": cov,
        "confidence": float(min(0.95, cos_inc)),
    }
    return p, meta


def yaw_from_heading(theta_robot: float, detection_yaw: Optional[float] = None) -> float:
    if detection_yaw is not None and math.isfinite(detection_yaw):
        return float(detection_yaw)
    return float(theta_robot)


def quat_from_yaw(yaw: float) -> Dict[str, float]:
    half = 0.5 * yaw
    return {
        "qw": math.cos(half),
        "qx": 0.0,
        "qy": 0.0,
        "qz": math.sin(half),
    }


def pose_map_dict(x: float, y: float, z: float, yaw: float) -> Dict[str, float]:
    q = quat_from_yaw(yaw)
    return {"x": float(x), "y": float(y), "z": float(z), **q}


def mat4_list(T: np.ndarray) -> List[List[float]]:
    return [[float(T[i, j]) for j in range(4)] for i in range(4)]


FRAME_DOC = {
    "map_odom_base": "FLU",
    "camera_optical": "RDF",
    "units": "meters",
    "yaw": "radians about +Z; asset +X aligns to yaw (robot heading unless detection_yaw set)",
    "viewer_threejs": "map (x,y) → Three (x,z); rotation.y = -yaw_map",
}
