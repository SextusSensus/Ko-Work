import sys, time, struct
import numpy as np
import cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image

TOPIC   = sys.argv[1] if len(sys.argv) > 1 else "/boostercamera/head/rgb"
FPS     = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
QUALITY = int(sys.argv[3]) if len(sys.argv) > 3 else 70
MAGIC   = b"K1F1"
out = sys.stdout.buffer

# --- ArUco lock-on detection (overlay + status byte) ------------------------
# Status byte sent with every frame: 0 = no marker, 1 = acquiring, 2 = LOCKED.
# "Locked" = the DICT_4X4_50 marker seen on LOCK_FRAMES consecutive frames.
ARUCO_DICT    = cv2.aruco.DICT_4X4_50
LOCK_FRAMES   = 3
MARKER_SIZE_M = 0.18
HFOV_DEG      = 70.0

_adict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
try:
    _det = cv2.aruco.ArucoDetector(_adict, cv2.aruco.DetectorParameters())
    def _detect(gray):
        c, ids, _ = _det.detectMarkers(gray); return c, ids
except AttributeError:
    def _detect(gray):
        c, ids, _ = cv2.aruco.detectMarkers(gray, _adict); return c, ids

_streak = 0

def _range_bearing(corners, w_img):
    c = corners.reshape(4, 2)
    cx = c[:, 0].mean()
    focal = (w_img / 2.0) / np.tan(np.radians(HFOV_DEG) / 2.0)
    side = (np.linalg.norm(c[0] - c[1]) + np.linalg.norm(c[2] - c[3])) / 2.0
    rng = (MARKER_SIZE_M * focal) / max(side, 1.0)
    bearing = np.degrees(np.arctan2((cx - w_img / 2.0), focal))
    return rng, bearing

def annotate(bgr):
    """Detect marker, draw lock overlay, return (status, annotated_bgr)."""
    global _streak
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    corners, ids = _detect(gray)
    found = ids is not None and len(ids) > 0
    if found:
        _streak = min(_streak + 1, 999)
    else:
        _streak = 0
    locked = found and _streak >= LOCK_FRAMES
    status = 2 if locked else (1 if found else 0)

    # center reticle
    cx0, cy0 = w // 2, h // 2
    cv2.line(bgr, (cx0 - 12, cy0), (cx0 + 12, cy0), (200, 200, 200), 1)
    cv2.line(bgr, (cx0, cy0 - 12), (cx0, cy0 + 12), (200, 200, 200), 1)

    if found:
        col = (0, 255, 0) if locked else (0, 165, 255)  # green locked / amber acquiring
        pts = corners[0].reshape(4, 2).astype(int)
        cv2.polylines(bgr, [pts], True, col, 3)
        mcx, mcy = int(pts[:, 0].mean()), int(pts[:, 1].mean())
        cv2.circle(bgr, (mcx, mcy), 5, col, -1)
        try:
            rng, bear = _range_bearing(corners[0], w)
            cv2.putText(bgr, "range %.2fm  bearing %+.0fdeg" % (rng, bear),
                        (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2, cv2.LINE_AA)
        except Exception:
            pass

    # status banner
    if status == 2:
        bg, txt = (0, 150, 0), "LOCKED - READY TO FOLLOW"
    elif status == 1:
        bg, txt = (0, 120, 200), "ACQUIRING... (%d/%d)" % (_streak, LOCK_FRAMES)
    else:
        bg, txt = (40, 40, 40), "NO MARKER"
    cv2.rectangle(bgr, (0, 0), (w, 30), bg, -1)
    cv2.putText(bgr, txt, (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                (255, 255, 255), 2, cv2.LINE_AA)
    return status, bgr


def to_bgr(msg):
    h, w, enc, step = msg.height, msg.width, msg.encoding.lower(), msg.step
    buf = np.frombuffer(bytes(msg.data), dtype=np.uint8)
    if enc == "nv12":
        yuv = buf[: (h * 3 // 2) * w].reshape((h * 3 // 2, w))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
    if enc in ("yuv420", "i420"):
        yuv = buf[: (h * 3 // 2) * w].reshape((h * 3 // 2, w))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    ch = step // w if w else 3
    img = buf[: h * step].reshape((h, step))[:, : w * ch].reshape((h, w, ch))
    if enc == "rgb8":  return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if enc == "rgba8": return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    if enc == "bgra8": return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if enc in ("mono8", "8uc1"): return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img

# ---------------------------------------------------------------------------
# Adaptive low-light enhancement (CLAHE on luma + gentle gamma). Auto-gated by
# mean brightness so a normally-lit frame is untouched. Never raises.
# ---------------------------------------------------------------------------
LL_DARK_THRESH = 90.0
LL_CLAHE_CLIP  = 2.5
LL_GAMMA       = 0.7
_ll_clahe = None
_ll_gamma_lut = np.array([((i / 255.0) ** LL_GAMMA) * 255 for i in range(256)],
                         dtype=np.uint8)


def low_light_boost(bgr, on=True, thresh=LL_DARK_THRESH):
    """Enhanced BGR when the scene is dark, else the input unchanged (color preserved)."""
    if not on or bgr is None:
        return bgr
    try:
        ycrcb = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        y = ycrcb[:, :, 0]
        if float(y.mean()) >= thresh:
            return bgr
        global _ll_clahe
        if _ll_clahe is None:
            _ll_clahe = cv2.createCLAHE(clipLimit=LL_CLAHE_CLIP, tileGridSize=(8, 8))
        y = _ll_clahe.apply(y)
        y = cv2.LUT(y, _ll_gamma_lut)
        ycrcb[:, :, 0] = y
        return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
    except Exception:
        return bgr


class Streamer(Node):
    def __init__(self):
        super().__init__("k1_cam_stream")
        qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                         history=HistoryPolicy.KEEP_LAST, depth=1,
                         durability=DurabilityPolicy.VOLATILE)
        # camera is flaky about which topic it uses; subscribe to all candidates.
        topics = list(dict.fromkeys([TOPIC, "/boostercamera/head/raw/rgb", "/boostercamera/head/rgb"]))
        for t in topics:
            self.create_subscription(Image, t, self.cb, qos)
        self.last = 0.0
        self.interval = 1.0 / max(1.0, FPS)
    def cb(self, msg):
        now = time.time()
        if now - self.last < self.interval:
            return
        self.last = now
        try:
            bgr = to_bgr(msg)
            bgr = low_light_boost(bgr)
            status, bgr = annotate(bgr)
            ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
            if not ok:
                return
            b = jpg.tobytes()
            # Protocol: MAGIC(4) + status(1) + length(4, big-endian) + jpeg
            out.write(MAGIC + bytes([status]) + struct.pack(">I", len(b)) + b)
            out.flush()
        except Exception:
            pass

rclpy.init()
n = Streamer()
try:
    rclpy.spin(n)
except KeyboardInterrupt:
    pass
rclpy.shutdown()
