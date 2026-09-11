"""Perception seam (P3.5): pinhole range/bearing helpers, the YOLO PersonDetector, the followed
target point, adaptive low-light boost, and the BEST_EFFORT camera ROS node (CamNode). Extracted
verbatim (pure move). CamNode logs frames/depth to the shared rerun sink (rerun_sink._RR).
"""
import glob
import math
import os
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

def _stamp_wall(msg):
    """ROS `header.stamp` as epoch seconds. Falls back to wall clock if absent/malformed
    (council 2026-09-11 #3 -- never pair RGB/depth on arrival latency)."""
    try:
        st = msg.header.stamp
        return float(st.sec) + 1e-9 * float(st.nanosec)
    except Exception:  # noqa: BLE001 -- defensive: a bad stamp must not kill cam-spin
        return time.time()


def focal_px(w_img, hfov_deg):
    return (w_img / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)


def bearing_from_x(cx, w_img, hfov_deg):
    return math.atan2((cx - w_img / 2.0), focal_px(w_img, hfov_deg))


def range_from_bbox_height(box_h_px, h_img, vfov_deg, person_h_m):
    """Fallback range from apparent person height:
    range ~= person_h_m * focal_px(h_img, vfov_deg) / box_h_px.

    Pass the VERTICAL fov. This used to be handed the horizontal one against the vertical
    pixel count, described as "a fine monocular proxy" -- it is not: that only holds when
    hfov/w == vfov/h, and on this camera (105.8 deg / 544 px vs 94.9 deg / 448 px) reusing
    hfov here understated the focal by ~1.21x on top of whatever hfov itself was wrong by.
    Both FOVs resolve to the same 205.8 px focal, which is the calibrated fx = fy."""
    if box_h_px <= 1:
        return None
    f = focal_px(h_img, vfov_deg)
    return (person_h_m * f) / box_h_px


def pinhole_intrinsics(w_img, h_img, hfov_deg):
    """Derived pinhole intrinsics for the recorded run (P6.1). This rig publishes NO CameraInfo
    (see focal_px above / range_from_bbox_height), so fx is derived from the configured --hfov-deg
    and fy is set equal to it (square-pixel monocular proxy); the principal point is the image
    centre. Marked approximate=True: P8.1 replaces this with a calibrated/CameraInfo intrinsic
    before any RGBD reconstruction trusts it. Returns a plain dict (JSON-serialisable, no numpy)."""
    f = focal_px(w_img, hfov_deg)
    return {
        "model": "pinhole_from_hfov",
        "approximate": True,
        "width": int(w_img),
        "height": int(h_img),
        "hfov_deg": float(hfov_deg),
        "fx": float(f),
        "fy": float(f),
        "cx": w_img / 2.0,
        "cy": h_img / 2.0,
        "note": "fx from --hfov-deg; fy:=fx (no vertical FOV published); calibrate in P8.1",
    }


# ---------------------------------------------------------------------------
# Person detection (YOLO11n ONNX). Loaded ONCE at startup. Never crashes loop.
# ---------------------------------------------------------------------------
YOLO_WARMUP_N = 3   # dummy predicts at construction (mirror ReidEngine's 3) -- see __init__


class PersonDetector:
    def __init__(self, model_path, conf, trt="off", trt_cache=""):
        self.model_path = model_path
        self.conf = conf
        self.model = None
        self.ok = False
        self.ep_active = ""
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path, task="detect")
            # Warm up (P4.2): the FIRST predict lazily builds the CUDA/engine kernels -- a ~0.8s
            # spike that landed in the control loop's first LOOP-MS window at BOTH stock and pinned
            # clocks (docs/LOOP_BASELINE.md, max=1052/826ms). Run a few dummy predicts HERE, at
            # construction (before run()'s loop), so the one-time build cost lands off the control
            # loop. Mirrors ReidEngine + GestureTrigger. Dummy frame -> zero decision effect
            # (byte-identical); tolerant of a model/version that rejects the dummy.
            try:
                _dummy = np.zeros((480, 640, 3), dtype=np.uint8)
                for _ in range(YOLO_WARMUP_N):
                    self.model.predict(_dummy, verbose=False)
            except Exception:  # noqa: BLE001
                pass
            self.ok = True
            self._try_trt(model_path, trt, trt_cache)
        except Exception as e:  # noqa: BLE001
            log("YOLO-LOAD-FAIL %s (%s) -- person detection disabled" % (model_path, e))
            self.ok = False

    def _try_trt(self, model_path, trt, trt_cache):
        """Swap ultralytics' ONNX session for one on the TensorRT EP at FP16.

        WHY A SWAP. ultralytics hardcodes its provider list (nn/autobackend.py ~L249: CPU, with
        CUDA inserted when available) and offers no hook for TensorrtExecutionProvider. Its ONNX
        branch only ever calls self.session.run(output_names, {input_name: im}), so a session
        built from the SAME file with the same I/O names is a drop-in. Measured on this robot
        2026-09-10, this also beats a hand-rolled raw-ORT detector, because ultralytics' letterbox
        is faster than an equivalent numpy one (raw-ORT total 34.7 ms vs ultralytics 33.4 ms) --
        reimplementing pre/post would have made it SLOWER, so we keep ultralytics and change only
        the execution provider.

        MEASURED WIN (real camera frames, 448x544): predict p50 25.4 -> 14.6 ms (1.73x). The raw
        forward alone goes 20.1 -> 7.35 ms (2.51x). Cutting GPU time also frees contention for
        depth and re-ID, so the whole loop benefits, not just this stage.

        NUMERICALLY VERIFIED before shipping, IoU-matched at the follow's operating conf 0.35:
        7/7 detections matched at IoU>=0.5 with none unmatched either way; worst corner delta
        0.29 px (p50 0.19); min matched IoU 0.992; worst confidence delta 0.0039; person-class
        counts identical (3/3). At a harsher conf 0.10 (43 detections) worst delta is 0.92 px.

        WARM CACHE IS A HARD PRECONDITION. A cold engine build measured 574.5 s on this Orin vs
        4.8 s warm. Ten minutes of silence at startup would look like a hang, and a build must
        never contend with a live control loop -- so this REFUSES to build inline and engages only
        when the cache already holds an engine. Populate it as a deploy step. Engines are keyed by
        graph hash + precision + SM arch (..._fp16_sm87.engine), so a changed model, JetPack or GPU
        simply misses the cache and falls back rather than loading a stale engine.

        FAIL-SAFE THROUGHOUT: a missing provider, cold cache, I/O-name mismatch, a session that
        did not actually activate TRT, or any exception leaves ultralytics' own CUDA session in
        place. Detection never degrades to CPU or to nothing because of this path."""
        self.ep_active = "CUDAExecutionProvider(ultralytics-default)"
        if trt != "on":
            return
        try:
            import onnxruntime as ort
            if "TensorrtExecutionProvider" not in ort.get_available_providers():
                log("YOLO-TRT skip: TensorrtExecutionProvider unavailable -> staying on the "
                    "ultralytics CUDA session")
                return
            cache = trt_cache or ""
            if not cache or not os.path.isdir(cache) or not glob.glob(os.path.join(cache, "*.engine")):
                log("YOLO-TRT skip: no cached engine in %r -- refusing to build inline (a cold "
                    "build measured 574s; it must be a deploy step, never a startup stall). "
                    "Staying on the CUDA session." % cache)
                return
            _pred = getattr(self.model, "predictor", None)
            _mdl = getattr(_pred, "model", None) if _pred is not None else None
            old = getattr(_mdl, "session", None) if _mdl is not None else None
            if old is None:
                log("YOLO-TRT skip: ultralytics exposed no .session (warmup may have failed) -> "
                    "staying on the default session")
                return
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            so.log_severity_level = 3
            opts = {"trt_fp16_enable": True, "trt_engine_cache_enable": True,
                    "trt_engine_cache_path": cache}
            t0 = time.monotonic()
            new = ort.InferenceSession(
                model_path, sess_options=so,
                providers=[("TensorrtExecutionProvider", opts),
                           "CUDAExecutionProvider", "CPUExecutionProvider"])
            load_s = time.monotonic() - t0
            # The swap is only sound if ultralytics' cached I/O names still address this session.
            if ([i.name for i in new.get_inputs()] != [i.name for i in old.get_inputs()]
                    or [o.name for o in new.get_outputs()] != [o.name for o in old.get_outputs()]):
                log("YOLO-TRT ABORT: I/O names differ between sessions -> keeping the CUDA "
                    "session (a mismatched swap would feed the wrong tensor)")
                return
            act = list(new.get_providers())
            if not act or act[0] != "TensorrtExecutionProvider":
                log("YOLO-TRT ABORT: session did not activate TRT (active=%s) -> keeping the CUDA "
                    "session" % ",".join(act))
                return
            _mdl.session = new
            self.ep_active = ",".join(act)
            log("YOLO-TRT active fp16 providers=%s cache=%s load=%.1fs (measured 1.73x on real "
                "frames, 25.4->14.6ms predict; boxes agree to 0.29px at conf 0.35)"
                % (self.ep_active, cache, load_s))
            try:                                   # re-warm THROUGH ultralytics on the new session
                _d = np.zeros((448, 544, 3), dtype=np.uint8)
                for _ in range(YOLO_WARMUP_N):
                    self.model.predict(_d, verbose=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001 -- an optimisation must never break detection
            log("YOLO-TRT ERR %s -> staying on the ultralytics CUDA session" % e)

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

    def detect_classes(self, frame, class_ids=None, min_conf=None):
        """Multi-class detect for Plan A class-aware brake. Returns list of
        {box, cx, cy, w, h, conf, cls}. Never raises. Independent of detect()
        (follow stays person-only / byte-identical when class-brake is off)."""
        if not self.ok or frame is None:
            return []
        conf = self.conf if min_conf is None else float(min_conf)
        try:
            kw = dict(conf=conf, verbose=False)
            if class_ids is not None:
                kw["classes"] = list(class_ids)
            try:
                res = self.model.predict(frame, **kw)
            except TypeError:
                res = self.model.predict(frame, conf=conf, verbose=False)
        except Exception:  # noqa: BLE001
            return []
        allow = None if class_ids is None else set(int(c) for c in class_ids)
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
                    if allow is not None and cls not in allow:
                        continue
                    try:
                        c = float(b.conf[0]) if b.conf is not None else 0.0
                    except Exception:  # noqa: BLE001
                        c = 0.0
                    if c < conf:
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
                        "conf": c,
                        "cls": cls,
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
    def __init__(self, topics, depth_topic, odom_topic="", head_pose_topic=""):
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
        # P6.1a: OPTIONAL planar odometry, recording-only (never feeds the control law). Guarded:
        # the Odometer msg lives in the Booster interface workspace (booster_interface.msg), which a
        # plain ssh may not have sourced -- a failed import or subscribe just disables odom recording
        # (latest_odom() stays None) and NEVER stops the follow. Default odom_topic '' = no subscription
        # (byte-identical). The capture profile turns it on for P8 map stitching.
        # HEAD POSE (head-tracking stage 2): OPTIONAL, read-only, and guarded exactly like odom.
        # WHY it matters beyond tracking: the follow derives target bearing from PIXEL offset and
        # assumes image-centre == body-forward. That is only true while the head is centred. Once
        # anything pans the head, every bearing is wrong by the pan angle -- so the pan angle must
        # be OBSERVED, never assumed. Absent topic / failed import -> head_yaw() returns None and
        # the caller fails closed. Default  = no subscription at all (byte-identical).
        self._head_lock = threading.Lock()
        self._head_yaw = None      # radians, +ve = panned toward image-right
        self._head_pitch = None
        self._head_stamp = 0.0
        if head_pose_topic:
            try:
                from geometry_msgs.msg import Pose as _HeadPose
                self.create_subscription(_HeadPose, head_pose_topic, self._head_cb, qos)
            except Exception as e:  # noqa: BLE001 -- absent topic/type -> head pose stays None
                print("HEAD-POSE subscribe failed (%s) -> head yaw unknown (head tracking will refuse)" % e)

        self._odom_lock = threading.Lock()
        self._odom = None          # (x, y, theta) planar pose, or None until first message
        self._odom_stamp = 0.0
        self._odom_count = 0
        if odom_topic:
            try:
                from booster_interface.msg import Odometer
                self.create_subscription(Odometer, odom_topic, self._odom_cb, qos)
            except Exception as e:  # noqa: BLE001 -- msg type/workspace absent -> odom disabled, follow proceeds
                print("ODOM subscribe failed (%s) -> odometry recording disabled (follow proceeds)" % e)
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
            rerun_sink._RR.frame(_seq_now, _stamp_wall(msg))
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
            rerun_sink._RR.frame(self._seq, _stamp_wall(msg))
            rerun_sink._RR.depth("/camera/depth", d, seq=_dcount)

    def _odom_cb(self, msg):
        # P6.1a: stash the latest planar pose + log to the (default-off) Rerun sink. Recording only;
        # wrapped so a malformed message can never kill the cam-spin thread. Booster Odometer is
        # {float32 x, y, theta}; be defensive about the exact field names anyway.
        try:
            x = float(getattr(msg, "x", 0.0))
            y = float(getattr(msg, "y", 0.0))
            th = float(getattr(msg, "theta", getattr(msg, "yaw", 0.0)))
        except Exception:  # noqa: BLE001
            return
        now = time.monotonic()
        with self._odom_lock:
            self._odom = (x, y, th)
            self._odom_stamp = now
            self._odom_count += 1
        if rerun_sink._RR.ok:
            rerun_sink._RR.scalar("/odom/x", x)
            rerun_sink._RR.scalar("/odom/y", y)
            rerun_sink._RR.scalar("/odom/theta", th)

    def _head_cb(self, msg):
        """Stash head yaw/pitch from the quaternion. Never raises: a malformed message must not
        kill the cam-spin thread (same contract as _odom_cb)."""
        try:
            q = msg.orientation
            yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
            _s = max(-1.0, min(1.0, 2.0 * (q.w * q.y - q.z * q.x)))
            pitch = math.asin(_s)
        except Exception:  # noqa: BLE001
            return
        with self._head_lock:
            self._head_yaw = float(yaw)
            self._head_pitch = float(pitch)
            self._head_stamp = time.monotonic()

    def head_yaw(self, max_age=0.5):
        """Head yaw in radians if FRESH within max_age, else None. None means UNKNOWN, and the
        caller must fail closed -- an assumed-zero yaw is exactly the silent-corruption case.
        Same freshness window as depth: a pose older than that cannot be trusted mid-stride."""
        with self._head_lock:
            if self._head_yaw is None:
                return None
            if (time.monotonic() - self._head_stamp) > max_age:
                return None
            return self._head_yaw

    def head_pitch(self, max_age=0.5):
        """Head pitch in radians if fresh, else None (see head_yaw)."""
        with self._head_lock:
            if self._head_pitch is None:
                return None
            if (time.monotonic() - self._head_stamp) > max_age:
                return None
            return self._head_pitch

    def latest_odom(self, max_age=1.0):
        """Latest (x, y, theta) planar pose if fresh within max_age, else None. Odometry publishes
        slower than the camera, so the window is looser than depth's."""
        with self._odom_lock:
            if self._odom is None:
                return None
            if (time.monotonic() - self._odom_stamp) > max_age:
                return None
            return self._odom

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
