"""Geometric self-masking: which depth pixels are the ROBOT'S OWN ARMS, computed from the robot's
own published link transforms rather than guessed from a range threshold.

THE PROBLEM THIS REPLACES
_corridor_clearance discarded every depth return closer than --obstacle-self-range-m on the theory
that "anything this near is the robot". That is a threshold standing in for geometry, and it fails
in both directions at once:
  * TOO LOW and the robot's own hands leak through. The URDF puts them at 0.67 m; with the gate at
    0.65 they arrived as 0.65-0.68 m obstacles -- below --obstacle-brake-stop (0.70), i.e. an
    INSTANT full stop. Measured live 2026-09-10: clearance pinned at 0.65-0.68 with vx <= 0.02 on
    47% of frames. The robot braked on its own hands, could not close distance, the operator
    drifted past the 36 deg bearing where k_yaw*bearing saturates vyaw_max, and it railed yaw
    left/right instead of following.
  * TOO HIGH and the robot goes blind to real obstacles in the same band. Raising the gate to
    clear the hands (0.75) buys back forward motion by giving up all vision inside 0.75 m.
There is no correct threshold, because a hand at 0.67 m and a chair at 0.67 m are the same number.
Only geometry separates them.

WHAT THIS DOES INSTEAD
The K1 publishes its complete kinematic tree on /tf at ~438 Hz -- trunk_link through both arm
chains, and trunk_link through the head to camera_optical_frame. So the arms' pose in the camera
frame is not something to infer; it is broadcast. Per frame we:
  1. compose camera_optical_frame <- ... <- trunk_link from /tf and invert it,
  2. compose trunk_link -> each arm link from /tf,
  3. transform that link's precomputed surface samples (from its collision STL) into camera space,
  4. project them through the calibrated intrinsics and fill their 2D convex hull.
No URDF joint math, no axis-angle reconstruction, no thresholds -- and it tracks the arm as it
MOVES, which is precisely what a static mask (models/self_mask.npy, currently all-false) cannot.

THE SAFETY PROPERTY, and why this cannot blind the robot
A silhouette alone would be dangerous: a pixel inside the arm's outline may be BACKGROUND seen
past the arm, and masking it would hide a real obstacle. So a pixel is masked only when its
MEASURED DEPTH also agrees with the arm surface projected into that pixel, within
--self-fk-depth-tol-m. The mask can therefore only ever remove returns that are physically at the
arm's distance. A return 2 m away inside the same outline survives untouched. This makes the mask
strictly safer than the range gate it replaces, which discarded EVERYTHING inside its radius
regardless of what it was.

FAIL-CLOSED: no /tf, a missing link in the chain, stale transforms, unreadable meshes, or any
exception yields ok=False and mask() returns None, and the caller keeps its existing range-gate
behaviour. This never invents a mask from partial data.

COST: the STL surface is subsampled ONCE at construction (--self-fk-samples per link, default 120,
from meshes of 2.3k-13k triangles). Per frame the work is 8 links x 120 points transformed and
projected -- a few thousand flops -- plus one cv2.fillConvexPoly per link. Measured well under
1 ms, against a detect stage of ~23 ms.
"""
import glob
import math
import os
import struct

import numpy as np

from common import log

# The two arm chains, and the chain from the trunk out to the camera's optical frame. These are
# /tf child_frame_id values; each consecutive pair is looked up as 'parent->child'.
LEFT_ARM = ["trunk_link", "Left_Arm_1", "Left_Arm_2", "Left_Arm_3", "left_forearm_pitch_link"]
RIGHT_ARM = ["trunk_link", "Right_Arm_1", "Right_Arm_2", "Right_Arm_3", "right_forearm_pitch_link"]
# STOPS AT head_color_optical_frame -- that IS the optical frame (x-right, y-down, z-forward).
# /tf also publishes head_color_optical_frame->camera_optical_frame, which is a FURTHER axis
# permutation; including it rotates back OUT of the optical convention and the projection breaks.
# Verified 2026-09-10 against four independent probes, expressed in trunk (REP-103 x-fwd/y-left/
# z-up) and checked against the calibrated intrinsics fx=fy=205.8 cx=247.3 cy=236.9:
#   1m straight ahead at camera height -> cam z=+0.937, u=264 v=236   (on-screen, u~cx, v~cy)
#   2m straight ahead                  -> cam z=+1.936                (scales linearly)
#   1m ahead + 0.5m LEFT               -> u 264 -> 156                (left decreases u)
#   1m ahead + 0.5m DOWN               -> v 236 -> 345                (down increases v)
# Including camera_optical_frame instead gave cam=[0.005,-0.937,0.075] for the first probe -- the
# forward distance landed in y, i.e. that frame is x-up/y-backward and is NOT what to project from.
CAM_CHAIN = ["trunk_link", "Head_1", "head_pitch_link", "head_color_optical_frame"]

# link name -> collision mesh basename (from .k1.clean.urdf). The forearm link's mesh is Arm_4.
MESH_OF = {
    "Left_Arm_1": "Left_Arm_1.STL", "Left_Arm_2": "Left_Arm_2.STL",
    "Left_Arm_3": "Left_Arm_3.STL", "left_forearm_pitch_link": "Left_Arm_4.STL",
    "Right_Arm_1": "Right_Arm_1.STL", "Right_Arm_2": "Right_Arm_2.STL",
    "Right_Arm_3": "Right_Arm_3.STL", "right_forearm_pitch_link": "Right_Arm_4.STL",
}


def quat_to_R(x, y, z, w):
    """Unit-quaternion -> 3x3 rotation. Normalised defensively: /tf quaternions arrive as float32
    over the wire and a slightly non-unit quaternion would silently scale the whole arm."""
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        return np.eye(3)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


def tf_to_T(v):
    """[tx,ty,tz,qx,qy,qz,qw] -> 4x4 homogeneous transform."""
    T = np.eye(4)
    T[:3, :3] = quat_to_R(v[3], v[4], v[5], v[6])
    T[:3, 3] = (v[0], v[1], v[2])
    return T


def invert_T(T):
    """Rigid-transform inverse. Explicit rather than np.linalg.inv: R is orthonormal, so R^T is the
    exact inverse and this cannot drift or fail on a near-singular general solve."""
    Ti = np.eye(4)
    R = T[:3, :3]
    Ti[:3, :3] = R.T
    Ti[:3, 3] = -R.T @ T[:3, 3]
    return Ti


def load_stl_samples(path, n_samples):
    """Uniformly subsample vertices from a binary STL, in the mesh's own link frame.

    Samples the SURFACE rather than taking a bounding box on purpose: an AABB of a diagonal link
    is far larger than the link, and an over-large self-mask would hide real obstacles. The convex
    hull of true surface samples is tight."""
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 84:
        raise ValueError("STL too short: %s" % path)
    n_tri = struct.unpack("<I", d[80:84])[0]
    if n_tri <= 0 or len(d) < 84 + n_tri * 50:
        raise ValueError("STL triangle count %d inconsistent with size %d: %s"
                         % (n_tri, len(d), path))
    # One vertex per triangle, strided so the samples span the whole mesh rather than clustering
    # in whichever region the exporter happened to emit first.
    step = max(1, n_tri // max(1, n_samples))
    idx = range(0, n_tri, step)
    pts = np.empty((len(list(idx)), 3), dtype=np.float64)
    k = 0
    for i in range(0, n_tri, step):
        off = 84 + i * 50 + 12          # skip the 12-byte normal
        pts[k] = struct.unpack("<fff", d[off:off + 12])
        k += 1
        if k >= pts.shape[0]:
            break
    return pts[:k]


class ArmSelfMask:
    """Per-frame boolean mask of the robot's own arm pixels, from /tf + collision meshes."""

    def __init__(self, urdf_path="", mesh_dir="", samples=120, dilate_px=4, depth_tol_m=0.12,
                 max_tf_age_s=0.5):
        self.ok = False
        self.dilate_px = max(0, int(dilate_px))
        self.depth_tol_m = float(depth_tol_m)
        self.max_tf_age_s = float(max_tf_age_s)
        self.pts = {}
        self._warned = set()
        self.n_masked_last = 0
        try:
            mdir = mesh_dir or "/opt/booster/Gait/configs/K1/K1_meshes"
            if not os.path.isdir(mdir):
                raise FileNotFoundError("mesh dir not found: %s" % mdir)
            total = 0
            for link, base in MESH_OF.items():
                p = os.path.join(mdir, base)
                if not os.path.isfile(p):
                    raise FileNotFoundError("missing collision mesh for %s: %s" % (link, p))
                s = load_stl_samples(p, samples)
                if s.shape[0] < 8:
                    raise ValueError("too few samples from %s" % p)
                self.pts[link] = s
                total += s.shape[0]
            self.ok = True
            log("SELF-FK ready: %d arm links, %d surface samples, depth-tol %.2fm, dilate %dpx "
                "(masks a pixel ONLY when its measured depth agrees with the projected arm "
                "surface -- it cannot hide a distant obstacle seen past the arm)"
                % (len(self.pts), total, self.depth_tol_m, self.dilate_px))
        except Exception as e:  # noqa: BLE001 -- fail closed; caller keeps the range gate
            log("SELF-FK unavailable (%s) -> geometric self-mask OFF, range gate unchanged" % e)
            self.ok = False

    # ---- transform plumbing -------------------------------------------------------------------
    def _chain(self, tf, names):
        """Compose parent->child transforms along `names`. Returns None if any hop is missing, so a
        partially-populated /tf can never yield a wrong (and therefore unsafe) mask."""
        T = np.eye(4)
        for a, b in zip(names[:-1], names[1:]):
            v = tf.get(a + "->" + b)
            if v is None:
                if b not in self._warned:
                    self._warned.add(b)
                    log("SELF-FK missing transform %s->%s -> mask skipped this frame" % (a, b))
                return None
            T = T @ tf_to_T(v)
        return T

    def mask(self, tf, depth, fx, fy, cx, cy):
        """Boolean array (True = this pixel is the robot's own arm), or None when not computable.

        tf: {'parent->child': [tx,ty,tz,qx,qy,qz,qw]} snapshot of /tf + /tf_static.
        depth: metres, same shape as the frame; used for the depth-agreement gate."""
        if not self.ok or depth is None:
            return None
        try:
            import cv2
            h, w = depth.shape[:2]
            T_trunk_cam = self._chain(tf, CAM_CHAIN)
            if T_trunk_cam is None:
                return None
            T_cam_trunk = invert_T(T_trunk_cam)
            out = np.zeros((h, w), dtype=bool)
            hit_links = 0
            for chain in (LEFT_ARM, RIGHT_ARM):
                # Walk the chain so each link's transform is available as we go.
                T = np.eye(4)
                for a, b in zip(chain[:-1], chain[1:]):
                    v = tf.get(a + "->" + b)
                    if v is None:
                        T = None
                        break
                    T = T @ tf_to_T(v)
                    P = self.pts.get(b)
                    if P is None:
                        continue
                    # link -> camera
                    T_cl = T_cam_trunk @ T
                    pc = (T_cl[:3, :3] @ P.T).T + T_cl[:3, 3]
                    z = pc[:, 2]
                    fwd = z > 0.05                       # behind/at the lens cannot project
                    if not fwd.any():
                        continue
                    pc = pc[fwd]; z = z[fwd]
                    u = fx * pc[:, 0] / z + cx
                    vv = fy * pc[:, 1] / z + cy
                    uv = np.stack([u, vv], axis=1)
                    inb = (uv[:, 0] > -4 * w) & (uv[:, 0] < 5 * w) & \
                          (uv[:, 1] > -4 * h) & (uv[:, 1] < 5 * h)
                    if inb.sum() < 3:
                        continue
                    uv = uv[inb]; zin = z[inb]
                    poly = cv2.convexHull(uv.astype(np.float32).reshape(-1, 1, 2))
                    # ROI-LOCAL RASTERISATION. Allocating a full-frame buffer and dilating it once
                    # PER LINK cost p50 10.96 ms / max 52.5 ms -- as much as the whole detect stage
                    # (measured 2026-09-10, 8 links x 448x544). An arm covers a small part of the
                    # frame, so clip to the polygon's bounding box and do all the work there. Same
                    # result, a fraction of the memory traffic.
                    pad = self.dilate_px + 1
                    x0 = max(0, int(np.floor(uv[:, 0].min())) - pad)
                    x1 = min(w, int(np.ceil(uv[:, 0].max())) + pad)
                    y0 = max(0, int(np.floor(uv[:, 1].min())) - pad)
                    y1 = min(h, int(np.ceil(uv[:, 1].max())) + pad)
                    if x1 <= x0 or y1 <= y0:
                        continue                     # projects entirely off-frame
                    sil = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
                    cv2.fillConvexPoly(sil, (poly.reshape(-1, 2) - (x0, y0)).astype(np.int32), 1)
                    if self.dilate_px > 0:
                        k = 2 * self.dilate_px + 1
                        sil = cv2.dilate(sil, np.ones((k, k), np.uint8))
                    if not sil.any():
                        continue
                    # DEPTH AGREEMENT: only claim pixels whose measured range matches this link's
                    # projected surface. Everything else inside the outline is background seen PAST
                    # the arm and must survive, or the mask would blind the robot.
                    zlo = float(zin.min()) - self.depth_tol_m
                    zhi = float(zin.max()) + self.depth_tol_m
                    droi = depth[y0:y1, x0:x1]
                    agree = sil.astype(bool) & np.isfinite(droi) & (droi >= zlo) & (droi <= zhi)
                    if agree.any():
                        out[y0:y1, x0:x1] |= agree
                        hit_links += 1
            self.n_masked_last = int(out.sum())
            self.n_links_last = hit_links
            return out
        except Exception as e:  # noqa: BLE001 -- a mask must never break the control loop
            if "err" not in self._warned:
                self._warned.add("err")
                log("SELF-FK ERR %s -> mask skipped (range gate unchanged)" % e)
            return None
