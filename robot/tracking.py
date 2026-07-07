"""Pixel-space multi-object tracker (P3.2): a constant-velocity Kalman track per person in IMAGE
space, giving every detection a stable track_id + motion prediction so the follower prefers the
SAME person across frames and can coast through brief occlusions. Extracted verbatim from
follow_person_k1.py (pure move). numpy-only; optional scipy for optimal assignment (greedy else).
"""
import math

import numpy as np

from common import iou_xyxy

try:
    from scipy.optimize import linear_sum_assignment as _linear_sum_assignment
    _HAVE_LSA = True
except Exception:  # noqa: BLE001 -- scipy is optional; greedy fallback below
    _HAVE_LSA = False




def _point_box_dist(px, py, box):
    """Euclidean distance from a point to the nearest edge of a box (0.0 if inside).
    Used by the gesture bystander-adjacency gate (SCOPE §2.1 G4)."""
    x1, y1, x2, y2 = box
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return math.hypot(dx, dy)


def max_iou_other(persons, idx, self_track_id=None):
    """Max IoU of persons[idx]['box'] vs every OTHER person's box. Self-excluded
    by INDEX (a copied tuple would self-match at IoU 1.0 and silently disable
    isolation). When self_track_id is given, also skip persons sharing it
    (split/duplicate detections of the SAME person). Returns 0.0 with no others;
    on any fault returns 1.0 (treated as NOT isolated -> fail safe: no admit/bank)."""
    try:
        box = persons[idx]["box"]
        m = 0.0
        for j, q in enumerate(persons):
            if j == idx:
                continue
            if self_track_id is not None and q.get("track_id") == self_track_id:
                continue
            m = max(m, iou_xyxy(box, q["box"]))
        return m
    except Exception:  # noqa: BLE001
        return 1.0


def _box_to_z(box):
    """(x1,y1,x2,y2) -> measurement [cx, cy, s(area), r(aspect w/h)]."""
    x1, y1, x2, y2 = box
    w = max(x2 - x1, 1e-3); h = max(y2 - y1, 1e-3)
    return np.array([x1 + w / 2.0, y1 + h / 2.0, w * h, w / h], dtype=np.float64)


def _x_to_box(x):
    """State -> (x1,y1,x2,y2) from [cx, cy, s, r, ...]."""
    cx, cy, s, r = float(x[0]), float(x[1]), max(float(x[2]), 1e-6), max(float(x[3]), 1e-6)
    w = math.sqrt(s * r); h = (s / w) if w > 0 else 1.0
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


class Track:
    """One constant-velocity Kalman track. State x = [cx, cy, s, r, vcx, vcy, vs].
    Measurement z = [cx, cy, s, r] (SORT-style). All in pixel/image units."""
    _F = None   # state transition (shared, constant)
    _H = None   # measurement matrix (shared, constant)

    def __init__(self, box, tid):
        if Track._F is None:
            F = np.eye(7)
            F[0, 4] = F[1, 5] = F[2, 6] = 1.0        # cx+=vcx, cy+=vcy, s+=vs
            Track._F = F
            H = np.zeros((4, 7)); H[0, 0] = H[1, 1] = H[2, 2] = H[3, 3] = 1.0
            Track._H = H
        self.id = tid
        self.x = np.zeros(7)
        self.x[:4] = _box_to_z(box)
        self.P = np.eye(7) * 10.0
        self.P[4:, 4:] *= 1000.0                     # velocities start very uncertain
        self.Q = np.eye(7); self.Q[4:, 4:] *= 0.01; self.Q[-1, -1] *= 0.01
        self.R = np.eye(4); self.R[2:, 2:] *= 10.0   # area/aspect noisier than centre
        self.time_since_update = 0
        self.hits = 1
        self.age = 0

    def predict(self):
        self.x = Track._F @ self.x
        self.P = Track._F @ self.P @ Track._F.T + self.Q
        self.age += 1
        self.time_since_update += 1
        return self.predicted_box()

    def update(self, box):
        z = _box_to_z(box)
        H = Track._H
        y = z - H @ self.x
        S = H @ self.P @ H.T + self.R
        try:
            K = self.P @ H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            return                                   # singular -> skip this update
        self.x = self.x + K @ y
        self.P = (np.eye(7) - K @ H) @ self.P
        self.time_since_update = 0
        self.hits += 1

    def predicted_box(self):
        return _x_to_box(self.x)

    def centroid(self):
        return float(self.x[0]), float(self.x[1])


class MultiTracker:
    """Tracking-by-detection over YOLO person boxes. Predicts every track, then
    associates detections to predictions by IoU (optimal if scipy is present,
    else greedy). Matched tracks are updated; UNMATCHED detections spawn FRESH,
    monotonically-increasing ids that can never reuse a retired id (so a newcomer
    can never inherit the anchored target's id); tracks unseen for > max_coast
    frames are retired. Stamps p['track_id'] on every detection in place."""

    def __init__(self, iou_min=0.2, max_coast=10):
        self.iou_min = float(iou_min)
        self.max_coast = int(max_coast)
        self.tracks = {}        # id -> Track
        self.next_id = 1

    def update(self, persons):
        for t in self.tracks.values():
            t.predict()
        track_ids = list(self.tracks.keys())
        pred_boxes = [self.tracks[i].predicted_box() for i in track_ids]
        det_boxes = [p["box"] for p in persons]

        matches = {}            # det_idx -> track_id
        if track_ids and det_boxes:
            iou = np.zeros((len(det_boxes), len(track_ids)), dtype=np.float64)
            for di, db in enumerate(det_boxes):
                for ti, tb in enumerate(pred_boxes):
                    iou[di, ti] = iou_xyxy(db, tb)
            if _HAVE_LSA:
                rows, cols = _linear_sum_assignment(-iou)
                for di, ti in zip(rows, cols):
                    if iou[di, ti] >= self.iou_min:
                        matches[int(di)] = track_ids[int(ti)]
            else:
                pairs = [(iou[di, ti], di, ti)
                         for di in range(len(det_boxes))
                         for ti in range(len(track_ids))
                         if iou[di, ti] >= self.iou_min]
                pairs.sort(reverse=True)             # highest IoU first
                used_d, used_t = set(), set()
                for _, di, ti in pairs:
                    if di in used_d or ti in used_t:
                        continue
                    used_d.add(di); used_t.add(ti)
                    matches[di] = track_ids[ti]

        for di, p in enumerate(persons):
            if di in matches:
                tid = matches[di]
                self.tracks[tid].update(p["box"])
                p["track_id"] = tid
            else:
                self.tracks[self.next_id] = Track(p["box"], self.next_id)
                p["track_id"] = self.next_id
                self.next_id += 1

        for tid in list(self.tracks.keys()):
            if self.tracks[tid].time_since_update > self.max_coast:
                del self.tracks[tid]
        return self.tracks

    def get(self, tid):
        return self.tracks.get(tid)


# ---------------------------------------------------------------------------
# Stage 2 -- anchored appearance gallery + distractor bank. Slot-0 is the FROZEN
# anchor (immutable, never EMA'd, never evicted, held SEPARATELY from the bounded
# deque so it can never be pushed out). gallery = confirmed same-person views
# (only LOWER cost). distractors = confirmed other-people views (only RAISE cost).
# feat_fn/sim_fn are the ONLY swap point for a Stage-4 embedding backend. Every
# method is crash-safe -> degrade to anchor-only behavior; never kills the loop.
# ---------------------------------------------------------------------------
