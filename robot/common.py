"""Shared helpers + constants for the K1 follow stack (P3.0, IMPLEMENTATION_README.md Phase 3).

Extracted verbatim from follow_person_k1.py so the seam modules (tracking, identity,
perception, triggers, fsm) can import them without a back-import into the monolith. Pure
move, no logic change. The DEF_* argparse-default block stays with parse_args (it is consumed
ONLY there). The mutable stream/logging globals (_STREAM/_MAGIC/_GDBG_LAST + log/emit_frame/gdbg)
land here in a later step.

Owning the S_*/TS_* state constants here also fixes a latent forward-reference: GestureTrigger
reads S_SEARCH/S_REACQUIRE/S_PARKED, which used to be defined ~1200 lines AFTER the class (a
call-time global lookup); as a top-level import they resolve immediately.
"""
import collections
import json
import struct
import sys
import time

import numpy as np
import cv2

# HARD velocity clamps -- the last-resort ceiling the control law is bounded by (the C++ bridge
# clamps again independently; that duplication is intentional defense-in-depth, do not remove).
HARD_VX_LIMIT   = 0.30
HARD_VYAW_LIMIT = 0.40

# Depth freshness tiers (seconds).
DEPTH_WARMUP_S = 15.0   # subscriber-gated spin-up grace before "never seen depth" == DOWN
DEPTH_FRESH_S  = 0.5    # depth age <= this == FRESH (matches latest_depth() default max_age)
DEPTH_DOWN_S   = 2.0    # depth age >  this == DOWN (between FRESH_S and DOWN_S == STALE)

PERSON_CLS    = 0             # COCO person class id
LL_DARK_THRESH = 90.0         # mean luma (0-255) below this -> low-light (parse_args default + low_light_boost)

# One-time lock hint the acquisition triggers hand to the FSM.
LockHint = collections.namedtuple("LockHint", "point owner_box owner_tid source point_kind")

# YOLO-pose keypoint indices (raised-hand gesture test).
KP_L_SHOULDER, KP_R_SHOULDER = 5, 6
KP_L_WRIST, KP_R_WRIST = 9, 10

# Follow FSM states.
S_SEARCH    = "SEARCH_MARKER"
S_TRACK     = "TRACK"
S_REACQUIRE = "REACQUIRE"
S_PARKED    = "PARKED"      # give-up: stood, watching for the marker, with a bounded clean-exit
S_SEARCHING = "SEARCHING"   # DRIVE-only: yaw-only rotate toward the last bearing to re-find a lost target

# Per-target track sub-states.
TS_LOCKED   = "LOCKED"      # anchored person detected + accepted this frame
TS_COASTING = "COASTING"    # briefly occluded -> following the motion prediction
TS_RELOCALIZING = "RELOCALIZING"  # Stage 3: passive appearance re-acquire while STOOD


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


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


def depth_to_meters(msg):
    """Return an (h,w) float32 array of depth in METERS, or None.
    16UC1 is millimeters; 32FC1 is already meters. Zeros = no data."""
    try:
        h, w, enc = msg.height, msg.width, msg.encoding.lower()
        if enc in ("16uc1", "mono16", "z16"):
            d = np.frombuffer(bytes(msg.data), dtype=np.uint16)[: h * w].reshape((h, w))
            return d.astype(np.float32) / 1000.0  # mm -> m
        if enc in ("32fc1", "32f"):
            d = np.frombuffer(bytes(msg.data), dtype=np.float32)[: h * w].reshape((h, w))
            return d.astype(np.float32)
        return None
    except Exception:  # noqa: BLE001 -- never let a bad depth frame matter
        return None


def iou_xyxy(a, b):
    """IoU of two (x1,y1,x2,y2) boxes. 0.0 when disjoint or degenerate."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1); ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0.0 else 0.0


# --- stream routing + logging (P3.0b) -------------------------------------------------------
# --stream routes status text to stderr so stdout carries only the binary annotated-frame protocol
# the Tracker page reads. follow_person_k1 sets common._STREAM = True when --stream is passed (and
# reads common._STREAM where it decides whether to emit frames). Kept as a module global here so a
# single flag is shared by log() and every importer.
_STREAM = False
_MAGIC = b"K1F1"


def log(msg):
    """One flushed status line. In --stream mode it goes to STDERR so stdout
    carries only the binary annotated-frame protocol the Tracker page reads."""
    f = sys.stderr if _STREAM else sys.stdout
    f.write(msg + "\n")
    f.flush()


def emit_frame(status, bgr, quality=70):
    """Write one annotated frame to stdout: MAGIC(4)+status(1)+len(4 BE)+jpeg.
    Same wire protocol as stream_cam.py so the app's frame reader is reused."""
    try:
        ok, jpg = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return
        b = jpg.tobytes()
        out = sys.stdout.buffer
        out.write(_MAGIC + bytes([status & 0xFF]) + struct.pack(">I", len(b)) + b)
        out.flush()
    except Exception:
        pass


# Per-tag-throttled gesture-debug logger (~1 Hz per first-word tag). No-op unless --gesture-debug,
# so it costs one getattr when off. Makes the whole gesture acquisition chain visible on demand.
_GDBG_LAST = {}


def gdbg(args, msg):
    if not getattr(args, "gesture_debug", False):
        return
    tag = msg.split(" ", 1)[0]
    now = time.monotonic()
    if now - _GDBG_LAST.get(tag, 0.0) < 1.0:
        return
    _GDBG_LAST[tag] = now
    log("GDBG " + msg)


class EventLog:
    """Always-on lightweight JSONL of safety-relevant per-tick signals for post-incident forensics
    (P5.3): one compact JSON object per line, LINE-BUFFERED so the last ticks survive a hard crash,
    and it NEVER raises into the control loop. A bad/unwritable path just disables the sink
    (self.f is None -> every tick is a no-op), so it is inert off-robot -- e.g. the replay gate can't
    open the robot path, so decisions stay byte-identical. Cheap: one text line (~10/s), no ROS bag."""
    def __init__(self, path):
        self.f = None
        try:
            if path:
                self.f = open(path, "a", buffering=1)   # line-buffered: each tick durably flushed
        except Exception:  # noqa: BLE001 -- missing dir / read-only fs just disables the sink
            self.f = None

    def tick(self, **fields):
        if self.f is None:
            return
        try:
            self.f.write(json.dumps(fields, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 -- never break the control loop for a log write
            pass

    def close(self):
        try:
            if self.f is not None:
                self.f.flush(); self.f.close()
        except Exception:  # noqa: BLE001
            pass
        self.f = None


# ---- PERF: per-stage cost accounting for the control loop (compute-manager Phase 1) ----------
# MEASUREMENT ONLY. Nothing gates, sheds or degrades on these numbers -- they exist so the shed
# ladder can later be built on measured cost instead of guesswork. The loop already reports a
# TOTAL (LOOP-MS p50/p90/p99); what was missing is ATTRIBUTION: with p50 116 / p99 179 ms against
# a 100 ms budget and three separate ad-hoc auto-disables (Rerun, gesture, ReID watchdog), there
# was no way to know which stage to shed first. Cost per sample is one perf_counter pair plus a
# list append (~1 us), i.e. ~0.005% of a 116 ms frame.
class _StageTimer:
    """Rolling per-stage millisecond costs. Bounded ring per stage; percentiles on demand."""

    __slots__ = ("_ms", "_cap")

    def __init__(self, cap=600):
        self._ms = {}
        self._cap = int(cap)

    def add(self, stage, ms):
        b = self._ms.get(stage)
        if b is None:
            b = self._ms[stage] = []
        b.append(float(ms))
        if len(b) > self._cap:                      # bounded: cannot grow across a long session
            del b[: len(b) - self._cap]

    def summary(self):
        """'stage=p50/p90' for every stage, ORDERED BY p90 DESCENDING -- i.e. read left to right
        as 'what is actually expensive', which is the shed-priority question."""
        rows = []
        for k, b in self._ms.items():
            if not b:
                continue
            s = sorted(b)
            n = len(s)
            rows.append((s[min(n - 1, int(0.9 * n))], k, s[n // 2]))
        rows.sort(reverse=True)
        return " ".join("%s=%.0f/%.0f" % (k, p50, p90) for p90, k, p50 in rows)

    def reset(self):
        self._ms.clear()


PERF = _StageTimer()
