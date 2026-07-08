"""Perception seam (P3.5): pinhole range/bearing helpers, the YOLO PersonDetector, the followed
target point, adaptive low-light boost, and the BEST_EFFORT camera ROS node (CamNode). Extracted
verbatim (pure move). CamNode logs frames/depth to the shared rerun sink (rerun_sink._RR).
"""
import math
import threading
import time

import numpy as np
import cv2

from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image

import rerun_sink
from common import (
    PERSON_CLS, LL_DARK_THRESH, DEPTH_WARMUP_S, DEPTH_FRESH_S, DEPTH_DOWN_S,
    log, to_bgr, depth_to_meters,
)

def focal_px(w_img, hfov_deg):
    return (w_img / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def bearing_from_x(cx, w_img, hfov_deg):
    return math.atan2((cx - w_img / 2.0), focal_px(w_img, hfov_deg))


def range_from_bbox_height(box_h_px, h_img, hfov_deg, person_h_m):
    """Fallback range from apparent person height. The camera's vertical FOV
    isn't published here, so we reuse the horizontal focal length (a fine
    monocular proxy): range ~= person_h_m * focal_px / box_h_px."""
    if box_h_px <= 1:
        return None
    f = focal_px(h_img, hfov_deg)
    return (person_h_m * f) / box_h_px


# ---------------------------------------------------------------------------
# Person detection (YOLO11n ONNX). Loaded ONCE at startup. Never crashes loop.
# ---------------------------------------------------------------------------
class PersonDetector:
    def __init__(self, model_path, conf):
        self.model_path = model_path
        self.conf = conf
        self.model = None
        self.ok = False
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path, task="detect")
            self.ok = True
        except Exception as e:  # noqa: BLE001
            log("YOLO-LOAD-FAIL %s (%s) -- person detection disabled" % (model_path, e))
            self.ok = False

    def detect(self, frame):
        """Return list of dicts: {box:(x1,y1,x2,y2), cx, cy, conf, w, h}.
        Person-class only. Never raises."""
        if not self.ok or frame is None:
            return []
        try:
            res = self.model.predict(frame, conf=self.conf, classes=[PERSON_CLS],
                                     verbose=False)
        except TypeError:
            # Older ultralytics signature: filter manually below.
            try:
                res = self.model.predict(frame, conf=self.conf, verbose=False)
            except Exception:  # noqa: BLE001
                return []
        except Exception:  # noqa: BLE001
            return []
        out = []
        try:
            for r in res:
                boxes = getattr(r, "boxes", None)
                if boxes is None:
                    continue
                for b in boxes:
                    try:
                        cls = int(b.cls[0]) if b.cls is not None else -1
                    except Exception:  # noqa: BLE001
                        cls = -1
                    if cls != PERSON_CLS:
                        continue
                    try:
                        conf = float(b.conf[0]) if b.conf is not None else 0.0
                    except Exception:  # noqa: BLE001
                        conf = 0.0
                    if conf < self.conf:
                        continue
                    try:
                        xy = b.xyxy[0].tolist()
                        x1, y1, x2, y2 = [float(v) for v in xy[:4]]
                    except Exception:  # noqa: BLE001
                        continue
                    if x2 <= x1 or y2 <= y1:
                        continue
                    out.append({
                        "box": (x1, y1, x2, y2),
                        "cx": (x1 + x2) * 0.5,
                        "cy": (y1 + y2) * 0.5,
                        "w": (x2 - x1),
                        "h": (y2 - y1),
                        "conf": conf,
                    })
        except Exception:  # noqa: BLE001
            return out
        return out


def target_point_from_box(person):
    """The single 'skeleton centroid' the controller follows. Currently the
    bbox centroid; a pose/skeleton model would override this one function to
    return e.g. the torso/hip midpoint instead. Everything downstream is
    point-based, so the swap is local."""
    return person["cx"], person["cy"]


# ---------------------------------------------------------------------------
# Stage 1 -- pixel-space multi-object tracker. A constant-velocity Kalman filter
# per person in IMAGE space (cx, cy, scale, aspect) -- intentionally NOT metric:
# the camera is monocular with no IMU fusion here, so a metric/world track would
# be untrustworthy (scale ambiguity). The tracker gives every detection a stable
# track_id and a motion prediction so the follower can (a) PREFER the same person
# across frames and (b) COAST through brief occlusions instead of dropping the
# lock. numpy-only (optional scipy for optimal assignment, else greedy). Every
# entry point is wrapped by the caller so a tracker fault degrades to the
# pre-Stage-1 stateless path -- it can never kill the control loop.
# ---------------------------------------------------------------------------
# Track / MultiTracker + box helpers -> tracking.py (P3.2).
# TargetGallery / color+striped descriptors / ReidEngine -> identity.py (P3.3).
# Adaptive low-light enhancement. When a frame is dim, lift shadow detail
# (CLAHE on the luma channel) + a gentle gamma so YOLO/ArUco can still find the
# subject. Auto-gated by mean brightness, so a normally-lit frame passes through
# untouched. A few ms/frame on the Jetson; never raises.
# ---------------------------------------------------------------------------
LL_CLAHE_CLIP  = 2.5
LL_GAMMA       = 0.7       # < 1 brightens midtones / shadows
_ll_clahe = None
_ll_gamma_lut = np.array([((i / 255.0) ** LL_GAMMA) * 255 for i in range(256)],
                         dtype=np.uint8)


def low_light_boost(bgr, on=True, thresh=LL_DARK_THRESH):
    """Return an enhanced BGR frame when the scene is dark, else the input
    unchanged. Preserves color (works on the luma channel only). Never raises."""
    if not on or bgr is None:
        return bgr
    try:
        ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        y = ycrcb[:, :, 0]
        if float(y.mean()) >= thresh:
            return bgr                       # bright enough -> leave it alone
        global _ll_clahe
        if _ll_clahe is None:
            _ll_clahe = cv2.createCLAHE(clipLimit=LL_CLAHE_CLIP, tileGridSize=(8, 8))
        y = _ll_clahe.apply(y)               # local-contrast lift (recovers shadows)
        y = cv2.LUT(y, _ll_gamma_lut)        # gentle gamma brighten
        ycrcb[:, :, 0] = y
        return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
    except Exception:                        # noqa: BLE001 -- never break the loop
        return bgr


# Bridge (the loco_follow_bridge process wrapper) -> bridge.py (P3.1).


# ---------------------------------------------------------------------------
# ROS node -- BEST_EFFORT camera (both head topics, deduped) + depth.
# ---------------------------------------------------------------------------
class CamNode(Node):
    def __init__(self, topics, depth_topic):
        super().__init__("k1_follow_person")
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.VOLATILE,
        )
        # The K1 camera is flaky about WHICH topic it publishes on (sometimes
        # /head/rgb, sometimes /head/raw/rgb), so subscribe to all candidates and
        # process whichever delivers a frame.
        for t in topics:
            self.create_subscription(Image, t, self._cb, qos)
        if depth_topic:
            self.create_subscription(Image, depth_topic, self._depth_cb, qos)
        self._lock = threading.Lock()
        self._latest = None        # newest BGR frame
        self._stamp = 0.0          # monotonic time it arrived
        self._seq = 0              # increments on every NEW frame
        self.frames_total = 0
        self._depth_lock = threading.Lock()
        self._depth = None         # newest depth-in-meters array
        self._depth_stamp = 0.0
        self._depth_count = 0      # total depth frames received (0 == never seen -> still WARMING)
        self._depth_fps = 0.0      # EMA of depth publish rate, for health + diagnosis
        # ITEM 7(c): RGB fps EMA, mirroring the depth fps above -- for the DRIVE precondition (d)
        # and observability. Stamped under _lock in _cb. 0 == no RGB frame pair seen yet.
        self._rgb_fps = 0.0
        self._t0 = time.monotonic()  # node start, for the depth warm-up grace window

    def _cb(self, msg):
        try:
            bgr = to_bgr(msg)
        except Exception:  # noqa: BLE001 -- never let a bad frame kill the node
            return
        if bgr is None:
            return
        with self._lock:
            # ITEM 7(c): RGB fps EMA (same 0.8/0.2 smoothing as the depth EMA in _depth_cb).
            # BOTH RGB topics (raw/rgb + rgb) deliver the SAME camera frames into this one callback,
            # so each frame can arrive twice ~ms apart; without a dt floor the EMA blends those
            # inter-pair bursts (inst ~200-500) with real frame gaps and reads 10x high (observed
            # 130-203 on a 13fps camera). Ignore dt < 20ms so the EMA tracks UNIQUE-frame rate
            # (a real camera never exceeds ~50fps here; twin-topic pairs are always < 20ms apart).
            _now = time.monotonic()
            if self._stamp > 0.0:
                _dt = _now - self._stamp
                if _dt >= 0.02:
                    _inst = 1.0 / _dt
                    self._rgb_fps = _inst if self._rgb_fps <= 0.0 else (0.8 * self._rgb_fps + 0.2 * _inst)
            self._latest = bgr
            self._stamp = _now
            self._seq += 1
            self.frames_total += 1
            _seq_now = self._seq
        # Rerun RGB (Phase 2): log on THIS (cam-spin) thread, OUTSIDE the lock so the control loop's
        # take_if_new never waits on the batcher. sink.image() COPIES (BGR->RGB) + decimates 1/N, so
        # it never aliases self._latest. Inert unless --rerun (rerun_sink._RR.ok False -> single branch skip).
        if rerun_sink._RR.ok:
            rerun_sink._RR.frame(_seq_now, time.time())
            rerun_sink._RR.image("/camera/rgb", bgr, seq=_seq_now)

    def _depth_cb(self, msg):
        d = depth_to_meters(msg)
        if d is None:
            return
        now = time.monotonic()
        with self._depth_lock:
            if self._depth_stamp > 0.0:
                dt = now - self._depth_stamp
                if dt > 0.0:
                    inst = 1.0 / dt
                    self._depth_fps = inst if self._depth_fps <= 0.0 else (0.8 * self._depth_fps + 0.2 * inst)
            self._depth = d
            self._depth_stamp = now
            self._depth_count += 1
            _dcount = self._depth_count
        # Rerun depth (Phase 2): cam-spin thread, OUTSIDE the lock; sink.depth() COPIES + casts +
        # decimates by depth count, never aliasing self._depth (which the control loop reads under
        # _depth_lock). frame_idx keyed best-effort to the current RGB seq (atomic int read).
        if rerun_sink._RR.ok:
            rerun_sink._RR.frame(self._seq, time.time())
            rerun_sink._RR.depth("/camera/depth", d, seq=_dcount)

    def take_if_new(self, last_seq):
        with self._lock:
            if self._seq != last_seq and self._latest is not None:
                return self._latest, self._seq, self._stamp
            return None, last_seq, self._stamp

    def latest_depth(self, max_age=0.5):
        with self._depth_lock:
            if self._depth is None:
                return None
            if (time.monotonic() - self._depth_stamp) > max_age:
                return None
            return self._depth

    def depth_health(self, now=None):
        """Coarse depth liveness for the safe-floor + operator badge + diagnosis.
        Returns (state, fps). WARMING = subscriber-gated ~10-15s spin-up, never seen
        depth yet (NOT a fault). FRESH = publishing. STALE = a short gap. DOWN = no
        depth -> follow must not drive forward on the bbox-height pinhole. Pure
        arithmetic on the already-stamped depth state; safe to call every tick."""
        now = now if now is not None else time.monotonic()
        with self._depth_lock:
            stamp = self._depth_stamp
            count = self._depth_count
            fps = self._depth_fps
            t0 = self._t0
        if count == 0:
            return ("WARMING" if (now - t0) < DEPTH_WARMUP_S else "DOWN"), fps
        age = now - stamp
        if age <= DEPTH_FRESH_S:
            return "FRESH", fps
        if age <= DEPTH_DOWN_S:
            return "STALE", fps
        return "DOWN", fps

    def rgb_fps(self):
        """ITEM 7(c): current RGB publish-rate EMA (0.0 until a frame pair is seen)."""
        with self._lock:
            return self._rgb_fps

    def rgb_stamp(self):
        """ITEM 7(b): monotonic time the newest RGB frame arrived (0.0 == none yet)."""
        with self._lock:
            return self._stamp

    def depth_stamp(self):
        """ITEM 7(b): monotonic time the newest depth frame arrived (0.0 == none yet)."""
        with self._depth_lock:
            return self._depth_stamp


# Stage 1 sub-states of S_TRACK, carried on Seed.track_state. The node-level
# state stays S_SEARCH / S_TRACK / S_REACQUIRE; these only refine what the
# tracker is doing while we hold the lock inside S_TRACK.
# TS_LOCKED / TS_COASTING / TS_RELOCALIZING now live in common.py (P3.0).


# ---------------------------------------------------------------------------
# Seed -- the locked target's identity. Created once at SEEDED, updated in TRACK.
# ---------------------------------------------------------------------------
