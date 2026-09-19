"""Finger counting for the 2FA lock gesture (crowd mode).

The robot's YOLO11n-POSE model sees 17 BODY joints and no fingers, so a finger countdown
needs a second, HAND keypoint model: a YOLO pose model trained on the ultralytics
`hand-keypoints` dataset (21 landmarks, the MediaPipe layout below). It runs ONLY on a small
crop around the raised wrist that the body pose already located, upscaled so a hand that is
~30 px wide at 2 m becomes ~200 px for the model. Nothing here drives or creates identity: it
returns a finger count (or None) that the sequence trigger consumes.

Fail-closed: no model file -> HandCounter.ok is False and count() is always None, so a
countdown step can never be satisfied without the model (the trigger refuses to construct).
"""
import math
import os
import time

import numpy as np
import cv2

from common import log

# 21-landmark hand layout (MediaPipe / ultralytics hand-keypoints).
WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20
N_HAND_KP = 21

_FINGERS = ((INDEX_MCP, INDEX_PIP, INDEX_TIP), (MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP),
            (RING_MCP, RING_PIP, RING_TIP), (PINKY_MCP, PINKY_PIP, PINKY_TIP))


def _d(lm, i, j):
    return math.hypot(float(lm[i][0]) - float(lm[j][0]), float(lm[i][1]) - float(lm[j][1]))


def count_fingers(lm, kp_conf=0.3):
    """Extended-finger count 0..5 from 21 (x, y, conf) landmarks, or None when the hand is
    not readable. Rotation-invariant: a finger is EXTENDED when its tip is further from the
    wrist than its PIP joint by a margin scaled on the palm (wrist -> middle MCP), so a raised,
    tilted or upside-down hand counts the same. The thumb is extended when its tip is further
    from the pinky MCP than its own MCP is (a thumb folded across the palm fails that). Pure
    function; never raises."""
    try:
        if lm is None or len(lm) < N_HAND_KP:
            return None
        need = (WRIST, MIDDLE_MCP, PINKY_MCP, THUMB_MCP, THUMB_TIP) + sum(_FINGERS, ())
        for i in need:
            if float(lm[i][2]) < kp_conf:
                return None
        palm = _d(lm, WRIST, MIDDLE_MCP)
        if palm <= 1e-3:
            return None
        margin = 0.15 * palm
        n = 0
        for mcp, pip, tip in _FINGERS:
            if _d(lm, WRIST, tip) > _d(lm, WRIST, pip) + margin:
                n += 1
        if _d(lm, THUMB_TIP, PINKY_MCP) > _d(lm, THUMB_MCP, PINKY_MCP) + margin:
            n += 1
        return n
    except Exception:  # noqa: BLE001
        return None


class HandCounter:
    """Runs the hand keypoint model on a wrist-centred crop. ok=False (and count() -> None)
    when the model is missing or fails to load -- callers must treat that as 'no count',
    never as zero fingers."""

    def __init__(self, model_path, kp_conf=0.3, crop_px=256, _model_override=None):
        self.model = None
        self.ok = False
        self.kp_conf = float(kp_conf)
        self.crop_px = int(crop_px)
        self.ms = []          # last inference times (latency instrument, bounded below)
        if _model_override is not None:            # tests only: a fake with .predict()
            self.model = _model_override
            self.ok = True
            return
        path = str(model_path or "")
        log("HAND-MODEL path=%s exists=%s size=%s"
            % (os.path.abspath(path) if path else "", os.path.exists(path),
               (os.path.getsize(path) if os.path.exists(path) else "NA")))
        if not path or not os.path.exists(path):
            return
        try:
            from ultralytics import YOLO
            self.model = YOLO(path, task="pose")
            try:
                _dummy = np.zeros((self.crop_px, self.crop_px, 3), dtype=np.uint8)
                for _ in range(3):
                    self.model.predict(_dummy, verbose=False)
            except Exception:  # noqa: BLE001
                pass
            self.ok = True
            log("HAND-COUNTER ok model=%s crop=%dpx (warmed up)" % (os.path.basename(path), self.crop_px))
        except Exception as e:  # noqa: BLE001
            log("HAND-COUNTER load FAILED (%s) -> countdown steps can never pass" % e)
            self.ok = False

    def roi(self, wrist_xy, scale_px, w_img, h_img):
        """Square crop around the wrist. scale_px ~ the person's shoulder width; the hand sits
        within ~1.2x of that above the wrist, so a 2.4x square centred slightly beyond the wrist
        (away from the elbow is unknown here, so centred) covers open fingers."""
        side = int(max(48.0, min(2.4 * float(scale_px), 0.6 * min(w_img, h_img))))
        cx, cy = float(wrist_xy[0]), float(wrist_xy[1])
        x1 = int(max(0.0, cx - side / 2.0)); y1 = int(max(0.0, cy - side / 2.0))
        x2 = int(min(float(w_img), x1 + side)); y2 = int(min(float(h_img), y1 + side))
        return x1, y1, x2, y2

    def count(self, frame, wrist_xy, scale_px):
        """Finger count of the hand nearest the wrist, or None. Crash-safe."""
        if not self.ok or frame is None:
            return None
        try:
            h_img, w_img = frame.shape[:2]
            x1, y1, x2, y2 = self.roi(wrist_xy, scale_px, w_img, h_img)
            if (x2 - x1) < 16 or (y2 - y1) < 16:
                return None
            crop = frame[y1:y2, x1:x2]
            s = float(self.crop_px) / float(max(x2 - x1, y2 - y1))
            up = cv2.resize(crop, (int((x2 - x1) * s), int((y2 - y1) * s)),
                            interpolation=cv2.INTER_CUBIC) if s > 1.0 else crop
            _t0 = time.monotonic()
            res = self.model.predict(up, verbose=False)
            self.ms.append((time.monotonic() - _t0) * 1000.0)
            if len(self.ms) > 200:
                del self.ms[:-200]
            best, best_dist = None, None
            ccx, ccy = up.shape[1] / 2.0, up.shape[0] / 2.0
            for r in res:
                kpts = getattr(r, "keypoints", None)
                if kpts is None or kpts.data is None:
                    continue
                kd = kpts.data
                for i in range(len(kd)):
                    lm = kd[i]
                    if len(lm) < N_HAND_KP:
                        continue
                    dist = math.hypot(float(lm[WRIST][0]) - ccx, float(lm[WRIST][1]) - ccy)
                    if best is None or dist < best_dist:
                        best, best_dist = lm, dist
            if best is None:
                return None
            return count_fingers(best, self.kp_conf)
        except Exception:  # noqa: BLE001
            return None
