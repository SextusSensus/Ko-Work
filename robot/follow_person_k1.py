#!/usr/bin/env python3
"""
follow_person_k1.py  -- runs ON the Booster K1 robot.

LOCK-AND-HANDOFF follow. The ArUco marker is a ONE-TIME trigger that says
"lock onto the human standing at this marker"; thereafter the robot follows
THAT PERSON markerlessly via YOLO person detection (bbox centroid + a color
signature). The marker is the re-seed / recovery mechanism, NOT a leash.

State machine (single rclpy node, ~10Hz loop, spin_once):
  SEARCH_MARKER : run ArUco + YOLO each frame. No motion. Waiting for a stable
                  marker lock to seed a target.
  SEEDED        : (one-shot) marker stable on >=SEED_FRAMES consecutive frames ->
                  pick the person whose bbox best contains/overlaps the marker
                  centroid (marker pixel inside a person bbox, else nearest
                  person centroid within a threshold). Record the SEED: bbox,
                  HS color histogram, last centroid. The marker is now DONE for
                  normal operation.
  TRACK         : associate the target each frame by
                  cost = w1*centroid_dist(norm) + w2*(1-hist_sim) [+ w3*size].
                  Pick lowest-cost person under a gate; follow its centroid:
                  bearing from centroid_x (pinhole), range from depth-at-centroid
                  (median of a small ROI ignoring zeros) else bbox-height pinhole.
                  Same control law + HARD clamps used throughout.
  REACQUIRE     : no person under the gate for LOST_GRACE frames -> stop (hold).
                  Keep YOLO running. RE-SEED only if the marker is shown again
                  (>=SEED_FRAMES) -> back to TRACK. After --reacquire-timeout with
                  no marker, stay stopped and keep asking for the marker. Never
                  silently re-lock onto a different person by appearance alone
                  after a hard loss.

Modes (argparse):
  --preview  (DEFAULT)  detect + print only. NEVER drives. No bridge spawned.
  --drive               spawn ./loco_follow_bridge, ping/prep/walk, then stream
                        'v vx vy vyaw' with HARD-clamped velocities.

SAFETY (drive mode):
  * 'ping' must return OK before anything moves (verifies loco WITHOUT walking).
  * Need at least one real camera frame before prep->walk.
  * prep -> wait ~3s -> walk -> wait ~2s, then per-frame velocity stream.
  * Target lost (LOST_GRACE frames) -> 'stop' (bridge issues MoveCommand 0,0,0).
  * No NEW frame for > --stall-seconds (~1s) -> 'stop'.
  * --max-seconds session watchdog -> auto 'stop' + clean shutdown.
  * If NO frame ever arrives (idle camera) we stay in a safe NO-FRAME loop and
    NEVER enter walk.
  * SIGINT / SIGTERM / any exception / EOF -> 'stop' then 'quit' to bridge,
    join, exit. The bridge itself does MoveCommand(0,0,0)+ChangeMode(kPrepare)
    on its own exit, so loco is always left safe.

Pose-model forward-compat: the tracker follows a single "skeleton centroid"
(currently the person bbox centroid). target_point_from_box() is the only place
that derives that point, so a pose/skeleton model can swap in later with no
change to the control law or state machine.

Env (caller sources these before python3):
    source /opt/ros/humble/setup.bash
    source /opt/booster/BoosterRos2/install/setup.bash

Every printed line is flushed so the K1Finder app log updates live.
"""

import os
import sys
import time
import math
import signal
import struct
import argparse
import subprocess
import threading
import collections

import numpy as np
import cv2
import yaml

import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from sensor_msgs.msg import Image

# P3.0: shared helpers + constants live in common.py (a sibling module -- /home/booster/common.py
# on the robot; the eval harness adds the node dir to sys.path). Pure move, no logic change.
import common  # noqa: E402  (module handle for the mutable common._STREAM flag, set on --stream)
import rerun_sink  # noqa: E402  (P3.4: module handle -- rerun_sink._RR is rebound by init_rerun)
from common import (  # noqa: E402
    HARD_VX_LIMIT, HARD_VYAW_LIMIT, DEPTH_WARMUP_S, DEPTH_FRESH_S, DEPTH_DOWN_S, PERSON_CLS, LL_DARK_THRESH,
    LockHint, KP_L_SHOULDER, KP_R_SHOULDER, KP_L_WRIST, KP_R_WRIST,
    S_SEARCH, S_TRACK, S_REACQUIRE, S_PARKED, S_SEARCHING, TS_LOCKED, TS_COASTING, TS_RELOCALIZING,
    clamp, to_bgr, depth_to_meters, iou_xyxy, log, emit_frame, gdbg,
)
from bridge import Bridge  # noqa: E402  (P3.1: loco_follow_bridge process wrapper)
from tracking import MultiTracker, max_iou_other, _point_box_dist  # noqa: E402  (P3.2)
from identity import (  # noqa: E402  (P3.3)
    TargetGallery, ReidEngine, color_hist, hist_similarity, striped_feat, striped_sim,
)
from perception import (  # noqa: E402  (P3.5)
    PersonDetector, CamNode, bearing_from_x, range_from_bbox_height, target_point_from_box, low_light_boost,
)


# ---------------------------------------------------------------------------
# TUNABLES (control law + person-track additions)
# ---------------------------------------------------------------------------
DEF_STANDOFF_M    = 1.2       # how far behind the target the robot holds
DEF_DEADBAND_M    = 0.20      # don't fidget within +/- this of standoff
DEF_HFOV_DEG      = 70.0      # K1 head camera horizontal FOV

DEF_K_YAW = 0.9               # turn gain  (rad/s per rad of bearing error)
DEF_K_VX  = 0.35              # speed gain (m/s per m of range error)

# HARD clamps -- intentionally smaller than the bridge's own ceilings.
DEF_VX_MIN, DEF_VX_MAX     = -0.06, 0.18
DEF_VYAW_MIN, DEF_VYAW_MAX = -0.30, 0.30
# HARD_VX_LIMIT / HARD_VYAW_LIMIT (the absolute ceilings) now live in common.py.
# Accel limiter (slew). Max change in commanded velocity PER CONTROL TICK -- the
# command may not step 0->max in one frame (humanoid balance, esp. the first frames
# after a relock/resume where vx/vyaw would otherwise jump straight to the clamp).
# Applied BEFORE the HARD clamps (never widens them). At DEF_RATE_HZ=10 these reach
# full vx (0.18) in ~3 ticks (~0.3s) and full vyaw (0.30) in ~3 ticks. <=0 disables.
DEF_VX_SLEW   = 0.06
DEF_VYAW_SLEW = 0.10
# Close-range floor (m). Non-zero by DEFAULT so the depth-validated geofence + the
# hard forward-vx backoff are ALWAYS ON: even a correct close reading backs the robot
# off. Acts ONLY on a VALIDATED depth range (rsrc=='depth'); a bbox-height guess can
# never trip it. 0 restores today's disabled behaviour byte-for-byte.
DEF_MIN_SAFE_RANGE = 0.6

# Depth-health thresholds (DEPTH_WARMUP_S/FRESH_S/DOWN_S) now live in common.py.
DEF_LOST_GRACE     = 8        # frames with no gated person before we stop+REACQUIRE
DEF_RATE_HZ        = 10.0
DEF_STALL_SECONDS  = 1.0      # no NEW frame for this long -> stop
DEF_MAX_SECONDS    = 120.0    # session watchdog
DEF_REACQUIRE_TIMEOUT = 30.0  # in REACQUIRE, seconds with no re-seed marker
                              # before we just keep asking for the marker (stay stopped)
DEF_TOPIC          = "/boostercamera/head/raw/rgb"

# --- YOLO person detection -------------------------------------------------
DEF_YOLO_PATH = "/opt/booster/BoosterFaceDetection/src/detection/yolo11n.onnx"
DEF_CONF      = 0.35          # PERSON_CLS now lives in common.py

# --- ArUco one-shot seed trigger ------------------------------------------
ARUCO_DICT    = cv2.aruco.DICT_4X4_50
DEF_SEED_FRAMES = 3           # consecutive marker frames for a stable lock

# --- target association weights / gate ------------------------------------
DEF_W_CENTROID = 1.0          # weight on normalized centroid distance
DEF_W_COLOR    = 1.0          # weight on (1 - color hist similarity)
DEF_W_SIZE     = 0.3          # weight on bbox-size inconsistency
DEF_GATE       = 0.85         # max acceptable association cost
DEF_SEED_NEAR_FRAC = 0.25     # marker->person centroid gate at seed (frac of img diag)
DEF_COLOR_EMA  = 0.05         # slow EMA toward current appearance (high-conf only)
DEF_HICONF     = 0.55         # only update appearance when assoc this confident

# --- range fallback (pinhole on person height) ----------------------------
DEF_PERSON_H_M = 1.7          # assumed standing person height for bbox-height range


# _STREAM / _MAGIC -> common.py (P3.0b). follow_person sets/reads common._STREAM.

# rerun _RR sink setup -> rerun_sink.py (P3.4).


# log() + emit_frame() -> common.py (P3.0b)


# clamp() -> common.py (P3.0)


# _GDBG_LAST + gdbg() -> common.py (P3.0b)


# NV12 -> BGR: to_bgr() -> common.py (P3.0; shared with stream_cam.py)


# Depth image -> float meters array: depth_to_meters() -> common.py (P3.0)


# ---------------------------------------------------------------------------
# ArUco detector (works across cv2 >=4.7 and older API). Verbatim approach.
# ---------------------------------------------------------------------------
_adict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
try:
    _detector = cv2.aruco.ArucoDetector(_adict, cv2.aruco.DetectorParameters())  # >=4.7

    def detect_markers(gray):
        corners, ids, _ = _detector.detectMarkers(gray)
        return corners, ids
except AttributeError:
    def detect_markers(gray):
        corners, ids, _ = cv2.aruco.detectMarkers(gray, _adict)                  # older
        return corners, ids


def marker_center(frame):
    """Return (mx, my) pixel center of the first marker, or None. Defensive."""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids = detect_markers(gray)
        if ids is None or len(ids) == 0:
            return None
        c = corners[0].reshape(4, 2)
        return float(c[:, 0].mean()), float(c[:, 1].mean())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# LOCK TRIGGER abstraction. A lock trigger is the ACQUISITION primitive: each
# frame it returns an optional LockHint -- a lock POINT (the role mc plays at the
# marker-detection site today) plus, for gesture, the OWNER it points at (box +
# tracker id, so _try_seed never has to re-derive the person from an anonymous
# point). A trigger NEVER creates identity: it produces a point/owner that the
# single funnel _try_seed consumes. ArUco is the DEFAULT and stays byte-identical;
# gesture is opt-in (--lock-trigger). A trigger fault degrades to "no lock this
# frame", never to motion (fall risk stays perception-side). See
# docs/SCOPE_lock_trigger_gesture.md for the full design + safety rationale.
# ---------------------------------------------------------------------------
# LockHint + the COCO-17 KP_* keypoint indices now live in common.py (P3.0).


class LockTrigger:
    """Base trigger. Produces a lock point only; never seeds, never drives."""
    name = "base"
    ok = True                 # False -> disabled; the loop then produces no lock point
    ran_inference = False     # True if this trigger ran a (heavy) model THIS tick

    def detect(self, frame, persons, state, w_img, h_img):
        return None

    def on_overrun(self):
        """Loop calls this after this trigger's inference ran over the loop budget for
        too many consecutive frames. Base = no-op (nothing heavy to disable)."""
        return


class ArucoTrigger(LockTrigger):
    """The marker trigger == today's marker_center() call, verbatim. The DEFAULT.
    Pure OpenCV, no model load, so it never overruns and never disables."""
    name = "aruco"

    def detect(self, frame, persons, state, w_img, h_img):
        mc = marker_center(frame)
        if mc is None:
            return None
        return LockHint(mc, None, None, "aruco", "marker")


class GestureTrigger(LockTrigger):
    """Raised-hand acquisition via a YOLO11n-POSE model (a SECOND neural model --
    loaded ONLY when --lock-trigger != aruco; see Follower.__init__). A person is a
    'raiser' when one wrist keypoint is above the same-side shoulder by >= margin,
    both keypoints confident. The raise must HOLD --gesture-hold consecutive frames,
    counted PER tracker track_id (NOT a bespoke association map -- it rides the
    validated Stage-1 tracker id stamped on each person). >=2 simultaneous confirmed
    raisers -> REFUSE (never guess). The returned lock point is the owner-box lower-
    centre; the authoritative owner (box + track_id) rides in the LockHint so _try_seed
    selects the SAME person the pose model saw, not a re-derived anonymous point.

    Runs ONLY in acquisition states (SEARCH/SEARCHING/REACQUIRE/PARKED); NEVER in
    S_TRACK (a 2nd model per frame is unaffordable on the saturated Orin AND there is
    no gesture re-seed of an active lock by construction). Crash-safe: any load/infer
    fault -> ok=False / None, the loop degrades to no-lock. every_n>1 decimates the
    pose inference (audit cadence) so it can never starve the live ArUco control loop."""
    name = "gesture"

    def __init__(self, model_path, args, every_n=1):
        self.a = args
        # The gesture hold rides the EXISTING tracker id stamped on each person (p['track_id']
        # at tracker.update) -- NOT a bespoke association map -- so no tracker handle is needed
        # here (SCOPE §2.2). Argparse refuses gesture together with --no-track.
        self.every_n = max(1, int(every_n))
        self.model = None
        self.ok = False
        self.ran_inference = False
        self.holds = {}            # track_id -> confirmed-raise frames (with miss forgiveness)
        self._hold_since = {}      # Phase 2.1: track_id -> monotonic time of the FIRST raise of the dwell
        self._miss = {}            # track_id -> consecutive missed pose frames (field fix: one
                                   # blurry/low-conf frame used to HARD-RESET the whole hold)
        self._tick = 0
        self._cmd_tick = 0         # separate decimation counter for the in-follow STOP gesture
        self._stop_streak = 0      # consecutive STOP-gesture checks the seed held both hands up
        self._ms = collections.deque(maxlen=200)   # rolling pose-inference ms (latency instrument)
        self._ms_log_last = 0.0
        self._head_logged = False  # one-time model-head shape log (detect-vs-pose export check)
        self._last_pose_dets = 0   # #pose detections last inference (for the GDBG FRAME line)
        # Always log the model path + existence -- the #1 silent failure is a wrong/missing file
        # (load fails -> ArUco fallback). Visible in the app once stderr is captured.
        log("GESTURE-MODEL path=%s exists=%s size=%s"
            % (os.path.abspath(str(model_path)), os.path.exists(str(model_path)),
               (os.path.getsize(model_path) if os.path.exists(str(model_path)) else "NA")))
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path, task="pose")
            # Warm up -- the FIRST inference is slow (alloc / JIT / TRT engine load); without this
            # the first gesture frame can trip the overrun-disable or stall acquisition. Mirrors
            # ReidEngine's warmup (policy-engineer: always warm up before going live). One-time
            # cost at load, before any motion. Tolerant of a model that rejects a dummy frame.
            try:
                _dummy = np.zeros((480, 640, 3), dtype=np.uint8)
                for _ in range(3):
                    self.model.predict(_dummy, verbose=False)
            except Exception:  # noqa: BLE001
                pass
            self.ok = True
            log("GESTURE-TRIGGER ok model=%s hold=%d kp-conf=%.2f every-n=%d (warmed up)"
                % (os.path.basename(str(model_path)), int(args.gesture_hold),
                   float(args.gesture_kp_conf), self.every_n))
        except Exception as e:  # noqa: BLE001 -- any failure -> caller falls back to ArUco
            log("GESTURE-TRIGGER load FAILED (%s) -> gesture disabled" % e)
            self.ok = False

    def on_overrun(self):
        if self.ok:
            self.ok = False
            log("GESTURE-DISABLED-SLOW: pose inference over loop budget %d frames -> gesture "
                "trigger disabled (degrade before the C++ staleness watchdog must safe loco)"
                % int(self.a.gesture_overrun_frames))

    def _log_latency(self):
        """Periodic rolling pose-inference p50/p99 (policy-engineer: measure end-to-end p99 in
        deploy power mode). Satisfies the on-Orin measurement precondition from a preview run --
        no separate harness. Pose runs ONLY while stationary, so this is acquisition-responsiveness
        + auto-disable tuning, NOT a control deadline (no control command is pending while it runs)."""
        now = time.monotonic()
        if (now - self._ms_log_last) < 5.0 or len(self._ms) < 5:
            return
        self._ms_log_last = now
        s = sorted(self._ms); n = len(s)
        log("GESTURE-MS p50=%.0f p99=%.0f n=%d every-n=%d -- pose inference (stationary states "
            "only; not a control-loop deadline)" % (s[n // 2], s[min(n - 1, int(n * 0.99))], n,
                                                    self.every_n))

    def _is_raised(self, k, margin):
        """One wrist above the same-side shoulder by >= margin, both kp confident.
        Image y grows downward so 'above' == smaller y. Never raises."""
        try:
            kpc = float(self.a.gesture_kp_conf)

            def ok(idx):
                return float(k[idx][2]) >= kpc

            def y(idx):
                return float(k[idx][1])

            left = ok(KP_L_WRIST) and ok(KP_L_SHOULDER) and y(KP_L_WRIST) < y(KP_L_SHOULDER) - margin
            right = ok(KP_R_WRIST) and ok(KP_R_SHOULDER) and y(KP_R_WRIST) < y(KP_R_SHOULDER) - margin
            return bool(left or right)
        except Exception:  # noqa: BLE001
            return False

    def _arm_owned(self, k, box, margin):
        """OWNER-AUTHORITY (hardened 2026-07-05, wrong-person lock): the raised arm must belong to the
        MATCHED body, not an overlapping neighbour. IoU box-overlap alone let a raiser's arm bind to a
        bystander who never raised a hand. Require: both CONFIDENT shoulders inside the matched box
        (the torso is this tracked person's) AND the RAISED wrist's X within the body column (the arm
        rises from this body, not reaching in from the side). Never raises -> False on any fault."""
        try:
            kpc = float(self.a.gesture_kp_conf)
            x1, y1, x2, y2 = box
            padx = 0.12 * max(x2 - x1, 1.0)
            def conf(idx): return float(k[idx][2]) >= kpc
            def inx(idx): return (x1 - padx) <= float(k[idx][0]) <= (x2 + padx)
            def iny(idx): return (y1 - 1.0) <= float(k[idx][1]) <= (y2 + 1.0)
            def yv(idx): return float(k[idx][1])
            # torso ownership: every CONFIDENT shoulder must sit inside the matched box
            for sh in (KP_L_SHOULDER, KP_R_SHOULDER):
                if conf(sh) and not (inx(sh) and iny(sh)):
                    return False
            # the RAISED wrist(s) must be in the body column (X inside the box, with pad)
            lr = conf(KP_L_WRIST) and conf(KP_L_SHOULDER) and yv(KP_L_WRIST) < yv(KP_L_SHOULDER) - margin and inx(KP_L_WRIST)
            rr = conf(KP_R_WRIST) and conf(KP_R_SHOULDER) and yv(KP_R_WRIST) < yv(KP_R_SHOULDER) - margin and inx(KP_R_WRIST)
            return bool(lr or rr)
        except Exception:  # noqa: BLE001
            return False

    def _raisers(self, frame, persons):
        """Run pose, return (set of track_ids raising this frame, {track_id: owner_box}).
        Each pose detection is matched to a TRACKED YOLO person by IoU (a within-frame
        cross-detector match -- NOT a frame-to-frame association; that rides the existing
        tracker id). Empty on any fault. Sets ran_inference True only when the model ran."""
        owners = {}
        raisers = set()
        # Phase 1.1 (OPTIMIZATION_PLAN.md): a raise can only bind to a TRACKED person (the pose->person
        # IoU match below requires a person with a track_id), so with ZERO persons the pose run is
        # guaranteed to yield no raisers -- pure waste (~56ms p50 / up to 279ms p99 in empty-scene SEARCH).
        # Skip it. The control decision is unchanged (detect() returns None either way); only the wasted
        # inference is removed -- byte-identical on the control/velocity path.
        if not persons:
            return raisers, owners
        try:
            _t0 = time.monotonic()
            res = self.model.predict(frame, verbose=False)
            self._ms.append((time.monotonic() - _t0) * 1000.0)   # latency instrument
        except Exception:  # noqa: BLE001
            return raisers, owners
        self.ran_inference = True
        dets = 0
        try:
            for r in res:
                kpts = getattr(r, "keypoints", None)
                boxes = getattr(r, "boxes", None)
                if not self._head_logged:
                    # One-time: proves the ONNX is a POSE export (has keypoints, shape (N,17,3)).
                    # A DETECT export loads fine but kpts is always None -> gesture can never fire.
                    self._head_logged = True
                    log("GESTURE-MODEL-HEAD dets=%s has_kpts=%s kpts_shape=%s"
                        % ((0 if boxes is None else len(boxes)), kpts is not None,
                           (None if (kpts is None or kpts.data is None) else tuple(kpts.data.shape))))
                if kpts is None or boxes is None or kpts.data is None:
                    continue
                kd = kpts.data            # (N,17,3) x,y,conf
                dets += len(kd)
                for i in range(len(kd)):
                    try:
                        k = kd[i]
                        pxy = boxes.xyxy[i].tolist()
                        pb = (float(pxy[0]), float(pxy[1]), float(pxy[2]), float(pxy[3]))
                    except Exception:  # noqa: BLE001
                        continue
                    best_tid, best_iou, best_box = None, 0.0, None
                    second_iou = 0.0
                    for p in persons:
                        tid = p.get("track_id")
                        if tid is None:
                            continue
                        iou = iou_xyxy(pb, p["box"])
                        if iou > best_iou:
                            second_iou = best_iou
                            best_iou, best_tid, best_box = iou, tid, p["box"]
                        elif iou > second_iou:
                            second_iou = iou
                    if best_tid is None or best_iou < self.a.iou_min:
                        gdbg(self.a, "NOMATCH pose#%d best_tid=%s best_iou=%.2f < iou_min=%.2f"
                             % (i, best_tid, best_iou, self.a.iou_min))
                        continue
                    # HARDEN (wrong-person lock): an AMBIGUOUS pose overlaps two people similarly ->
                    # refuse to bind (don't guess which one raised the hand). Refusing is safe; the
                    # operator re-raises when the frame is clearer.
                    if (best_iou - second_iou) < self.a.gesture_owner_margin:
                        gdbg(self.a, "AMBIG pose#%d best=%.2f second=%.2f gap<%.2f -> refuse bind"
                             % (i, best_iou, second_iou, self.a.gesture_owner_margin))
                        continue
                    bh = max(best_box[3] - best_box[1], 1.0)
                    margin = self.a.gesture_kp_margin_frac * bh
                    raised = self._is_raised(k, margin)
                    try:
                        gdbg(self.a, "CAND tid=%s iou=%.2f margin=%.0f raised=%s "
                             "Lw=(y%.0f,c%.2f) Ls=(y%.0f,c%.2f) Rw=(y%.0f,c%.2f) Rs=(y%.0f,c%.2f)"
                             % (best_tid, best_iou, margin, raised,
                                float(k[KP_L_WRIST][1]), float(k[KP_L_WRIST][2]),
                                float(k[KP_L_SHOULDER][1]), float(k[KP_L_SHOULDER][2]),
                                float(k[KP_R_WRIST][1]), float(k[KP_R_WRIST][2]),
                                float(k[KP_R_SHOULDER][1]), float(k[KP_R_SHOULDER][2])))
                    except Exception:  # noqa: BLE001
                        pass
                    if raised:
                        # HARDEN (wrong-person lock): the raised arm must be OWNED by the matched body
                        # (shoulders inside the box + raised wrist in the body column). Blocks binding
                        # a raiser's arm onto an overlapping bystander who never raised a hand.
                        if not self._arm_owned(k, best_box, margin):
                            gdbg(self.a, "ARM-DISOWNED tid=%s -> refuse (raised arm not owned by "
                                 "the matched body; likely an overlapping neighbour)" % best_tid)
                        else:
                            raisers.add(best_tid)
                            owners[best_tid] = best_box
            self._last_pose_dets = dets
        except Exception:  # noqa: BLE001
            self._last_pose_dets = dets
            return raisers, owners
        return raisers, owners

    def detect(self, frame, persons, state, w_img, h_img):
        self.ran_inference = False
        if not self.ok:
            return None
        # Gesture inference runs ONLY in the STATIONARY acquisition states (tuple built at
        # call-time so the module-level S_* constants are resolved). Excludes S_TRACK (following)
        # AND S_SEARCHING (the yaw-scan) -- runtime-safety: never run a heavy model in the control
        # path while the robot is driving. Gesture re-acquire after a loss defers to the stood
        # S_REACQUIRE that follows the scan; the other three states command no motion.
        if state not in (S_SEARCH, S_REACQUIRE, S_PARKED):
            gdbg(self.a, "STATE-SKIP state=%s not in (SEARCH,REACQUIRE,PARKED) -> pose not run "
                 "(raise a hand while stationary/searching, not while following or scanning)" % state)
            return None
        self._tick += 1
        if self.every_n > 1 and (self._tick % self.every_n) != 0:
            return None                     # decimated (audit cadence)
        raisers, owners = self._raisers(frame, persons)
        self._log_latency()
        # MISS FORGIVENESS (field fix 2026-07-02): a single blurry / kp-conf-dip pose frame used
        # to hard-reset the whole hold, which at kp-conf 0.5 made an 8-streak nearly impossible
        # to complete. Now a hold survives up to --gesture-miss-tol consecutive missed frames
        # (the miss does NOT increment the hold); identity walls are untouched (two raisers
        # still refuse; the reseed anchor floor still gates re-seeds).
        _tol = int(getattr(self.a, "gesture_miss_tol", 0))
        _now = time.monotonic()
        for tid in list(self.holds.keys()):
            if tid not in raisers:
                self._miss[tid] = self._miss.get(tid, 0) + 1
                if self._miss[tid] > _tol:
                    del self.holds[tid]
                    self._miss.pop(tid, None)
                    self._hold_since.pop(tid, None)   # Phase 2.1: a miss RUN > tol resets the wall-clock dwell
        for tid in raisers:
            if tid not in self.holds:
                self._hold_since[tid] = _now          # Phase 2.1: stamp the first raise of a fresh dwell
            self.holds[tid] = self.holds.get(tid, 0) + 1
            self._miss.pop(tid, None)
        # Phase 2.1 (deterministic confirm): gate on continuous wall-clock dwell (--gesture-hold-s) so the
        # time-to-lock is loop-rate-independent, with a min FRAME floor (--gesture-hold-floor) so a single
        # fluke frame that happens to span the window can't seed. --gesture-hold-s 0 = the legacy pure
        # frame-count gate (holds >= --gesture-hold). The identity walls below (ambiguity / owner-authority
        # / two-raiser refuse) are unchanged -> INV-4 held; pose runs only in stationary states.
        hold_n = int(self.a.gesture_hold)
        hold_s = float(getattr(self.a, "gesture_hold_s", 0.0))
        if hold_s > 0.0:
            _floor = max(1, int(getattr(self.a, "gesture_hold_floor", 3)))
            confirmed = [tid for tid, c in self.holds.items()
                         if c >= _floor and (_now - self._hold_since.get(tid, _now)) >= hold_s]
        else:
            confirmed = [tid for tid, c in self.holds.items() if c >= hold_n]
        gdbg(self.a, "FRAME persons=%d pose_dets=%d raisers=%s holds=%s need=%d confirmed=%s"
             % (len(persons), self._last_pose_dets, sorted(raisers), dict(self.holds),
                hold_n, confirmed))
        if not confirmed:
            return None
        if len(confirmed) >= 2:
            log("GESTURE-AMBIGUOUS %d raisers held -> refusing (one person, raise one hand)"
                % len(confirmed))
            return None
        tid = confirmed[0]
        box = owners.get(tid)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        bh = max(y2 - y1, 1.0)
        point = ((x1 + x2) * 0.5, y1 + 0.6 * bh)   # owner-box lower-centre
        return LockHint(point, box, tid, "gesture", "gesture")

    def _both_hands(self, k, margin):
        """True if BOTH wrists are above their same-side shoulders by >= margin (the 'stop' pose).
        Distinct from the one-hand lock gesture. Never raises."""
        try:
            kpc = float(self.a.gesture_kp_conf)

            def ok(idx):
                return float(k[idx][2]) >= kpc

            def y(idx):
                return float(k[idx][1])

            left = ok(KP_L_WRIST) and ok(KP_L_SHOULDER) and y(KP_L_WRIST) < y(KP_L_SHOULDER) - margin
            right = ok(KP_R_WRIST) and ok(KP_R_SHOULDER) and y(KP_R_WRIST) < y(KP_R_SHOULDER) - margin
            return bool(left and right)
        except Exception:  # noqa: BLE001
            return False

    def stop_gesture(self, frame, persons, seed_tid):
        """In-follow STOP gesture: returns True when the FOLLOWED person (seed_tid) has HELD both
        hands up for --gesture-stop-hold checks. DECIMATED by --gesture-stop-every-n. Runs pose
        (records ms, sets ran_inference) ONLY on a checked frame. De-escalating use only -- the
        caller maps a True to a commanded WAIT (HOLD). Crash-safe -> False."""
        self.ran_inference = False
        if not self.ok or seed_tid is None:
            self._stop_streak = 0
            return False
        self._cmd_tick += 1
        if self._cmd_tick % max(1, int(self.a.gesture_stop_every_n)) != 0:
            return False                          # decimated frame -> no check, streak unchanged
        try:
            _t0 = time.monotonic()
            res = self.model.predict(frame, verbose=False)
            self._ms.append((time.monotonic() - _t0) * 1000.0)
        except Exception:  # noqa: BLE001
            return False
        self.ran_inference = True
        up = False
        try:
            seed_box = None
            for p in persons:
                if p.get("track_id") == seed_tid:
                    seed_box = p["box"]; break
            if seed_box is not None:
                margin = self.a.gesture_kp_margin_frac * max(seed_box[3] - seed_box[1], 1.0)
                for r in res:
                    kpts = getattr(r, "keypoints", None); boxes = getattr(r, "boxes", None)
                    if kpts is None or boxes is None or kpts.data is None:
                        continue
                    kd = kpts.data
                    for i in range(len(kd)):
                        try:
                            pxy = boxes.xyxy[i].tolist()
                            pb = (float(pxy[0]), float(pxy[1]), float(pxy[2]), float(pxy[3]))
                        except Exception:  # noqa: BLE001
                            continue
                        if iou_xyxy(pb, seed_box) >= self.a.iou_min and self._both_hands(kd[i], margin):
                            up = True; break
                    if up:
                        break
        except Exception:  # noqa: BLE001
            return False
        self._stop_streak = self._stop_streak + 1 if up else 0
        return self._stop_streak >= int(self.a.gesture_stop_hold)


class CompositeTrigger(LockTrigger):
    """A/B substrate for --lock-trigger both: ArUco DRIVES (its hint is returned and is
    the only point that reaches _try_seed); gesture runs AUDIT-ONLY (computed + stashed
    for _gesture_audit, never returned, never seeds, never drives). Mirrors the repo's
    audit-only relock vote. on_overrun disables only the gesture sub-trigger -- ArUco
    keeps driving."""
    name = "both"

    def __init__(self, aruco, gesture):
        self.aruco = aruco
        self.gesture = gesture
        self.ok = True
        self.ran_inference = False
        self.last_aruco_hint = None
        self.last_gesture_hint = None

    def detect(self, frame, persons, state, w_img, h_img):
        a = self.aruco.detect(frame, persons, state, w_img, h_img)
        g = self.gesture.detect(frame, persons, state, w_img, h_img)
        self.ran_inference = self.gesture.ran_inference
        self.last_aruco_hint = a
        self.last_gesture_hint = g
        if g is not None:
            gdbg(self.gesture.a, "AUDIT-ONLY raised-hand confirmed (tid=%s) but lock-trigger=BOTH "
                 "-> NOT seeding; use 'Gesture lock', not 'A/B (compare)'"
                 % getattr(g, "owner_tid", None))
        return a                            # ArUco drives; gesture is audit-only

    def on_overrun(self):
        self.gesture.on_overrun()


# ---------------------------------------------------------------------------
# Tier-1 command channel -- a WATCHED FILE the host writes one token to (the
# K1Finder typed box / voice front-end -> a short-lived `ssh ... printf > file`).
# Drained once per tick, non-blocking, crash-safe. NEVER the node's PTY stdin
# (which carries the Ctrl-C e-stop) and NEVER the bridge stdin (raw velocity).
# The brain selects only a member of a FROZEN enum -- never a velocity, never an
# identity. See docs/SCOPE_nl_command_layer.md.
# ---------------------------------------------------------------------------
class CommandChannel:
    def __init__(self, path):
        self.path = path
        self._read_fault_logged = False
        # Clear any STALE token a prior session left behind so it can NEVER be executed on the
        # first tick (autonomy-ops: a stale command is the most dangerous failure mode -- it is
        # the difference between "the operator just commanded this" and "a ghost from last run").
        try:
            if path and os.path.exists(path):
                open(path, "w").close()
        except Exception:  # noqa: BLE001
            pass

    def poll(self):
        """Return the next command LINE (UPPERCASED, e.g. "RESUME ARM") or None. Reads ALL queued
        lines, truncates the file (consume), and returns ONE line with de-escalation priority on
        its FIRST WORD (STOP > HOLD > first-other) so a pending safe-down is never jumped. The full
        line is returned so a motion-initiating command keeps its ARM token. A torn/empty/malformed
        line -> None (NO-OP, never a mis-parse). Never raises. A persistent read fault is logged
        ONCE (fail-loud) -- file commands then drop, but the Ctrl-C / gamepad / hardware e-stop
        remain the real deadman, so the safety floor never depends on this file."""
        try:
            if not self.path or not os.path.exists(self.path):
                return None
            with open(self.path, "r+") as f:
                data = f.read()
                f.seek(0); f.truncate()         # consume (read-then-truncate contract)
        except Exception as e:  # noqa: BLE001
            if not self._read_fault_logged:
                self._read_fault_logged = True
                log("CMD-CHANNEL unreadable (%s) -- file commands DROPPED; use the Ctrl-C / "
                    "gamepad / hardware e-stop (the real deadman, independent of this file)" % e)
            return None
        self._read_fault_logged = False         # recovered -> allow the next fault to log again
        lines = [ln.strip().upper() for ln in data.splitlines() if ln.strip()]
        if not lines:
            return None

        def _first(word):
            for ln in lines:
                if ln.split()[0] == word:        # priority on the command word, not the ARM token
                    return ln
            return None
        return _first("STOP") or _first("HOLD") or lines[0]


# ---------------------------------------------------------------------------
# Pinhole helpers (bearing>0 = target is to the RIGHT of image center)
# ---------------------------------------------------------------------------
# perception (pinhole helpers / PersonDetector / low_light_boost / CamNode) -> perception.py (P3.5).
class Seed:
    def __init__(self, box, centroid, hist, conf):
        self.box = box                 # last bbox (x1,y1,x2,y2)
        self.centroid = centroid       # last (cx,cy)
        self.hist = hist               # HS color histogram (normalized) -- EMA working copy
        # IMMUTABLE original signature captured the instant the marker locks on.
        # Never EMA'd/overwritten. This is the sticky-lock ANCHOR: the followed
        # person must keep matching it, so the lock can never switch to someone else.
        self.anchor_hist = hist.copy() if hist is not None else None
        self.size = (box[2] - box[0]) * (box[3] - box[1])  # last bbox area
        self.conf = conf
        # Stage 1 motion-tracking state (no-ops when --no-track).
        self.track_id = None           # bound MultiTracker id (the anchored person)
        self.track_state = TS_LOCKED   # LOCKED / COASTING within S_TRACK
        self.confidence = float(conf)  # carried beside the estimate every frame
        self.miss_streak = 0           # consecutive coasting frames (no detection)
        self.pred_centroid = centroid  # last motion-predicted centroid
        # Stage 2 appearance gallery + distractor bank (None == --gallery-size 1 == Stage-1).
        self.gallery = None            # TargetGallery, built in _try_seed


# ---------------------------------------------------------------------------
# Main controller -- the lock-and-handoff state machine.
# ---------------------------------------------------------------------------
# S_SEARCH / S_TRACK / S_REACQUIRE / S_PARKED / S_SEARCHING now live in common.py (P3.0).


class Follower:
    def __init__(self, args):
        self.a = args
        self.drive = args.drive
        self.bridge = None
        self.node = None
        self.det = None             # PersonDetector
        self.walking = False        # True only after a successful prep+walk
        self.standing = False       # True after a fail-safe kPrepare stand (blocks velocity)
        self._stop_requested = False
        self._cleaned = False
        self._cleanup_lock = threading.Lock()
        self.t_start = time.monotonic()

        # state machine
        self.state = S_SEARCH
        self.marker_streak = 0      # consecutive frames with a marker
        self.seed = None            # Seed once locked
        self.seed_prev_centroid = (0.0, 0.0)  # last target centroid (EMA jump guard)
        self.lost_count = 0         # consecutive TRACK frames with no gated person
        self.reacquire_since = 0.0  # monotonic time we entered REACQUIRE
        # Stage 3: passive markerless re-acquire. auto OFF by default. The re-lock
        # ARM is a DELIBERATE opt-in (--arm-reacquire, default False): the vote
        # votes+logs but never re-locks unless armed, because markerless re-ID can
        # lock onto a stranger. Stage 4's part-based descriptor raises the bar enough
        # to make arming reasonable in non-crowd settings -- still the operator's call.
        self.reloc_since = 0.0
        self._reloc_streak = 0
        self._reloc_track_id = None
        self._relock_armed = False   # set below, AFTER the appearance backend (needs self.reid/feat_fn)
        # S_PARKED give-up state (named terminal with its own bounded clean-exit).
        self.parked_since = 0.0
        self._parked_log_last = 0.0
        self._range_gated = False   # follow-range geofence latch (hysteresis)
        # latest viz state for the annotated stream (Tracker page)
        self._viz_persons = []
        self._viz_mc = None
        self._viz_range = None
        self._viz_bearing = 0.0
        # S_SEARCHING: yaw-only scan toward the last bearing to re-find a lost target.
        self.search_since = 0.0
        self.search_dir = 1.0
        self._search_log_last = -9.0

        # Stage 1: pixel-space motion tracker (None == --no-track == legacy path).
        self.tracker = MultiTracker(args.iou_min, args.max_coast) if args.track else None
        self._overrun_streak = 0    # consecutive frames the loop ran over its period
        self._rr_overrun_streak = 0  # consecutive over-budget frames with --rerun on (auto-disable)
        self._loop_ms_hist = collections.deque(maxlen=600)  # ~60s of loop WORK-time @ 10Hz
        self._last_loopms_log = 0.0  # 10s cadence for the LOOP-MS observability line

        # Stage 2/4: appearance-feature backend -- the single swap point. 'global' =
        # the original single HS histogram (default; Stages 1-3 behavior verbatim);
        # 'striped' = the Stage-4 model-free part-based descriptor. The gallery /
        # distractor / re-acquire machinery is feat-agnostic (it only calls feat_fn/
        # sim_fn), so this one switch re-points the entire appearance stack.
        self.reid = None
        if args.appearance == "osnet":
            try:
                ih, iw = [int(v) for v in str(args.reid_input).lower().split("x")]
            except Exception:  # noqa: BLE001
                ih, iw = 256, 128
            self.reid = ReidEngine(
                args.reid_engine, input_hw=(ih, iw), fp16=args.reid_fp16,
                cache_dir=(os.path.dirname(args.reid_engine) or None),
                letterbox=args.reid_letterbox, batch=args.reid_batch)
            if self.reid.ok:
                self.feat_fn = self.reid.embed     # deep embedding
                self.sim_fn = striped_sim          # cosine (generic over 1D vectors)
            else:
                self.feat_fn = color_hist          # hard fallback (logged in ReidEngine)
                self.sim_fn = hist_similarity
        elif args.appearance == "striped":
            self.feat_fn = striped_feat
            self.sim_fn = striped_sim
        else:
            self.feat_fn = color_hist
            self.sim_fn = hist_similarity
        self._frame_idx = 0
        self._reid_cache = {}   # Stage-4 osnet: track_id -> (frame_idx, embedding) for --reid-every-n

        # ARM markerless re-lock ONLY with OSNet (weak HS/striped can re-lock onto a
        # same-clothing stranger) AND a valid strictness ladder. Anything weaker -> the vote
        # still runs but stays AUDIT-ONLY. The re-lock is the only path that drives. Placed
        # HERE (not in the reloc-init block) so self.reid/self.feat_fn already exist.
        # NOTE: use == not `is` -- `self.reid.embed` makes a FRESH bound-method object on
        # every access, so `feat_fn is self.reid.embed` is ALWAYS False (it silently forced
        # audit-only). == compares __self__/__func__, which is the real intent.
        _osnet_ok = (self.reid is not None and self.reid.ok and self.feat_fn == self.reid.embed)
        # ITEM 3: an OSNet running on the CPU EP (GPU EP was available but fell back) is too slow to
        # trust for a DRIVING re-lock -> treat it like a failed arm precondition (audit-only). This
        # only ever DENIES arming; it can never grant it (additive to the safe direction).
        _ep_ok = not (self.reid is not None and getattr(self.reid, "cpu_ep_degraded", False))
        _ladder_ok = (args.bank_floor >= args.anchor_floor
                      and args.reloc_floor > args.anchor_floor
                      and args.reloc_view_floor >= args.bank_floor
                      and 0.0 < args.reloc_anchor_backstop < args.anchor_floor
                      and args.reloc_iso_bypass_anchor > args.anchor_floor
                      and args.reloc_arm_streak >= args.reloc_streak
                      and args.reloc_arm_margin >= args.reloc_margin)
        self._relock_armed = bool(args.arm_reacquire) and _osnet_ok and _ep_ok and _ladder_ok
        if args.arm_reacquire and not self._relock_armed:
            log("ARM-REFUSED: markerless re-lock requires --appearance osnet with a loaded ReID "
                "engine (osnet_ok=%s) on a GPU EP (ep_ok=%s) AND a valid floor ladder (ladder_ok=%s) "
                "-> audit-only" % (_osnet_ok, _ep_ok, _ladder_ok))

        # Lock trigger -- the ACQUISITION primitive (--lock-trigger). DEFAULT 'aruco'
        # reproduces today byte-for-byte (the gesture model is not even constructed, so
        # ZERO new compute at the default). 'gesture' seeds from a raised hand; 'both'
        # runs ArUco-drives + gesture-audit (the A/B harness). See SCOPE_lock_trigger_gesture.md.
        self._lock_hint = None
        self._viz_lock_src = None
        self._gesture_overrun_streak = 0
        _aruco = ArucoTrigger()
        if args.lock_trigger == "aruco":
            self._lock_trigger = _aruco
        else:
            _gest = GestureTrigger(
                args.gesture_model, args,
                every_n=(args.gesture_every_n if args.lock_trigger == "both" else 1))
            if args.lock_trigger == "gesture":
                self._lock_trigger = _gest if _gest.ok else _aruco
                if not _gest.ok:
                    log("LOCK-TRIGGER gesture requested but model load FAILED -> ArUco fallback "
                        "(needs a physical marker; if none is in scene NO SEED is possible)")
            else:  # both
                self._lock_trigger = CompositeTrigger(_aruco, _gest)
        # Always-on banner: the SINGLE most useful diagnostic -- can gesture actually SEED, or are we
        # effectively running ArUco (A/B mode, or the pose model failed to load)? gesture_can_seed=False
        # while you expect gesture is the whole bug class #1/#2.
        _eff = self._lock_trigger
        log("GESTURE-DEBUG banner: lock_trigger=%s effective=%s model_ok=%s gesture_can_seed=%s "
            "every_n=%s gesture_stop=%s gesture_debug=%s"
            % (args.lock_trigger, _eff.name, getattr(_eff, "ok", True),
               (args.lock_trigger == "gesture" and getattr(_eff, "ok", True)),
               getattr(_eff, "every_n", 1), bool(args.gesture_stop), bool(args.gesture_debug)))

        # A/B audit accumulators (only allocated in --lock-trigger both).
        self._gb = None
        if args.lock_trigger == "both":
            self._gb = {
                "frames": 0, "active": 0, "agree": 0, "disagree": 0,
                "g_only": 0, "a_only": 0, "stranger": 0, "ftrig": 0, "miss": 0,
                "bystander": False, "a_ttl": [], "g_ttl": [], "ep_start": None,
                "a_locked_ep": False, "g_locked_ep": False, "g_streak": 0,
                "g_streak_tid": None, "last_log": -9.0,
                # EPISODE-level event counters (sim-eval: the unit of analysis is the acquisition
                # opportunity, NOT the frame). acq_opps = episodes where ArUco WOULD seed a person;
                # acq_bystander = those with a 2nd person in frame (the rule-of-three denominator);
                # acq_both = both triggers stable-locked; acq_agree = on the SAME track_id;
                # acq_seed_disagree = both locked but DIFFERENT id (the safety-relevant seed event
                # that ALSO covers the first-seed case the reseed-floor 'stranger' metric cannot).
                "acq_opps": 0, "acq_bystander": 0, "acq_both": 0, "acq_agree": 0,
                "acq_seed_disagree": 0, "ep_bystander": False,
                "a_lock_tid": None, "g_lock_tid": None, "ep_recorded": False,
            }

        # Tier-1 command channel (SCOPE_nl_command_layer.md). Default OFF (--no-commands)
        # -> byte-identical: self._cmd is None, the per-tick drain never runs, commanded_hold
        # never latches. This slice wires only the DE-ESCALATING commands (STATUS/HOLD/PARK/
        # STOP -- none initiate motion, none need ARM); RESUME/FOLLOW are a later ARM-gated slice.
        # commanded_hold latches a stand that FREEZES the FSM (one top-level guard in
        # _process_frame), so no auto-resume/reseed/relock/coast can move a held robot. The
        # attrs are set unconditionally so the guard never AttributeErrors when commands are off.
        self._cmd = CommandChannel(args.cmd_file) if args.commands else None
        self.commanded_hold = False
        self.hold_since = 0.0
        self._hold_log_last = 0.0

        # Untethered operator-heartbeat deadman (default OFF -> tethered byte-identical).
        self.require_hb = bool(args.require_heartbeat)
        self.hb_file = args.hb_file
        self.hb_stale_s = max(0.05, args.hb_stale_ms / 1000.0)
        self._hb_lost_logged = False
        # P1.2: record when drive is running via the unsafe override (the parse_args gate let it
        # through only because --allow-untethered-unsafe was passed) so the log/.rrd captures that
        # the operator deadman was deliberately bypassed.
        if self.drive and getattr(args, "allow_untethered_unsafe", False) and not self.require_hb:
            log("WARN UNTETHERED-UNSAFE override active: driving with NO operator deadman "
                "(--allow-untethered-unsafe). Keep a hand on the gamepad e-stop.")

        self.vx_min = max(args.vx_min, -HARD_VX_LIMIT)
        self.vx_max = min(args.vx_max,  HARD_VX_LIMIT)
        self.vyaw_min = max(args.vyaw_min, -HARD_VYAW_LIMIT)
        self.vyaw_max = min(args.vyaw_max,  HARD_VYAW_LIMIT)

        # FIX C -- accel (slew) limiter state. The per-tick rate caps how fast the
        # commanded velocity may change; <=0 disables that axis. _prev_* holds the
        # velocity ACTUALLY SENT last tick (written in _drive_vel AFTER the heartbeat
        # deadman zero and the walking gate), so it is 0.0 across any stand/hold/
        # deadman-zero/non-walking frame -> the first frame after a relock/resume always
        # ramps from 0. Slew is applied in the control law BEFORE the existing clamp().
        self.vx_slew = max(args.vx_slew, 0.0)
        self.vyaw_slew = max(args.vyaw_slew, 0.0)
        self._prev_vx = 0.0
        self._prev_vyaw = 0.0
        # FIX F1 -- ARMED-relock RANGE admission gate state. _track_range_hist is a short window
        # of the most recent VALIDATED depth ranges while TRACKING; its MEDIAN is a glitch-
        # resistant reference for the relock jump gate (a single spike can't poison it).
        # _track_range_t stamps the window so the gate can AGE OUT a stale reference -- a target
        # who legitimately walked far during a long loss must still be able to re-lock (REG-1).
        # _reacq_range_* count consecutive in-range same-id relock frames; _postrelock_noforward
        # is a one-shot forbidding forward vx on the first TRACK frame after an armed relock.
        # _close_streak debounces the close-range geofence stand (REG-2).
        self._track_range_hist = []
        self._track_range_t = None
        self._reacq_range_streak = 0
        self._idsw_pending = None    # Phase 2.4 ID-stability debounce: track_id of an unconfirmed id-switch
        self._idsw_frames = 0        # Phase 2.4: consecutive frames the pending new track_id has persisted
        self._clr_hist = []          # OBSTACLE-BRAKE: recent forward-corridor clearances (aged-median)
        self._last_clr_log = 0.0     # 1Hz throttle for the CLEARANCE observability line
        self._reacq_range_id = None
        self._postrelock_noforward = False
        self._close_streak = 0
        # ITEM 4: mid-run OSNet fault watchdog. _reid_none_streak counts CONSECUTIVE frames where
        # embed_batch returned all-None over a NON-EMPTY person set; at --reid-fault-k it latches
        # _reid_degraded, which FORCES armed re-lock to audit-only (disarm) until a valid embed
        # returns. Latching + a single de-latch log on recovery.
        self._reid_none_streak = 0
        self._reid_degraded = False
        # ITEM 5: depth-fps floor latch (hysteresis). True == sustained depth fps below --min-depth-fps
        # -> force TURN-ONLY (suppress forward vx) via the existing forbid_forward keystone.
        self._depth_starved = False
        # F4/FR-4 log-flood throttles: NO-FRAME and the per-frame RELOC vote lines are safety
        # telemetry, not a firehose -- 1/s each keeps the .err file and the app log readable.
        self._last_noframe_log = 0.0
        self._reloc_log_last = 0.0

    # -- signals ------------------------------------------------------------
    def install_signal_handlers(self):
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        # SIGHUP: when the app stops the (no-PTY) stream by killing ssh, the remote
        # node gets SIGHUP -> run the same clean stop+quit (the bridge EOF also safes).
        try:
            signal.signal(signal.SIGHUP, self._on_signal)
        except (AttributeError, ValueError):
            pass

    def _on_signal(self, signum, frame):  # noqa: ARG002
        log("SIGNAL %d -> stopping" % signum)
        self._stop_requested = True

    # -- velocity out (only ever in drive+walking) --------------------------
    def _hb_fresh(self):
        """Untethered deadman: True if the operator heartbeat file is fresh, or hb is not
        required. Fail-CLOSED -- a missing/unreadable file reads as NOT fresh (stop)."""
        if not self.require_hb:
            return True
        try:
            dt = time.time() - os.path.getmtime(self.hb_file)
            # future mtime (clock skew) -> NOT fresh; tiny negative tolerated (fs/float granularity).
            return -0.005 <= dt <= self.hb_stale_s
        except OSError:
            return False

    @staticmethod
    def _slew(prev, target, rate):
        """Accel limiter: move 'prev' toward 'target' by at most 'rate' per call.
        rate<=0 disables (returns target). Pure/exception-free -- safe to call from
        any control-law site inside run()'s per-frame try."""
        if rate <= 0.0:
            return target
        d = target - prev
        if d > rate:
            return prev + rate
        if d < -rate:
            return prev - rate
        return target

    def _drive_vel(self, vx, vy, vyaw):
        # OPERATOR-HEARTBEAT precondition (untethered): NO velocity may leave this process
        # without a fresh operator heartbeat. Defense-in-depth WITH the bridge's own hb
        # watchdog (which also kPrepares). No-op unless --require-heartbeat.
        if self.require_hb and not self._hb_fresh():
            if not self._hb_lost_logged:
                log("HB-LOST operator heartbeat stale -> velocity gated to zero")
                self._hb_lost_logged = True
            vx = vy = vyaw = 0.0
        elif self.require_hb and self._hb_lost_logged:
            log("HB-OK operator heartbeat restored")
            self._hb_lost_logged = False
        if self.drive and self.walking and self.bridge is not None:
            self.bridge.send_velocity(vx, vy, vyaw)
            # FIX C: baseline = what we ACTUALLY sent (post-deadman, post-clamp). On any
            # non-walking / deadman-zeroed / hold / stand frame we fall through with the
            # baseline forced to 0.0 below, so the slew always ramps from 0 after a
            # stand/relock/resume. This stays AFTER the heartbeat zero (deadman first).
            self._prev_vx = vx
            self._prev_vyaw = vyaw
        else:
            # not actually moving (preview / stood / not yet walking) -> baseline rests at 0
            self._prev_vx = 0.0
            self._prev_vyaw = 0.0

    # ---- OBSTACLE-BRAKE reflex (Phase 3 of OBSTACLE_LABELING_PLAN.md) ---------------------------
    # Geometry TRIGGERS, semantics modulate: a cheap depth forward-clearance reduction grades vx down
    # as an obstacle enters the forward corridor. It ONLY ever REDUCES forward vx (never authorizes
    # it), yaw is untouched, and it composes with the forbid_forward keystone -- so it cannot make the
    # forward path less safe, only more cautious. Default-off (--obstacle-brake); byte-identical off.
    def _corridor_clearance(self):
        """Robust nearest-obstacle range (m) in the forward corridor, or None. Cheap: a percentile
        over the central band of the depth map (excludes the floor via a mid-vertical band). Uses a
        PERCENTILE (not raw min) + an AGED-MEDIAN history -- Phase-2 finding: a single depth-glitch
        pixel at a raw min would FALSE-BRAKE (the relock-lunge failure class)."""
        if self.node is None:
            return None
        # FIX (audit 2026-07-04): use latest_depth()'s default freshness (max_age=0.5 == DEPTH_FRESH_S),
        # the SAME depth-staleness contract the main follow uses at its depth read. The prior code read
        # a "depth_max_age" argparse attr that was never defined, so --obstacle-brake raised
        # AttributeError on every tracked frame (this read is above the try) and bricked the follow.
        d = self.node.latest_depth()
        if d is None:
            return None
        try:
            h, w = d.shape[:2]
            cf = max(0.05, min(1.0, self.a.obstacle_corridor_frac))
            x0 = int(w * (0.5 - cf / 2.0)); x1 = int(w * (0.5 + cf / 2.0))
            y0 = int(h * self.a.obstacle_band_top); y1 = int(h * self.a.obstacle_band_bot)
            band = d[y0:y1, x0:x1]
            v = band[(band > 0.15) & (band < self.a.obstacle_max_m) & np.isfinite(band)]
            if v.size < self.a.obstacle_min_valid:
                return None
            clr = float(np.percentile(v, self.a.obstacle_pctile))
        except Exception:  # noqa: BLE001 -- a reflex must never break the loop
            return None
        self._clr_hist.append(clr)
        self._clr_hist = self._clr_hist[-max(1, self.a.obstacle_aged):]
        return float(sorted(self._clr_hist)[len(self._clr_hist) // 2])   # aged-median

    def _obstacle_vx_cap(self, target_range):
        """Max forward vx allowed by the corridor clearance (None = no cap). Graded: clear above
        --obstacle-brake-start, linearly down to 0 at --obstacle-brake-stop.

        Only brakes for obstacles BETWEEN the robot and the followed target -- the operator is in the
        forward corridor BY DEFINITION, so if the nearest corridor return is at (or beyond) the target
        range minus a margin, that return IS the target and we do NOT brake (else the reflex fights the
        follow's own standoff control). We brake only when something is meaningfully closer -- a chair
        in the way."""
        if not self.a.obstacle_brake:
            return None, None
        clr = self._corridor_clearance()
        if clr is None:
            return None, None
        if target_range is not None and clr > (target_range - self.a.obstacle_target_margin):
            return None, clr                             # nearest return is the followed target -> no cap
        bs, bp = self.a.obstacle_brake_start, self.a.obstacle_brake_stop
        if clr >= bs:
            cap = None                                   # corridor clear -> no forward cap
        elif clr <= bp:
            cap = 0.0                                    # too close -> no forward drive (turn/back only)
        else:
            cap = self.vx_max * (clr - bp) / max(1e-6, bs - bp)
        return cap, clr

    def _hold(self):
        """Command a halt (hold position). Safe in preview (no-op)."""
        self._drive_vel(0.0, 0.0, 0.0)

    def _stand(self):
        """Fail-safe on perception loss: zero velocity, THEN command kPrepare --
        the verified stable, balanced standing stance -- so the robot settles
        instead of holding a walking gait blind (kDamping would go limp = a fall).
        Idempotent: issues prep once (gated by self.standing) and sets
        walking=False so no velocity can stream until a successful re-walk.
        Wrapped so a bridge write failure can never crash the loop."""
        try:
            self._hold()   # zero velocity first (no-op if already not walking)
            if self.drive and self.walking and self.bridge is not None and not self.standing:
                self.bridge.prep()       # ChangeMode(kPrepare)
                self.walking = False     # gate velocity off until we re-walk
                self.standing = True
                log("FAIL-SAFE STAND (kPrepare) -- perception lost; holding a stable stance")
        except Exception as e:  # noqa: BLE001 -- never let a stand attempt kill the loop
            log("STAND-ERR %s (zero velocity held)" % e)

    def _resume_walk(self):
        """Re-enter kWalking from a fail-safe stand before following again.
        Fire-and-forget prep->settle->walk->settle (the bridge executes the
        ChangeModes regardless; mid-stream OK-replies are unreliable because the
        velocity acks pile up unread). Returns True on success; on failure the
        robot stays standing (safe) and no velocity streams."""
        if not self.drive or self.bridge is None:
            self.standing = False
            return True
        try:
            self.bridge.prep()
            if self._sleep_interruptible(3.0):
                return False
            self.bridge.walk()
            if self._sleep_interruptible(2.0):
                return False
            self.walking = True
            self.standing = False
            log("RESUME prep->walk -> following again")
            return True
        except Exception as e:  # noqa: BLE001
            log("RESUME-ERR %s -> staying standing" % e)
            return False

    # -- Tier-1 command handling ------------------------------------------------
    # STOP/HOLD/PARK/STATUS de-escalate (never ARM -- safing must never be gated). RESUME/FOLLOW
    # INITIATE motion and require a PER-COMMAND ARM token under --drive (the "ARM" word on the
    # command line, set only by the operator's ARM-MOTION confirm -- NOT a re-read of the launch
    # --drive flag, so an NL RESUME can never walk with no live human-in-loop). SCOPE_nl_command_layer §2.4.
    CMD_VALID = ("STOP", "HOLD", "PARK", "STATUS", "RESUME", "FOLLOW")

    def _drain_command(self):
        """Apply at most ONE command this tick. Every consumed command gets a flushed CMD ACK so
        the host can show pending->acked over SSH latency. Fully crash-safe -- any fault degrades
        to a logged no-op, NEVER a session kill (the whole body is wrapped)."""
        try:
            line = self._cmd.poll()
            if line is None:
                return
            parts = line.split()
            tok = parts[0]
            armed = "ARM" in parts[1:]              # per-command motion credential
            if tok == "STOP":
                # Additive to the GUI Ctrl-C deadman, never a replacement. Terminal.
                log("CMD STOP applied -> stopping (terminal; loco safed on exit)")
                self._stop_requested = True
            elif tok == "HOLD":
                # Latch FIRST, THEN stand (SCOPE §2.3): the LATCH (not self.standing) is what
                # RESUME clears, so a WAIT after a lost-track stand is never a silent no-op.
                self.commanded_hold = True
                self.hold_since = time.monotonic()
                self._hold_log_last = 0.0
                self._stand()
                log("CMD HOLD applied -> commanded WAIT (latched stand; FSM frozen until RESUME)")
            elif tok == "PARK":
                self.state = S_PARKED
                self.parked_since = time.monotonic()
                self._parked_log_last = 0.0
                log("CMD PARK applied -> PARKED (stood; bounded clean-exit, RESUME/lock to recover)")
            elif tok == "STATUS":
                log("CMD STATUS: state=%s%s walking=%s standing=%s seed=%s trigger=%s%s"
                    % (self.state, " HELD" if self.commanded_hold else "",
                       self.walking, self.standing, "Y" if self.seed is not None else "n",
                       self.a.lock_trigger,
                       "" if (self.drive and self.walking) else " [preview]"))
            elif tok in ("RESUME", "FOLLOW"):
                if self.drive and not armed:
                    log("CMD %s ignored:no-arm -- motion-initiating under --drive needs the "
                        "per-command ARM credential (operator ARM-MOTION confirm), NOT the launch "
                        "flag. The WAIT latch stays set." % tok)
                    return
                if tok == "RESUME":
                    self._cmd_resume()
                else:
                    self._cmd_follow()
            else:
                log("CMD %s ignored:unknown (valid: %s)" % (tok, ",".join(self.CMD_VALID)))
        except Exception as e:  # noqa: BLE001 -- a command fault must never kill the loop/session
            log("CMD-ERR %s" % e)

    def _cmd_resume(self):
        """Clear the WAIT latch and re-drive PER SOURCE STATE (SCOPE §2.4). RESUME cannot recreate
        a lost identity: only a live S_TRACK lock re-drives; other states simply un-freeze and
        resume their own (stood) behavior, and the operator must show the lock to re-seed. In
        --preview this only clears the latch + logs (no motion possible). ARM-checked by caller."""
        if not self.commanded_hold:
            log("CMD RESUME ignored:not-held (nothing to resume)")
            return
        self.commanded_hold = False
        live = (self.state == S_TRACK and self.seed is not None)
        if live and self.drive and self.standing:
            if not self._resume_walk():            # blocks ~5s; robot safely STANDING throughout
                self._stand()
                log("CMD RESUME: re-walk failed -> staying stood")
                return
            # Post-_resume_walk recheck (SCOPE §2.4 HARD requirement): the ~5s bring-up drained no
            # commands; if a STOP arrived (SIGINT) or HOLD was re-latched, safe immediately.
            if self._stop_requested or self.commanded_hold:
                self._stand()
                return
        log("CMD RESUME applied -> %s%s"
            % ("re-following the live lock" if live
               else "latch cleared; NO live target (state=%s) -- show the lock to re-seed" % self.state,
               "" if (self.drive and self.walking) else " [preview]"))

    def _cmd_follow(self):
        """Clear the WAIT latch and RE-ARM the acquisition hunt (force S_SEARCH). Cannot create
        identity -- it only re-opens the lock hunt with whatever the active --lock-trigger is
        (trigger-agnostic, SCOPE_lock_trigger_gesture §4). Does NOT pre-resume walk: the robot
        stays in a balanced kPrepare stand while searching (no kWalking-station idle), and the
        seed -> S_TRACK -> _track path resumes walking when a lock is actually acquired."""
        self.commanded_hold = False
        self.state = S_SEARCH
        self.marker_streak = 0
        # autonomy-planner: clear any stale gesture hold so a leftover count can't instantly re-fire
        # a seed the instant FOLLOW re-arms. Handles both GestureTrigger and CompositeTrigger.
        _g = getattr(self._lock_trigger, "gesture", self._lock_trigger)
        if hasattr(_g, "holds"):
            try:
                _g.holds.clear()
            except Exception:  # noqa: BLE001
                pass
        log("CMD FOLLOW applied -> re-arming acquisition (S_SEARCH, trigger=%s); show the lock to "
            "seed%s" % (self.a.lock_trigger, "" if self.drive else " [preview]"))

    # -- drive bring-up (verbatim safety chain) -----------------------------
    def start_drive_chain(self):
        """Returns True if we successfully reached WALK; False to stay safe."""
        path = self.a.bridge
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not (os.path.exists(path) and os.access(path, os.X_OK)):
            log("DRIVE-ABORT bridge not found/executable: %s" % path)
            return False

        extra_env = None
        if self.require_hb:
            extra_env = {"K1_REQUIRE_HB": "1", "K1_HB_FILE": self.hb_file}
        self.bridge = Bridge(path, extra_env=extra_env)
        try:
            self.bridge.start()
        except Exception as e:  # noqa: BLE001
            log("DRIVE-ABORT cannot spawn bridge: %s" % e)
            return False

        ok, reply = self.bridge.command_expect_ok("ping")
        log("BRIDGE ping -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT ping failed; not moving")
            return False

        if not self._wait_for_first_frame(self.a.startup_frame_wait):
            log("DRIVE-ABORT no camera frame within %.1fs; staying safe (no walk)"
                % self.a.startup_frame_wait)
            return False

        if self._stop_requested:
            return False

        # ITEM 7(d): DRIVE PRECONDITION. Before kWalking, require BOTH RGB and depth publishing at
        # >= --drive-min-fps, sustained for --drive-ready-secs. BOUNDED by --startup-frame-wait so it
        # can NEVER hang. On timeout we REFUSE to drive (DRIVE-ABORT) -- the safe, least-surprising
        # choice: every existing frame-starvation path in this file refuses to walk rather than
        # walking blind (no-frame -> DRIVE-ABORT, YOLO-fail -> DRIVE-ABORT), and forward drive is
        # gated on validated depth anyway, so a robot that walks with dead depth would only ever
        # turn-in-place -- surprising and pointless. --drive-min-fps 0 disables the whole gate
        # (today's single-first-frame behaviour, byte-for-byte).
        if float(getattr(self.a, "drive_min_fps", 0.0)) > 0.0:
            _floor = float(self.a.drive_min_fps)
            _need = max(float(getattr(self.a, "drive_ready_secs", 0.0)), 0.0)
            # Depth-DISABLED config (--depth-topic none): there is no depth subscription, so its fps
            # is 0.0 forever and requiring it would make drive PERMANENTLY unreachable (a regression
            # -- that config could previously drive, turn-only, with forward vx already impossible
            # via the rsrc=='depth' keystone). Require depth freshness only when depth is enabled.
            _need_depth = bool(self.a.depth_topic) and (self.a.depth_topic.lower() != "none")
            _t0 = time.monotonic()
            _ready_since = None
            _last_wait_log = 0.0
            while (time.monotonic() - _t0) < self.a.startup_frame_wait:
                if self._stop_requested:
                    return False
                time.sleep(0.1)   # cam-spin thread services callbacks
                _now = time.monotonic()
                _rf = self.node.rgb_fps()
                _dh, _df = self.node.depth_health(_now)
                # FR-3: fps EMAs only update on frame ARRIVAL and never decay, so a stream that
                # dies mid-window keeps its last healthy number -- 'sustained-fresh' must ALSO
                # check AGE. RGB: newest frame within a floor-scaled window. Depth: the age-based
                # health state must be FRESH (WARMING/STALE/DOWN all fail).
                _rs = self.node.rgb_stamp()
                _fresh_win = max(2.0 / _floor, 0.5)
                _rgb_live = (_rs > 0.0) and ((_now - _rs) <= _fresh_win)
                _ok_now = ((_rf >= _floor) and _rgb_live
                           and ((not _need_depth) or (_df >= _floor and _dh == "FRESH")))
                if _ok_now:
                    if _ready_since is None:
                        _ready_since = _now
                    if (_now - _ready_since) >= _need:
                        log("DRIVE-READY rgb_fps=%.1f depth_fps=%.1f (>= floor %.1f for %.1fs) -> walk"
                            % (_rf, _df, _floor, _need))
                        break
                else:
                    _ready_since = None
                if (_now - _last_wait_log) >= 1.0:
                    _last_wait_log = _now
                    log("DRIVE-WAIT rgb_fps=%.1f depth_fps=%.1f depth=%s need>=%.1f for %.1fs"
                        % (_rf, _df, _dh, _floor, _need))
            else:
                log("DRIVE-ABORT sensors not sustained-fresh (rgb_fps=%.1f depth_fps=%.1f floor=%.1f) "
                    "within %.1fs; staying safe (no walk)"
                    % (self.node.rgb_fps(), self.node.depth_health()[1], _floor, self.a.startup_frame_wait))
                return False

        ok, reply = self.bridge.command_expect_ok("prep", timeout=6.0)
        log("BRIDGE prep -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT prep failed")
            return False
        if self._sleep_interruptible(3.0):
            return False

        ok, reply = self.bridge.command_expect_ok("walk", timeout=6.0)
        log("BRIDGE walk -> %s" % reply)
        if not ok:
            log("DRIVE-ABORT walk failed")
            return False
        if self._sleep_interruptible(2.0):
            return False

        self.walking = True
        log("DRIVE-ACTIVE follow enabled (vx[%.2f,%.2f] vyaw[%.2f,%.2f])"
            % (self.vx_min, self.vx_max, self.vyaw_min, self.vyaw_max))
        return True

    def _wait_for_first_frame(self, timeout):
        t0 = time.monotonic()
        while (time.monotonic() - t0) < timeout:
            if self._stop_requested:
                return False
            time.sleep(0.1)   # cam-spin thread services callbacks
            frame, _, _ = self.node.take_if_new(-1)
            if frame is not None or self.node.frames_total > 0:
                return True
        return self.node.frames_total > 0

    def _sleep_interruptible(self, secs):
        t0 = time.monotonic()
        while (time.monotonic() - t0) < secs:
            if self._stop_requested:
                return True
            time.sleep(0.05)   # callbacks are serviced by the cam-spin executor thread
        return False

    def _cam_spin(self):
        """Background executor thread: services ALL CamNode callbacks at sensor rate (FR-1).
        rclpy.shutdown() during teardown ends the spin; the thread is a daemon and must
        never crash the process."""
        try:
            self._cam_exec.spin()
        except Exception:  # noqa: BLE001 -- teardown races are expected and harmless
            pass

    def _reid_watchdog(self, any_valid):
        """ITEM 4 mid-run OSNet fault watchdog (shared). Count CONSECUTIVE all-None embed
        outcomes over a non-empty person set; at --reid-fault-k latch _reid_degraded (surface
        ONCE), forcing armed re-lock -> audit-only. Any valid embed de-latches with a single
        recovery line. Called from BOTH _embed_persons (TRACK) and _try_reacquire (REACQUIRE
        vote) so a recovered engine de-latches even while the target is lost. --reid-fault-k 0
        disables (byte-identical to the pre-watchdog behavior)."""
        _k = int(getattr(self.a, "reid_fault_k", 0))
        if _k <= 0:
            return
        if any_valid:
            if self._reid_degraded:
                log("REID-DEGRADED recovered (valid embed after %d all-none frames) -> re-arm eligible"
                    % self._reid_none_streak)
            self._reid_none_streak = 0
            self._reid_degraded = False
        else:
            self._reid_none_streak += 1
            if self._reid_none_streak >= _k and not self._reid_degraded:
                self._reid_degraded = True
                log("REID-DEGRADED all-none streak=%d k=%d -> armed re-lock forced audit-only"
                    % (self._reid_none_streak, _k))

    # -- main run -----------------------------------------------------------
    def run(self):
        rclpy.init(args=None)
        topics = list(dict.fromkeys([self.a.topic,
                                     "/boostercamera/head/raw/rgb",
                                     "/boostercamera/head/rgb"]))
        # Depth is DISABLED for "" / whitespace / any-case "none" -- parse_args normalizes the
        # spelling, and the DRIVE precondition's _need_depth uses the SAME test so they can never
        # diverge (an empty-string topic used to subscribe nothing yet still be "required").
        depth_topic = self.a.depth_topic if (self.a.depth_topic
                                             and self.a.depth_topic.lower() != "none") else None
        self.node = CamNode(topics, depth_topic)
        # FR-1 (CRITICAL): service CamNode on a DEDICATED background executor thread. The old
        # one-spin_once-per-10Hz-tick pattern measured the LOOP's callback-servicing rate, not the
        # sensor: with 2 RGB subs + depth at KEEP_LAST 1, depth was serviced at <=3.3-5Hz on a
        # healthy 30fps sensor, so the --min-depth-fps floor latched spuriously and could never
        # clear, and rgb/depth stamps (the skew gate) were tick-quantized. CamNode is fully
        # lock-protected (_lock/_depth_lock cover every accessor), so a background executor is
        # thread-safe as written; every fps EMA and stamp now measures the true sensor. The main
        # loop no longer spins ROS at all; the wait loops become plain sleeps.
        self._cam_exec = SingleThreadedExecutor()
        self._cam_exec.add_node(self.node)
        threading.Thread(target=self._cam_spin, daemon=True, name="cam-spin").start()

        # Load YOLO ONCE at startup (before any walking).
        self.det = PersonDetector(self.a.yolo_path, self.a.conf)
        log("MODE %s  topics=%s  depth=%s  yolo=%s"
            % ("DRIVE" if self.drive else "PREVIEW", ",".join(topics),
               depth_topic or "off", "ok" if self.det.ok else "DISABLED"))

        if self.drive:
            # A drive REFUSAL exits 4 (not 0) so the app's exit-code classifier can tell
            # "node declined to walk" from a normal operator stop and fetch the log tail.
            if not self.det.ok:
                log("DRIVE-ABORT YOLO not loaded; cannot follow a person. Staying safe.")
                self._cleanup()
                rclpy.shutdown()
                sys.exit(4)
            if not self.start_drive_chain():
                self._cleanup()
                rclpy.shutdown()
                sys.exit(4)

        period = 1.0 / max(1.0, self.a.rate_hz)
        last_seq = -1
        last_frame_mono = 0.0
        ever_framed = False
        last_depth_state = None   # depth-health heartbeat (log on transition + 10s pulse)
        last_depth_hb = 0.0

        try:
            while not self._stop_requested:
                t0 = time.monotonic()

                if (t0 - self.t_start) >= self.a.max_seconds:
                    log("WATCHDOG max-seconds (%.0fs) reached -> stop" % self.a.max_seconds)
                    break

                # (no ROS spin here -- the cam-spin executor thread services callbacks at
                #  sensor rate; this loop just consumes the latest frame under lock)
                frame, last_seq, _ = self.node.take_if_new(last_seq)

                # Rerun (Phase 2): stamp this control-loop iteration's frame_idx ONCE (keyed to the
                # same self._seq the cam-spin image path uses -> a decision aligns to its frame).
                # rerun time is per-thread, so this never collides with the cam-spin cursor. Inert
                # unless --rerun.
                if rerun_sink._RR.ok:
                    rerun_sink._RR.frame(last_seq if last_seq >= 0 else 0, time.time())

                # Tier-1 command drain: one token per tick, BEFORE _process_frame, every tick
                # (so STOP/HOLD are honored even during a NO-FRAME stall). Microseconds; inert
                # unless --commands. STOP/HOLD are prioritized inside CommandChannel.poll().
                if self._cmd is not None:
                    self._drain_command()

                now = time.monotonic()
                # Depth-health heartbeat: log on every state transition + a 10s pulse.
                # Cheap; covers NO-FRAME stretches too, where depth is the usual suspect.
                if self.node is not None:
                    _dh, _dfps = self.node.depth_health(now)
                    if _dh != last_depth_state or (now - last_depth_hb) >= 10.0:
                        last_depth_state = _dh
                        last_depth_hb = now
                        # NOTE: depth_health STATE (FRESH/STALE/DOWN) is age-based and does not by
                        # itself gate forward drive -- the rsrc=='depth' keystone in _track does.
                        # The old '(forward drive disabled)' string was MISLEADING; report the state
                        # plainly and let DEPTH-STARVED below own the fps-floor gating.
                        # ITEM 7(c): surface RGB fps on the SAME 10s/transition pulse (RGB is the
                        # other half of the DRIVE precondition and a common silent-stall culprit).
                        log("DEPTH %s fps=%.1f" % (_dh, _dfps))
                        # NB: BOTH RGB topics feed one callback, so this is the COMBINED message
                        # intake (~2x a single camera's rate on this rig) -- a LIVENESS number.
                        log("RGB fps=%.1f (all-topics) total=%d" % (self.node.rgb_fps(), self.node.frames_total))
                    # ITEM 5 (depth-fps floor): latch TURN-ONLY when sustained depth fps drops below
                    # --min-depth-fps; clear only above floor + --depth-starved-margin (hysteresis).
                    # Only meaningful once depth has actually been seen (not WARMING, count>0); a floor
                    # of 0 disables this entirely (byte-identical to today). _depth_starved is folded
                    # into the existing forbid_forward keystone in _track/_try_coast (TURN-ONLY, no
                    # new stand). Latch-on/clear-off log once each.
                    _floor = float(getattr(self.a, "min_depth_fps", 0.0))
                    # FR-2: require a frame PAIR (_depth_count > 1) -- the EMA is 0.0 until two
                    # frames exist, so a count>0 guard guaranteed a spurious fps=0.0 latch on the
                    # first depth frame of every run. FR-1(8): also latch on age (_dh == "DOWN") --
                    # a DEAD stream freezes the EMA at its last healthy value and a pure fps test
                    # would never fire; clear only when the stream is genuinely FRESH again.
                    if _floor > 0.0 and _dh != "WARMING" and self.node._depth_count > 1:
                        if not self._depth_starved and (_dfps < _floor or _dh == "DOWN"):
                            self._depth_starved = True
                            log("DEPTH-STARVED fps=%.1f state=%s floor=%.1f -> TURN-ONLY (forward vx suppressed)"
                                % (_dfps, _dh, _floor))
                        elif (self._depth_starved and _dh == "FRESH"
                              and _dfps >= (_floor + max(self.a.depth_starved_margin, 0.0))):
                            self._depth_starved = False
                            log("DEPTH-STARVED cleared fps=%.1f >= %.1f -> forward vx re-enabled"
                                % (_dfps, _floor + max(self.a.depth_starved_margin, 0.0)))
                    # Rerun health series (Phase 2): the sensor-truthful fps EMAs + the depth-starved
                    # latch -- the exact signals whose misreading caused a whole session's misdiagnosis.
                    if rerun_sink._RR.ok:
                        rerun_sink._RR.scalar("/health/depth_fps", _dfps)
                        rerun_sink._RR.scalar("/health/rgb_fps", self.node.rgb_fps())
                        rerun_sink._RR.scalar("/health/depth_starved", 1.0 if self._depth_starved else 0.0)
                if frame is not None:
                    ever_framed = True
                    last_frame_mono = now
                    # Low-light boost ONCE here so detection (YOLO/ArUco/color) and
                    # the annotated --stream view all use the same enhanced frame.
                    frame = low_light_boost(frame, self.a.low_light, self.a.ll_dark_thresh)
                    try:
                        self._process_frame(frame)
                        if common._STREAM:
                            self._stream_frame(frame)
                    except Exception as e:  # noqa: BLE001 -- loop must never die
                        log("FRAME-ERR %s" % e)
                        if self.a.stand_on_loss:
                            self._stand()   # perception/processing error -> stable stand
                        else:
                            self._hold()
                else:
                    # F4: throttle BOTH NO-FRAME emissions to ~1/s -- they used to fire every 10Hz
                    # tick, flooding the log through the whole camera warmup / any stall. The
                    # stand/hold enforcement stays OUTSIDE the throttle (fires every tick).
                    if not ever_framed:
                        if (now - self._last_noframe_log) >= 1.0:
                            self._last_noframe_log = now
                            log("NO-FRAME")
                    else:
                        stalled = (now - last_frame_mono) > self.a.stall_seconds
                        if stalled:
                            if (now - self._last_noframe_log) >= 1.0:
                                self._last_noframe_log = now
                                log("NO-FRAME stall=%.1fs -> %s"
                                    % (now - last_frame_mono,
                                       "stand" if self.a.stand_on_loss else "stop"))
                            if self.a.stand_on_loss:
                                self._stand()   # camera frozen -> stable stand, never hold a gait blind
                            else:
                                self._hold()

                # Rerun FSM series (Phase 2): log state EVERY iteration (not just TRACK) so SEARCH/
                # SEARCHING/REACQUIRE show on the scrubber -- the reacquire-spin lives in those states.
                if rerun_sink._RR.ok:
                    rerun_sink._RR.state("/fsm/state", self.state)

                if self.drive and self.walking and not self.bridge.alive():
                    log("BRIDGE died -> exiting (loco safed by bridge)")
                    break

                dt = time.monotonic() - t0
                if rerun_sink._RR.ok:
                    rerun_sink._RR.scalar("/diag/loop_ms", dt * 1000.0)   # true loop dt straight into the .rrd
                # Always-on loop-timing observability (autonomy-ops: observable by default). WORK-time
                # per iteration (before the fill-sleep); a 10s p50/p99/max pulse tagged rerun on/off,
                # so the --rerun loop-cost gate compares directly against the baseline in the logs.
                self._loop_ms_hist.append(dt * 1000.0)
                if (now - self._last_loopms_log) >= 10.0 and len(self._loop_ms_hist) >= 10:
                    self._last_loopms_log = now
                    _xs = sorted(self._loop_ms_hist)
                    _n = len(_xs)
                    _pct = lambda p: _xs[min(_n - 1, int(p * _n))]
                    log("LOOP-MS n=%d p50=%.0f p90=%.0f p99=%.0f max=%.0f budget=%.0f rerun=%s"
                        % (_n, _pct(0.5), _pct(0.9), _pct(0.99), _xs[-1], period * 1000.0,
                           "on" if rerun_sink._RR.ok else "off"))
                # Stage 1 slow-frame guard: surface a perception overrun in the
                # log (visible in --preview) before it ever matters under --drive.
                if dt > period:
                    self._overrun_streak += 1
                    if self._overrun_streak >= 5:
                        log("SLOW-LOOP %d frames over budget (dt=%.0fms > %.0fms target @ %.0fHz)"
                            % (self._overrun_streak, dt * 1000.0, period * 1000.0,
                               self.a.rate_hz))
                        self._overrun_streak = 0
                    # Rerun auto-disable backstop (Phase 2 loop-safety gate item 4): if the loop is
                    # over budget for N consecutive frames WHILE --rerun is on, shed the OPTIONAL
                    # Rerun load FIRST -- disabling rerun_sink._RR makes the follow byte-identical again, well
                    # before the C++ staleness watchdog would have to safe. Conservative default (8)
                    # so a transient perception spike doesn't kill a useful recording; the .rrd's
                    # /diag/rr_track_ms shows whether Rerun was actually the cost. This is a backstop,
                    # NOT the primary gate -- the operator still runs the on-Orin probe before --drive.
                    if rerun_sink._RR.ok:
                        # WARMUP GRACE (fix 2026-07-06, "rerun cuts off mid-run"): the first seconds are
                        # model/TensorRT warmup -- the loop is legitimately slow (p90 ~1s) AND the robot
                        # is not walking yet (SEARCH, ARM-gated), so over-budget frames here are NO safety
                        # risk. Counting them tripped the shed DURING warmup and killed the recording for
                        # the whole run even after the loop recovered to healthy. Don't accumulate the
                        # streak until warmup has elapsed; the shed still fires for a genuinely-overloaded
                        # DRIVING loop after that.
                        if (t0 - self.t_start) < self.a.rerun_warmup_s:
                            self._rr_overrun_streak = 0
                        else:
                            self._rr_overrun_streak += 1
                            if self._rr_overrun_streak >= self.a.rerun_overrun_frames:
                                rerun_sink._RR.ok = False
                                log("RERUN-DISABLED-SLOW %d frames over budget with --rerun -> Rerun OFF "
                                    "(follow safe + byte-identical from here)" % self._rr_overrun_streak)
                                self._rr_overrun_streak = 0
                    # NET-NEW gesture overrun auto-disable (SCOPE §4.3): an INDEPENDENT counter
                    # that does NOT share the SLOW-LOOP reset above (which fires every 5 frames
                    # and could never accumulate to a larger K). Only blames frames where the
                    # gesture pose model actually ran. At K consecutive -> disable gesture so a
                    # 2nd model degrades to no-gesture BEFORE the C++ staleness watchdog must safe.
                    # SCOPED TO DRIVING STATES (field fix 2026-07-02): only an S_TRACK pose frame
                    # (the in-follow STOP gesture -- the June trip case) can destabilize a walking
                    # gait. The ACQUISITION trigger runs only in STATIONARY states (stood/held;
                    # bridge stale tiers 800/3000ms), where a ~104ms frame is harmless -- yet the
                    # unscoped counter was killing the trigger 0.5s into every SEARCH (field log:
                    # SLOW-LOOP dt=104ms x5 -> GESTURE-DISABLED-SLOW while stood, hand-up ignored).
                    if (getattr(self._lock_trigger, "ran_inference", False)
                            and self.state == S_TRACK):
                        self._gesture_overrun_streak += 1
                        if self._gesture_overrun_streak >= self.a.gesture_overrun_frames:
                            self._lock_trigger.on_overrun()
                            self._gesture_overrun_streak = 0
                else:
                    self._overrun_streak = 0
                    self._gesture_overrun_streak = 0   # within budget -> 'consecutive' resets (was cumulative)
                    self._rr_overrun_streak = 0        # same: Rerun auto-disable needs CONSECUTIVE overruns
                    rem = period - dt
                    if rem > 0:
                        time.sleep(rem)
        except KeyboardInterrupt:
            pass
        except Exception as e:  # noqa: BLE001
            log("EXCEPTION %s -> stopping" % e)
        finally:
            # Flush the --rerun .rrd tail on any clean exit (SIGINT/TERM/HUP -> stop -> here). No-op
            # when Rerun is disabled; wrapped so a flush fault can never mask the real exit path.
            try:
                rerun_sink._RR.close()
            except Exception:  # noqa: BLE001
                pass
            self._gbind_session_log()   # A/B rollup (no-op unless --lock-trigger both)
            self._cleanup()
            try:
                self.node.destroy_node()
            except Exception:  # noqa: BLE001
                pass
            try:
                rclpy.shutdown()
            except Exception:  # noqa: BLE001
                pass
            log("EXIT")

    # -- annotated frame for the Tracker page (drawn from stashed viz state) --
    def _stream_frame(self, frame):
        h, w = frame.shape[:2]
        persons = self._viz_persons
        mc = self._viz_mc
        target_box = self.seed.box if self.seed is not None else None

        # all detected persons -> thin gray boxes
        for p in persons:
            x1, y1, x2, y2 = [int(v) for v in p["box"]]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (170, 170, 170), 1)

        # center reticle
        cx0, cy0 = w // 2, h // 2
        cv2.line(frame, (cx0 - 12, cy0), (cx0 + 12, cy0), (200, 200, 200), 1)
        cv2.line(frame, (cx0, cy0 - 12), (cx0, cy0 + 12), (200, 200, 200), 1)

        if self.state == S_TRACK and target_box is not None:
            status = 2
            coasting = (self.seed is not None and self.seed.track_state == TS_COASTING)
            # Amber while coasting on the motion prediction, green while LOCKED.
            box_col = (0, 200, 255) if coasting else (0, 255, 0)
            tid = self.seed.track_id if self.seed is not None else None
            label = ("COASTING id=%s" % tid) if coasting else ("FOLLOWING id=%s" % tid)
            x1, y1, x2, y2 = [int(v) for v in target_box]
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_col, 3)
            cv2.putText(frame, label, (x1, max(16, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, box_col, 2, cv2.LINE_AA)
            if self._viz_range is not None:
                cv2.putText(frame, "range %.2fm  bearing %+.0fdeg  conf %.2f"
                            % (self._viz_range, self._viz_bearing,
                               self.seed.confidence if self.seed is not None else 0.0),
                            (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            box_col, 2, cv2.LINE_AA)
            if coasting:
                banner, bcol = "COASTING - PREDICTING THROUGH OCCLUSION", (0, 140, 200)
            else:
                banner, bcol = "TRACKING - FOLLOWING PERSON", (0, 150, 0)
        elif self.state == S_SEARCHING:
            status = 3
            banner, bcol = "SEARCHING - SCANNING TO RE-FIND YOU", (0, 140, 200)
        elif self.state == S_REACQUIRE:
            status = 3
            # F3: keep the banner consistent with the REACQUIRE vote gate -- the vote is now
            # eligible for the whole REACQUIRE window (reacquire_timeout), not just reloc_seconds,
            # so 'RELOCALIZING' must stay shown for the full stood dwell instead of flipping to
            # 'LOST - SHOW MARKER' at 6s while the vote is still actually running.
            relocating = (self.a.auto_reacquire and self.seed is not None
                          and self.seed.gallery is not None
                          and (time.monotonic() - self.reacquire_since) <= self.a.reacquire_timeout)
            if relocating:
                banner = ("RELOCALIZING - AUTO RE-ACQUIRING (streak %d/%d)"
                          % (self._reloc_streak, self.a.reloc_streak))
                bcol = (0, 90, 200)
            else:
                banner, bcol = "LOST - SHOW MARKER TO RE-SEED", (0, 0, 200)
        elif self.state == S_PARKED:
            status = 3
            banner, bcol = "PARKED - SHOW MARKER TO RESUME", (0, 0, 160)
        else:  # SEARCH
            # Source-aware prompts; with --lock-trigger aruco (default) these are byte-identical
            # to the original "MARKER" / "HOLD UP MARKER" strings.
            _acq = "GESTURE" if (self._viz_lock_src == "gesture") else "MARKER"
            _prompt = "RAISE A HAND" if self.a.lock_trigger == "gesture" else "HOLD UP MARKER"
            if mc is not None and self.marker_streak > 0:
                status = 1
                banner = "ACQUIRING %s (%d/%d)" % (_acq, self.marker_streak, self.a.seed_frames)
                bcol = (0, 120, 200)
            else:
                status = 0
                banner, bcol = "SEARCHING - " + _prompt, (60, 60, 60)

        if mc is not None:   # marker indicator
            cv2.circle(frame, (int(mc[0]), int(mc[1])), 8, (255, 255, 0), 2)

        # Commanded HOLD overrides the state banner so an operator WAIT is visually distinct
        # from a perception-loss stand (SCOPE_nl_command_layer.md invariant 10).
        if self.commanded_hold:
            banner, bcol, status = "HELD - COMMANDED WAIT (RESUME to continue)", (0, 0, 160), 3

        cv2.rectangle(frame, (0, 0), (w, 30), bcol, -1)
        cv2.putText(frame, banner, (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2, cv2.LINE_AA)
        emit_frame(status, frame, self.a.stream_quality)

    # -- per-frame state machine -------------------------------------------
    def _process_frame(self, frame):
        h_img, w_img = frame.shape[:2]

        # YOLO persons every frame (defensive; [] on any failure).
        persons = self.det.detect(frame)
        self._frame_idx += 1   # Stage 2: monotonic frame counter (admit spacing)

        # Stage 1: stamp a stable track_id + motion prediction on each person.
        # Annotation only -- never changes which person exists; a tracker fault
        # leaves persons untouched so we degrade to the stateless path.
        if self.tracker is not None:
            try:
                self.tracker.update(persons)
            except Exception as e:  # noqa: BLE001 -- tracking must never kill the loop
                log("TRACK-ERR %s (stateless fallback this frame)" % e)

        # Commanded HOLD (SCOPE_nl_command_layer.md §2.3): a latched operator WAIT FREEZES the
        # whole FSM in a stand -- placed BEFORE trigger detection (so the gesture pose model is
        # not run while held) and before any state branch, so NO auto-resume / reseed / relock /
        # coast can move a held robot. _stand() is idempotent. Cleared only by RESUME/FOLLOW (a
        # later slice). Distinct from perception-loss self.standing (which auto-resumes on re-find).
        if self.commanded_hold:
            self._viz_persons = persons
            self._viz_mc = None
            self._stand()
            if (time.monotonic() - self._hold_log_last) >= 3.0:
                self._hold_log_last = time.monotonic()
                log("HELD (commanded WAIT) %.1fs stood; awaiting RESUME%s"
                    % (time.monotonic() - self.hold_since,
                       "" if (self.drive and self.walking) else " [preview]"))
            return

        # Lock-trigger presence + stable-lock streak (used by SEARCH and REACQUIRE).
        # The trigger PRODUCES the lock point mc (ArUco marker, or a raised hand); the
        # streak math below is byte-identical to the original marker block, and with
        # --lock-trigger aruco the trigger returns exactly marker_center(frame).
        hint = self._lock_trigger.detect(frame, persons, self.state, w_img, h_img)
        self._lock_hint = hint
        mc = hint.point if hint is not None else None
        if mc is not None:
            self.marker_streak = min(self.marker_streak + 1, 9999)
        else:
            self.marker_streak = 0
        marker_locked = mc is not None and self.marker_streak >= self.a.seed_frames
        self._viz_persons = persons   # stash for the annotated stream
        self._viz_mc = mc
        self._viz_lock_src = hint.source if hint is not None else None
        # A/B audit (both-mode only): gesture compared AUDIT-ONLY; ArUco still drives.
        if self.a.lock_trigger == "both":
            self._gesture_audit(frame, persons, mc, marker_locked, w_img, h_img)

        if self.state == S_SEARCH:
            if marker_locked:
                if self._try_seed(
                        frame, persons, mc, w_img, h_img,
                        point_kind=(self._lock_hint.point_kind if self._lock_hint else "marker"),
                        owner_box=(self._lock_hint.owner_box if self._lock_hint else None),
                        owner_tid=(self._lock_hint.owner_tid if self._lock_hint else None)):
                    return
            log("SEARCH persons=%d lock=%s src=%s streak=%d/%d%s"
                % (len(persons), "Y" if mc is not None else "n",
                   (self._viz_lock_src or "none"), self.marker_streak, self.a.seed_frames,
                   "" if (self.drive and self.walking) else " [preview]"))
            return

        if self.state == S_TRACK:
            # In-follow STOP gesture (opt-in): the FOLLOWED person raising BOTH hands -> commanded
            # WAIT. Decimated pose in this DRIVING state -- justified because it ONLY de-escalates
            # (a late frame just stops slightly later), and the overrun auto-disable + C++ watchdog
            # still bound it. Needs a pose backend (gesture/both); a no-op under aruco.
            if self.a.gesture_stop and self.seed is not None:
                _g = getattr(self._lock_trigger, "gesture", self._lock_trigger)
                if hasattr(_g, "stop_gesture"):
                    fired = _g.stop_gesture(frame, persons, self.seed.track_id)
                    self._lock_trigger.ran_inference = _g.ran_inference   # so the overrun guard counts it
                    if fired:
                        self.commanded_hold = True
                        self.hold_since = time.monotonic()
                        self._hold_log_last = 0.0
                        self._stand()
                        log("GESTURE-STOP both hands up -> commanded WAIT (latched stand; RESUME to continue)")
                        return
            self._track(frame, persons, w_img, h_img)
            return

        if self.state == S_SEARCHING:
            # Yaw-only rotate toward the last-known bearing to sweep the lost person
            # back into the FOV. NO forward motion (vx=vy=0 -- never drive toward an
            # unseen target). Stays in WALKING mode (came straight from a walking TRACK,
            # so no stand/re-walk). The auto-relock vote (OSNet) and the marker both
            # re-lock; time-boxed -> _stand() + REACQUIRE if it can't re-find them.
            searched = time.monotonic() - self.search_since
            if (self.a.auto_reacquire and self.seed is not None and self.seed.gallery is not None
                    and not marker_locked
                    and (time.monotonic() - self.reloc_since) <= self.a.reloc_seconds):
                if self._try_reacquire(frame, persons, w_img, h_img):
                    log("AUTO-RELOCK (during SEARCH @%.1fs) -> TRACK" % searched)
                    return
            if marker_locked:
                if self._try_seed(
                        frame, persons, mc, w_img, h_img,
                        point_kind=(self._lock_hint.point_kind if self._lock_hint else "marker"),
                        owner_box=(self._lock_hint.owner_box if self._lock_hint else None),
                        owner_tid=(self._lock_hint.owner_tid if self._lock_hint else None)):
                    log("RESEED via marker (during SEARCH) -> TRACK")
                    return
            if searched >= self.a.search_timeout:
                log("SEARCH timed out (%.1fs, no re-lock) -> stand + REACQUIRE" % searched)
                if self.a.stand_on_loss:
                    self._stand()
                else:
                    self._hold()
                self.state = S_REACQUIRE
                self.reacquire_since = time.monotonic()
                # REACQUIRE gets its OWN fresh passive-relock window. The window stamped at loss
                # (reloc_since) was already consumed by this SEARCH phase -- search_timeout (8s) >
                # reloc_seconds (6s) -- so without this the stand-still REACQUIRE vote is pre-expired
                # and never fires. Reset the streak too, mirroring the loss-time reset.
                self.reloc_since = time.monotonic()
                self._reloc_streak = 0
                self._reloc_track_id = None
                self._reacq_range_streak = 0   # REG-3: fresh in-range streak entering REACQUIRE
                self._reacq_range_id = None
                return
            # SEARCH DWELL (field finding 2026-07-02): with someone in frame, HOLD the scan
            # (yaw -> 0 via the slew) instead of sweeping past them -- the re-lock vote needs
            # ~5 consecutive same-track frames, and rotating at full yaw blurred embeddings,
            # churned tracker ids, and left the vote at g=0.51-0.54 vs the 0.55 floor. Bounded:
            # the --search-timeout still stands the robot, so a bystander can stall the scan at
            # most until the normal timeout (fail-safe direction). --no-search-dwell restores
            # the continuous sweep.
            _sy_target = self.search_dir * self.a.search_yaw
            if persons and getattr(self.a, "search_dwell", False):
                _sy_target = 0.0
            sy = self._slew(self._prev_vyaw, _sy_target, self.vyaw_slew)
            sy = clamp(sy, self.vyaw_min, self.vyaw_max)
            self._drive_vel(0.0, 0.0, sy)   # yaw-only scan (gated by drive+walking; inert in preview)
            if (searched - self._search_log_last) >= 0.5:
                self._search_log_last = searched
                log("SEARCH %.1f/%.1fs yaw=%+.2f persons=%d%s"
                    % (searched, self.a.search_timeout, sy, len(persons),
                       "" if (self.drive and self.walking) else " [preview]"))
            return

        if self.state == S_REACQUIRE:
            # Keep YOLO running but DO NOT follow by appearance after a hard loss;
            # require the marker to re-seed. Hold a stable STAND (kPrepare) so a
            # long abandonment can never end in a fall. _stand() is idempotent.
            if self.a.stand_on_loss:
                self._stand()
            else:
                self._hold()
            # Stage 3: passive markerless re-acquire (default OFF). Runs ONLY after the
            # STAND above (zero motion), commands no motion itself, and re-locks only on
            # a strict sustained appearance vote. The marker still wins -- 'not
            # marker_locked' also keeps the (potentially blocking) resume off a marker
            # frame. On HS this votes+logs but never re-locks (self._relock_armed False).
            # F3: STOOD in REACQUIRE it is SAFE to keep voting for the whole dwell, so the
            # vote is eligible for the entire REACQUIRE window (reacquire_timeout) rather than
            # the SEARCH (WALKING) bound reloc_seconds. The 6s reloc_seconds cap exists only to
            # stop voting forever WHILE WALKING (SEARCH, the L1699 gate) -- it must NOT pre-expire
            # the zero-motion stand. Without this, persons reappear after 6s but the vote never
            # fires (only the marker can recover). reacquire_since is set on REACQUIRE entry;
            # window closes exactly when waited>=reacquire_timeout below (-> PARKED).
            if (self.a.auto_reacquire and self.seed is not None and self.seed.gallery is not None
                    and not marker_locked
                    and (time.monotonic() - self.reacquire_since) <= self.a.reacquire_timeout):
                if self._try_reacquire(frame, persons, w_img, h_img):
                    log("AUTO-RELOCK -> TRACK")
                    return
            elif (time.monotonic() - self.reacquire_since) > self.a.reacquire_timeout:
                self._reloc_streak = 0      # window expired -> no stale streak leaks in
                self._reloc_track_id = None
            if marker_locked:
                if self._try_seed(
                        frame, persons, mc, w_img, h_img,
                        point_kind=(self._lock_hint.point_kind if self._lock_hint else "marker"),
                        owner_box=(self._lock_hint.owner_box if self._lock_hint else None),
                        owner_tid=(self._lock_hint.owner_tid if self._lock_hint else None)):
                    log("RESEED via marker -> TRACK")
                    return
            waited = time.monotonic() - self.reacquire_since
            if waited >= self.a.reacquire_timeout:
                # Give-up gets its OWN exit edge: REACQUIRE -> PARKED. Still stood;
                # PARKED keeps watching the marker (recovery) + heartbeats + has a
                # bounded clean-exit, so REACQUIRE is no longer a marker-only dead-end.
                self.state = S_PARKED
                self.parked_since = time.monotonic()
                self._parked_log_last = 0.0
                log("REACQUIRE timed out (%.1fs) -> PARKED (stood; show marker to resume)" % waited)
                return
            log("REACQUIRE persons=%d marker=%s streak=%d waited=%.1fs%s"
                % (len(persons), "Y" if mc is not None else "n", self.marker_streak,
                   waited, "" if (self.drive and self.walking) else " [preview]"))
            return

        if self.state == S_PARKED:
            # Named give-up state. Stay STOOD (idempotent kPrepare); command NO motion.
            # Recovery: a stable marker re-seeds -> TRACK (the frozen anchor == the SAME
            # person). Bounded exit: after --park-timeout (>0) with no marker, end the
            # session CLEANLY -- set the stop flag so run()'s finally -> _cleanup() ->
            # bridge EOF -> kPrepare. With --park-timeout 0 (default) it stays parked
            # until the --max-seconds session watchdog (deliberately terminal-until-then).
            if self.a.stand_on_loss:
                self._stand()
            else:
                self._hold()
            if marker_locked:
                if self._try_seed(
                        frame, persons, mc, w_img, h_img,
                        point_kind=(self._lock_hint.point_kind if self._lock_hint else "marker"),
                        owner_box=(self._lock_hint.owner_box if self._lock_hint else None),
                        owner_tid=(self._lock_hint.owner_tid if self._lock_hint else None)):
                    log("RESEED via marker -> TRACK (from PARKED)")
                    return
            parked = time.monotonic() - self.parked_since
            if self.a.park_timeout > 0.0 and parked >= self.a.park_timeout:
                log("PARKED %.1fs >= park-timeout %.1fs -> clean exit (robot safed: stop+kPrepare)"
                    % (parked, self.a.park_timeout))
                self._stop_requested = True   # run() loop exits -> finally cleanup safes loco
                return
            if (parked - self._parked_log_last) >= 3.0:   # throttled heartbeat (never go silent)
                self._parked_log_last = parked
                tail = (" exit@%.0fs" % self.a.park_timeout) if self.a.park_timeout > 0.0 else ""
                log("PARKED %.1fs stood; show marker to resume%s%s"
                    % (parked, tail, "" if (self.drive and self.walking) else " [preview]"))
            return

    # -- SEEDED (one-shot): pick the person at the marker, record identity --
    def _try_seed(self, frame, persons, mc, w_img, h_img,
                  point_kind="marker", owner_box=None, owner_tid=None):
        if not persons:
            return False
        mx, my = mc
        diag = math.hypot(w_img, h_img)

        # Gesture lock point: owner-authoritative selection with STRICTER gates than the
        # marker path (SCOPE_lock_trigger_gesture.md §2.1). The marker path below is
        # byte-identical -- point_kind defaults to "marker" for every existing caller.
        if point_kind == "gesture":
            chosen = self._select_gesture_owner(persons, mx, my, diag, owner_tid)
            if chosen is None:
                return False
            return self._seed_from(frame, chosen, kind="gesture")

        # ---- marker selection (BYTE-IDENTICAL to pre-change). NOTE: the A/B audit
        # replays this exact logic READ-ONLY in _marker_choice (SCOPE §3.3, intentional
        # duplication). Any change here MUST be mirrored there or 'both'-mode measures a
        # different marker policy than production drives. ----
        def _plausible(p):
            # Marker is held/taped at a standing person: it should fall in the
            # lower-center of THEIR bbox, not the head or an arm's-length edge.
            x1, y1, x2, y2 = p["box"]
            bw = max(x2 - x1, 1.0); bh = max(y2 - y1, 1.0)
            fx = (mx - x1) / bw
            fy = (my - y1) / bh
            return (0.15 <= fx <= 0.85) and (fy >= 0.25)

        # 1) persons whose bbox plausibly contains the marker (lower-center).
        containing = []
        for p in persons:
            x1, y1, x2, y2 = p["box"]
            if x1 <= mx <= x2 and y1 <= my <= y2 and _plausible(p):
                containing.append(p)

        chosen = None
        if len(containing) == 1:
            chosen = containing[0]
        elif len(containing) >= 2:
            # Ambiguous: two people overlap the marker. Take the marker's owner
            # only if ONE is clearly closest; else REFUSE (ask for a clean shot).
            containing.sort(key=lambda p: math.hypot(p["cx"] - mx, p["cy"] - my))
            d0 = math.hypot(containing[0]["cx"] - mx, containing[0]["cy"] - my)
            d1 = math.hypot(containing[1]["cx"] - mx, containing[1]["cy"] - my)
            if d0 <= 0.6 * d1:
                chosen = containing[0]
            else:
                log("SEED-AMBIGUOUS %d persons contain marker -> refusing (show marker clearly, one person)"
                    % len(containing))
                return False
        else:
            # 2) else nearest person centroid within a tight threshold.
            near = min(persons, key=lambda p: math.hypot(p["cx"] - mx, p["cy"] - my))
            d = math.hypot(near["cx"] - mx, near["cy"] - my)
            if d <= self.a.seed_near_frac * diag and _plausible(near):
                chosen = near

        if chosen is None:
            return False

        return self._seed_from(frame, chosen, kind="marker")

    def _seed_from(self, frame, chosen, kind="marker"):
        """Shared identity tail for BOTH the marker and gesture lock paths: record the
        appearance anchor via the configured feat_fn (HS / striped / OSNet -- so "gesture
        -> OSNet seed" is automatic), enforce the sticky-lock reseed floor, and construct
        the SOLE Seed. Extracted VERBATIM from the marker path (uses only frame/chosen/self),
        so identity is still created/replaced ONLY here (PLAN.md §6) and the marker path is
        behaviour-identical to the inline tail it replaces."""
        hist = self.feat_fn(frame, chosen["box"])
        if hist is None:
            log("SEED-REJECT color signature unavailable -> retry")
            return False
        # STICKY-LOCK: after the first lock, a RE-SEED must be the SAME person -- compare the
        # candidate to the FROZEN anchor and refuse to re-lock onto a different identity even
        # if they trigger (marker OR gesture). --reseed-anchor-floor 0 disables this check.
        prev_anchor = self.seed.anchor_hist if self.seed is not None else None
        if prev_anchor is not None and self.a.reseed_anchor_floor > 0.0:
            rs = self.sim_fn(prev_anchor, hist)
            if rs < self.a.reseed_anchor_floor:
                log("SEED-REJECT identity-mismatch sim=%.2f < %.2f (not the locked person) -> keep asking for the original person"
                    % (rs, self.a.reseed_anchor_floor))
                return False
        self.seed = Seed(chosen["box"], (chosen["cx"], chosen["cy"]),
                         hist, chosen["conf"])
        if prev_anchor is not None:
            self.seed.anchor_hist = prev_anchor   # keep the ORIGINAL frozen anchor across re-seeds
        # Stage 1: bind the lock to this person's motion track so we can prefer
        # the SAME id next frame and coast through a brief occlusion.
        self.seed.track_id = chosen.get("track_id")
        self.seed.track_state = TS_LOCKED
        self.seed.miss_streak = 0
        # Stage 2: build the anchored gallery on the FROZEN anchor (slot-0). The
        # anchor is the same across re-seeds (preserved above), so identity is still
        # created/replaced ONLY here. --gallery-size 1 leaves it None (Stage-1 path).
        if self.a.gallery_size > 1:
            self.seed.gallery = TargetGallery(
                self.seed.anchor_hist, self.feat_fn, self.sim_fn,
                self.a.gallery_size, self.a.distractor_size,
                self.a.anchor_floor, self.a.bank_floor,
                self.a.admit_conf, self.a.admit_spacing)
        else:
            self.seed.gallery = None
        self.seed_prev_centroid = self.seed.centroid
        self.state = S_TRACK
        self.lost_count = 0
        self.marker_streak = 0
        log("LOCKED person seeded box=(%d,%d,%d,%d) c=(%d,%d) conf=%.2f -- %s handoff complete"
            % (int(chosen["box"][0]), int(chosen["box"][1]),
               int(chosen["box"][2]), int(chosen["box"][3]),
               int(chosen["cx"]), int(chosen["cy"]), chosen["conf"], kind))
        return True

    # -- gesture lock-point -> person selection (SCOPE §2.1 G1-G4) -----------
    def _select_gesture_owner(self, persons, mx, my, diag, owner_tid):
        """Owner-authoritative selection with a bystander-adjacency refuse. Returns the chosen
        person dict or None (logged); the shared identity tail (reseed floor, Seed) then runs
        in _seed_from. The OPERATIVE gates are G1 (the pose model already resolved exactly which
        tracked person raised a hand -> we never re-derive an anonymous point) and G4 (refuse if
        a bystander is too close to be unambiguous). The lock point here is the owner-box lower-
        centre, so G2/G3 are cheap DEFENSIVE validity checks (degenerate boxes / future point
        derivations), NOT the primary protection -- never the nearest-centroid guessing the
        marker path allows."""
        # G1 (operative): owner is authoritative -- the tracked person the pose model fired on.
        chosen = None
        if owner_tid is not None:
            for p in persons:
                if p.get("track_id") == owner_tid:
                    chosen = p
                    break
        if chosen is None:
            log("GESTURE-SEED-REJECT owner track_id=%s not associable this frame -> no seed"
                % owner_tid)
            return None
        x1, y1, x2, y2 = chosen["box"]
        bw = max(x2 - x1, 1.0); bh = max(y2 - y1, 1.0)
        # G2 (defensive): point must be inside the owner box; NEVER a nearest-centroid fallback
        # for gesture (a hand point is far from any body centroid -> nearest could grab a stranger).
        if not (x1 <= mx <= x2 and y1 <= my <= y2):
            log("GESTURE-SEED-REJECT point not contained in owner box -> no seed (no nearest fallback)")
            return None
        # G3 (defensive): the point must resolve to a full standing body, not a head/edge.
        fx = (mx - x1) / bw; fy = (my - y1) / bh
        if not (0.05 <= fx <= 0.95 and fy <= 0.6):
            log("GESTURE-SEED-REJECT geometry fx=%.2f fy=%.2f out of gate -> no seed" % (fx, fy))
            return None
        # G4 (operative): bystander-adjacency -- refuse if any OTHER person is within min-sep.
        min_sep = self.a.gesture_min_sep_frac * diag
        for q in persons:
            if q is chosen:
                continue
            if _point_box_dist(mx, my, q["box"]) < min_sep:
                log("GESTURE-SEED-REJECT bystander within %.0fpx of lock point -> ambiguous, no seed"
                    % min_sep)
                return None
        return chosen

    # -- A/B audit: read-only marker-choice replay + gesture-vs-ArUco compare --
    def _marker_choice(self, persons, mc, w_img, h_img):
        """READ-ONLY replay of the marker selection (_try_seed marker path) to find which
        track_id ArUco WOULD seed from mc, with NO side effects. DUPLICATED (not refactored)
        from _try_seed per SCOPE §3.3 -- drift risk in a read-only audit is cheaper than any
        edit to the sole Seed creator. Returns (track_id|None, reason)."""
        if mc is None or not persons:
            return None, "nopoint"
        mx, my = mc
        diag = math.hypot(w_img, h_img)

        def _plausible(p):
            x1, y1, x2, y2 = p["box"]
            bw = max(x2 - x1, 1.0); bh = max(y2 - y1, 1.0)
            fx = (mx - x1) / bw; fy = (my - y1) / bh
            return (0.15 <= fx <= 0.85) and (fy >= 0.25)

        containing = []
        for p in persons:
            x1, y1, x2, y2 = p["box"]
            if x1 <= mx <= x2 and y1 <= my <= y2 and _plausible(p):
                containing.append(p)
        if len(containing) == 1:
            return containing[0].get("track_id"), "contain1"
        if len(containing) >= 2:
            containing.sort(key=lambda p: math.hypot(p["cx"] - mx, p["cy"] - my))
            d0 = math.hypot(containing[0]["cx"] - mx, containing[0]["cy"] - my)
            d1 = math.hypot(containing[1]["cx"] - mx, containing[1]["cy"] - my)
            if d0 <= 0.6 * d1:
                return containing[0].get("track_id"), "contain2"
            return None, "ambig"
        near = min(persons, key=lambda p: math.hypot(p["cx"] - mx, p["cy"] - my))
        d = math.hypot(near["cx"] - mx, near["cy"] - my)
        if d <= self.a.seed_near_frac * diag and _plausible(near):
            return near.get("track_id"), "near"
        return None, "none"

    def _gesture_audit(self, frame, persons, mc, marker_locked, w_img, h_img):
        """A/B observability for --lock-trigger both. ArUco DRIVES; this compares the
        gesture trigger's choice against ArUco's, replaying BOTH the marker selection
        (_marker_choice) and the sticky-lock reseed floor (SCOPE §3.4) so 'stranger' is
        the production-relevant event, not a track_id-disagreement proxy. Read-only:
        never seeds, never drives. Crash-safe (never kills the loop)."""
        gb = self._gb
        if gb is None:
            return
        try:
            gb["frames"] += 1
            g_hint = getattr(self._lock_trigger, "last_gesture_hint", None)
            g_tid = g_hint.owner_tid if g_hint is not None else None
            a_tid, a_reason = self._marker_choice(persons, mc, w_img, h_img)
            if len(persons) >= 2:
                gb["bystander"] = True

            now = time.monotonic()
            if a_tid is None and g_tid is None and mc is None:
                # Episode boundary: both triggers quiet -> reset the per-episode event state.
                gb["ep_start"] = None
                gb["a_locked_ep"] = False
                gb["g_locked_ep"] = False
                gb["g_streak"] = 0
                gb["g_streak_tid"] = None
                gb["ep_bystander"] = False
                gb["a_lock_tid"] = None
                gb["g_lock_tid"] = None
                gb["ep_recorded"] = False
            else:
                if gb["ep_start"] is None:
                    gb["ep_start"] = now
                if len(persons) >= 2:
                    gb["ep_bystander"] = True
                # ArUco stable-lock onto a real person = ONE acquisition OPPORTUNITY (the unit).
                if marker_locked and a_tid is not None and not gb["a_locked_ep"]:
                    gb["a_locked_ep"] = True
                    gb["a_lock_tid"] = a_tid
                    gb["a_ttl"].append(now - gb["ep_start"])
                    gb["acq_opps"] += 1
                    if gb["ep_bystander"]:
                        gb["acq_bystander"] += 1
                if g_tid is not None and g_tid == gb["g_streak_tid"]:
                    gb["g_streak"] += 1
                elif g_tid is not None:
                    gb["g_streak_tid"] = g_tid
                    gb["g_streak"] = 1
                else:
                    gb["g_streak"] = 0
                    gb["g_streak_tid"] = None
                if gb["g_streak"] >= self.a.seed_frames and not gb["g_locked_ep"]:
                    gb["g_locked_ep"] = True
                    gb["g_lock_tid"] = gb["g_streak_tid"]
                    gb["g_ttl"].append(now - gb["ep_start"])
                # When BOTH have stable-locked this episode, record ONE seed-agreement event
                # (per-episode, not per-frame). Different ids = a seed disagreement -- the
                # safety-relevant event, and the ONLY one that covers a first-seed wrong-person
                # pick (no anchor yet, so the reseed-floor 'stranger' metric is blind to it).
                if (gb["a_locked_ep"] and gb["g_locked_ep"] and not gb["ep_recorded"]):
                    gb["ep_recorded"] = True
                    gb["acq_both"] += 1
                    if gb["a_lock_tid"] == gb["g_lock_tid"]:
                        gb["acq_agree"] += 1
                    else:
                        gb["acq_seed_disagree"] += 1
                        log("GBIND-SEED-DISAGREE a_id=%s g_id=%s bystander=%s -- gesture would "
                            "seed a DIFFERENT person than ArUco at this acquisition"
                            % (gb["a_lock_tid"], gb["g_lock_tid"],
                               "Y" if gb["ep_bystander"] else "n"))

            both = (a_tid is not None and g_tid is not None)
            agree = both and (a_tid == g_tid)
            ftrig = (g_tid is not None and mc is None)
            miss = (marker_locked and g_tid is None)
            if both:
                gb["active"] += 1
                gb["agree" if agree else "disagree"] += 1
            elif g_tid is not None:
                gb["g_only"] += 1
            elif a_tid is not None:
                gb["a_only"] += 1
            if ftrig:
                gb["ftrig"] += 1
            if miss:
                gb["miss"] += 1

            # reseed-floor replay (SCOPE §3.4): a STRANGER is gesture picking a DIFFERENT
            # identity that WOULD PASS the sticky-lock floor (the floor failing to catch it).
            stranger = False
            floorpass = False
            if (self.seed is not None and g_hint is not None
                    and g_hint.owner_box is not None and self.seed.anchor_hist is not None):
                feat = self.feat_fn(frame, g_hint.owner_box)
                if feat is not None:
                    sim = self.sim_fn(self.seed.anchor_hist, feat)
                    floorpass = (self.a.reseed_anchor_floor <= 0.0
                                 or sim >= self.a.reseed_anchor_floor)
                    different = (g_tid is not None and g_tid != self.seed.track_id)
                    stranger = bool(different and floorpass)
            if stranger:
                gb["stranger"] += 1

            if (now - gb["last_log"]) >= 0.5 and (g_tid is not None or mc is not None):
                gb["last_log"] = now
                log("GBIND g=%s gtid=%s gstreak=%d | a=%s atid=%s areason=%s astreak=%d "
                    "| agree=%s ftrig=%s miss=%s stranger=%s floorpass=%s tid=%s"
                    % ("Y" if g_tid is not None else "n", g_tid, gb["g_streak"],
                       "Y" if a_tid is not None else "n", a_tid, a_reason, self.marker_streak,
                       "Y" if agree else "n", "Y" if ftrig else "n", "Y" if miss else "n",
                       "Y" if stranger else "n", "Y" if floorpass else "n",
                       "on" if self.a.track else "off"))
        except Exception as e:  # noqa: BLE001 -- audit must never kill the loop
            log("GBIND-ERR %s" % e)

    def _gbind_session_log(self):
        """One grep-able GBIND-SESSION rollup at clean shutdown -- what the flip gate
        (SCOPE §3.7) reads. Crash-safe; only emits in --lock-trigger both."""
        gb = self._gb
        if gb is None:
            return
        try:
            active = max(gb["active"], 1)
            frames = max(gb["frames"], 1)

            def _med(xs):
                if not xs:
                    return 0.0
                s = sorted(xs); n = len(s)
                return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])

            log("GBIND-SESSION frames=%d active=%d agree=%d disagree=%d g_only=%d a_only=%d "
                "stranger=%d agree_pct=%.1f ftrig_pct=%.1f miss_pct=%.1f g_ttl=%.2fs a_ttl=%.2fs "
                "bystander_present=%s track=%s every_n=%d"
                % (gb["frames"], gb["active"], gb["agree"], gb["disagree"], gb["g_only"],
                   gb["a_only"], gb["stranger"], 100.0 * gb["agree"] / active,
                   100.0 * gb["ftrig"] / frames, 100.0 * gb["miss"] / frames,
                   _med(gb["g_ttl"]), _med(gb["a_ttl"]),
                   "Y" if gb["bystander"] else "n", "on" if self.a.track else "off",
                   self.a.gesture_every_n))
            # EPISODE-level rollup -- THIS is the line the flip gate reads (sim-eval: unit =
            # acquisition opportunity, not frame). seed_disagree/acq_bystander are the
            # safety-relevant counts; N for the rule-of-three is acq_bystander, NOT sessions.
            opps = max(gb["acq_opps"], 1); both = max(gb["acq_both"], 1)
            log("GBIND-EPISODES acq_opps=%d acq_bystander=%d both_locked=%d seed_agree=%d "
                "seed_disagree=%d seed_agree_pct=%.1f -- flip-gate N(rule-of-three)=acq_bystander; "
                "seed_disagree and stranger MUST be 0; agree_pct is per-EPISODE not per-frame"
                % (gb["acq_opps"], gb["acq_bystander"], gb["acq_both"], gb["acq_agree"],
                   gb["acq_seed_disagree"], 100.0 * gb["acq_agree"] / both))
        except Exception as e:  # noqa: BLE001
            log("GBIND-SESSION-ERR %s" % e)

    # -- TRACK: associate target, follow its centroid -----------------------
    def _associate(self, frame, persons, w_img, h_img):
        """Return (best, best_cost, best_sim, second_cost, best_hist, track_locked)
        considering ONLY candidates that match the FROZEN sticky-lock anchor. Other
        people in frame are simply IGNORED (not followed, and NOT counted as a loss).
        second_cost is among the anchor-matching set. Returns best=None only when the
        locked person isn't present.

        track_locked is True when the chosen candidate IS the bound motion track
        (Stage 1): it is then BOTH motion- and appearance-consistent, so the caller
        may skip the look-alike ambiguity guard. It still had to pass anchor_floor,
        so a mis-associated track can never carry the lock onto a different identity."""
        if self.seed is None or not persons:
            return None, None, None, None, None, False
        diag = math.hypot(w_img, h_img)
        sx, sy = self.seed.centroid
        s_area = max(self.seed.size, 1.0)
        use_anchor = (self.seed.anchor_hist is not None and self.a.anchor_floor > 0.0)

        gal = self.seed.gallery
        scored = []
        _aud_b = None; _aud_o = 0.0               # P0-C cosine audit (gated by --audit-cosine)
        _bound_tid = self.seed.track_id if self.seed is not None else None
        for p in persons:
            if "_ph" in p:                        # Stage-4: reuse the batched osnet embedding
                ph = p["_ph"]
            else:
                ph = self.feat_fn(frame, p["box"])
                p["_ph"] = ph                     # cache so _track banking need not recompute
            if self.a.audit_cosine and gal is not None and gal.anchor_feat is not None:
                _av = gal.sim_fn(gal.anchor_feat, ph)
                if p.get("track_id") == _bound_tid:
                    _aud_b = _av
                elif _av > _aud_o:
                    _aud_o = _av
            # STICKY-LOCK / slot-0 AND-veto: the FROZEN anchor must pass anchor_floor
            # as a HARD necessary condition. The gallery NEVER grants admission a
            # slot-0 failure would deny -- it only reorders survivors via a bounded
            # cost bonus. Other people in frame are ignored (neither switched to NOR
            # counted as a loss). The anchor-only branch is byte-identical to Stage 1.
            if gal is not None:
                if use_anchor and not gal.anchor_pass(ph):
                    continue
                anchor_sim, g_sim, d_sim = gal.score(ph)
            else:
                if use_anchor and self.sim_fn(self.seed.anchor_hist, ph) < self.a.anchor_floor:
                    continue
                anchor_sim = g_sim = d_sim = 0.0
            d = math.hypot(p["cx"] - sx, p["cy"] - sy) / max(diag, 1.0)
            sim = self.sim_fn(self.seed.hist, ph)   # EMA self-sim (feeds hiconf gate); feat-agnostic
            area = max(p["w"] * p["h"], 1.0)
            size_term = clamp(abs(math.log(area / s_area)) / 2.0, 0.0, 1.0)
            cost = (self.a.w_centroid * d
                    + self.a.w_color * (1.0 - sim)
                    + self.a.w_size * size_term)
            if gal is not None:
                cost -= self.a.w_gallery * max(0.0, g_sim - anchor_sim)        # gallery LOWERS cost
                cost += min(self.a.w_distractor * max(0.0, d_sim - g_sim),     # distractor RAISES cost
                            self.a.w_distractor)                               # clamped: can't alone exceed gate
            scored.append((cost, sim, p, ph))
        if self.a.audit_cosine and gal is not None:
            log("AUDIT bound_anchor=%s best_other_anchor=%.2f (cosine; tune --anchor-floor)"
                % (("%.2f" % _aud_b) if _aud_b is not None else "n/a", _aud_o))
        if not scored:
            return None, None, None, None, None, False  # locked person not visible this frame
        scored.sort(key=lambda t: t[0])
        # Stage 1 track-id preference: if the bound motion track is among the
        # anchor-matching candidates, take it (motion + appearance both agree) and
        # report track_locked so the caller bypasses the ambiguity guard. The
        # runner-up is then the best AMONG OTHER ids, for logging/telemetry.
        if self.tracker is not None and self.seed.track_id is not None:
            for cost, sim, p, ph in scored:
                if p.get("track_id") == self.seed.track_id:
                    others = [c for (c, _s, q, _h) in scored
                              if q.get("track_id") != self.seed.track_id]
                    second = min(others) if others else None
                    return p, cost, sim, second, ph, True
        best_cost, best_sim, best, best_hist = scored[0]
        second_cost = scored[1][0] if len(scored) >= 2 else None
        return best, best_cost, best_sim, second_cost, best_hist, False

    def _embed_persons(self, frame, persons):
        """Stage-4 osnet: batch-compute the embedding for every person ONCE per frame and
        stamp p['_ph'] (with optional per-track every-N caching). No-op for global/striped
        (so they stay byte-identical) and when the engine isn't loaded. Always re-embeds the
        bound target and anyone NOT isolated (overlapping); evicts retired track ids."""
        # == not `is not`: self.reid.embed is a FRESH bound-method object each access, so the old
        # `feat_fn is not self.reid.embed` was ALWAYS True -- this method NO-OP'd, the batched embed
        # pass never ran, and the ITEM-4 mid-run fault watchdog below could never fire. == compares
        # __self__/__func__ (matches the arm-gate ~L1721 and the relock re-check). Compute-neutral at
        # --reid-every-n 1: embed_batch per-row here == the per-box embeds _associate did lazily.
        if (self.reid is None or not self.reid.ok or self.feat_fn != self.reid.embed
                or not persons):
            return
        n = max(1, int(self.a.reid_every_n))
        bound = self.seed.track_id if self.seed is not None else None
        boxes = []; need = []
        for i, p in enumerate(persons):
            tid = p.get("track_id")
            cached = self._reid_cache.get(tid) if tid is not None else None
            iso = max_iou_other(persons, i, tid) < self.a.bbox_iou_isolate
            if (n <= 1 or cached is None or tid is None or tid == bound or not iso
                    or (self._frame_idx - cached[0]) >= n):
                boxes.append(p["box"]); need.append(i)
            else:
                p["_ph"] = cached[1]
        if boxes:
            feats = self.reid.embed_batch(frame, boxes)
            any_valid = False
            for k, i in enumerate(need):
                f = feats[k] if k < len(feats) else None
                persons[i]["_ph"] = f
                if f is not None:
                    any_valid = True
                tid = persons[i].get("track_id")
                if n > 1 and tid is not None and f is not None:   # Phase 1.4: cache is only CONSULTED when n>1
                    self._reid_cache[tid] = (self._frame_idx, f)
            # ITEM 4 (mid-run fault watchdog): boxes was non-empty, so embed_batch was asked to embed
            # a real person set. All-None == the engine faulted this frame. Shared with the
            # _try_reacquire vote path so recovery is also observable during REACQUIRE.
            self._reid_watchdog(any_valid)
        if n > 1 and self.tracker is not None:        # Phase 1.4: bound the cache (only the n>1 path writes it)
            live = set(self.tracker.tracks.keys())
            for tid in list(self._reid_cache.keys()):
                if tid not in live:
                    del self._reid_cache[tid]

    def _track(self, frame, persons, w_img, h_img):
        self._embed_persons(frame, persons)   # Stage-4: one batched osnet embed pass / frame
        best, cost, sim, second_cost, best_hist, track_locked = \
            self._associate(frame, persons, w_img, h_img)

        # Ambiguity guard: if a runner-up (among the ANCHOR-MATCHING candidates) is
        # nearly as good, we can't safely tell the target apart -> no-match (prefer stop).
        # _associate already dropped non-matching people, so two unrelated bystanders
        # never trip this -- only two people who both resemble the locked person do.
        # SKIP it when track_locked: the bound motion track already disambiguated
        # (motion + appearance agree), so two look-alikes no longer force a stop.
        ambiguous = (not track_locked
                     and second_cost is not None and cost is not None
                     and (second_cost - cost) < self.a.assoc_margin)

        if best is None or cost is None or cost > self.a.gate or ambiguous:
            # No confident, unambiguous, anchor-matched target this frame. Because
            # _associate ignores anyone who doesn't match the locked anchor, OTHER
            # people in frame do NOT cause a loss -- only the locked person actually
            # being absent (best is None) or two look-alikes being ambiguous does.
            #
            # Stage 1: before escalating, try to COAST on the bound track's motion
            # prediction (bridges a brief occlusion -- failure mode C -- yaw-only,
            # never driving toward an unseen target). Only triggers when the locked
            # person has NO detection this frame; an ambiguous-but-visible target
            # falls through to the held-grace path below.
            if self._try_coast(w_img, h_img):
                return
            self.lost_count += 1
            self._viz_range = None   # no live target this frame
            why = "ambiguous" if ambiguous else "no-match"
            if self.lost_count >= self.a.lost_grace:
                self.marker_streak = 0
                # Stage 3: open the passive re-acquire window + reset the vote (used by
                # BOTH the new SEARCH scan and the REACQUIRE stand).
                self.reloc_since = time.monotonic()
                self._reloc_streak = 0
                self._reloc_track_id = None
                self._reacq_range_streak = 0   # REG-3: fresh in-range streak per loss episode
                self._reacq_range_id = None
                self._reid_cache = {}   # Stage-4: re-acquire must vote on FRESH embeddings
                if self.seed is not None:
                    self.seed.track_state = TS_LOCKED   # reset sub-state on hard loss
                    self.seed.miss_streak = 0
                if (self.a.search_on_loss and self.drive and self.walking
                        and self.seed is not None):
                    # SEARCH: stay WALKING and rotate yaw-only toward the bearing the
                    # target exited, to sweep them back into the FOV (the auto-relock
                    # vote re-IDs them there). No stand/re-walk -- we came straight from
                    # a walking TRACK. Bounded by --search-timeout -> stand + REACQUIRE.
                    self.state = S_SEARCHING
                    self.search_since = time.monotonic()
                    self._search_log_last = -9.0
                    # bearing>0 == target was RIGHT of center -> turn right (vyaw<0); <0 -> left.
                    self.search_dir = -1.0 if self._viz_bearing >= 0.0 else 1.0
                    log("LOST target (%d frames, %s) -> SEARCH (rotate %s toward last bearing %+.0fdeg)"
                        % (self.lost_count, why,
                           "right" if self.search_dir < 0 else "left", self._viz_bearing))
                else:
                    # Fail-safe STAND (or hold) then REACQUIRE (marker / passive vote).
                    if self.a.stand_on_loss:
                        self._stand()
                    else:
                        self._hold()
                    self.state = S_REACQUIRE
                    self.reacquire_since = time.monotonic()
                    log("LOST target (%d frames, %s, best_cost=%s) -> %s + REACQUIRE (show marker)%s"
                        % (self.lost_count, why,
                           ("%.2f" % cost) if cost is not None else "n/a",
                           "stand" if self.a.stand_on_loss else "stop",
                           "" if (self.drive and self.walking) else " [preview]"))
            else:
                # within grace: hold velocity at zero (stay in gait), keep looking.
                # Brief occlusions do NOT escalate to a stand (avoids mode thrash).
                self._hold()
                log("NO-PERSON grace=%d/%d (%s)%s"
                    % (self.lost_count, self.a.lost_grace, why,
                       "" if (self.drive and self.walking) else " [preview]"))
            return

        # Accepted match -> update seed, follow.
        self.lost_count = 0
        # Phase 2.4 (ID-stability debounce): a look-alike crossing can make ByteTrack hop the bound
        # track id for a frame or two; the unconditional re-bind below would instantly re-point the
        # follow at the wrong body. When the accepted candidate carries a DIFFERENT id than the held
        # seed, require the new id to persist --idsw-debounce-frames before committing the switch; until
        # then HOLD on the last-good centroid (never a new forward command -> INV-1) and keep the old
        # binding (-> INV-4). Same-id frames clear the pending and proceed byte-identically.
        _bid = best.get("track_id")
        _sid = self.seed.track_id if self.seed is not None else None
        if self.seed is not None and _bid is not None and _sid is not None and _bid != _sid:
            self._idsw_frames = (self._idsw_frames + 1) if _bid == self._idsw_pending else 1
            self._idsw_pending = _bid
            if self._idsw_frames < max(1, int(self.a.idsw_debounce_frames)):
                self._hold()   # hold last-good; do NOT re-point at an unconfirmed new id
                log("ID-DEBOUNCE track_id %s->%s %d/%d -> hold last-good (anti wrong-person-lock)%s"
                    % (_sid, _bid, self._idsw_frames, int(self.a.idsw_debounce_frames),
                       "" if (self.drive and self.walking) else " [preview]"))
                return
            log("ID-DEBOUNCE track_id %s->%s confirmed after %d frames -> switch"
                % (_sid, _bid, self._idsw_frames))
        else:
            self._idsw_pending = None
            self._idsw_frames = 0
        # Stage 1: we have the anchored person in view again -> LOCKED. Keep the
        # track binding fresh (the candidate passed anchor_floor, so re-binding to
        # its id can never move identity off the frozen anchor).
        if self.seed is not None:
            self.seed.track_state = TS_LOCKED
            self.seed.miss_streak = 0
            self.seed.confidence = clamp(1.0 - cost / max(self.a.gate, 1e-6), 0.0, 1.0)
            if best.get("track_id") is not None:
                self.seed.track_id = best["track_id"]
        cx, cy = target_point_from_box(best)
        self.seed_prev_centroid = self.seed.centroid   # BEFORE overwrite (EMA jump guard)
        self.seed.centroid = (cx, cy)
        self.seed.box = best["box"]
        self.seed.size = max(best["w"] * best["h"], 1.0)
        self.seed.conf = best["conf"]

        # Geometry up-front so the follow-range geofence can gate BEFORE we resume
        # walking (never resume just to stand again). Reused by the control law below.
        rng, rsrc = self._range_for(best, cx, cy, w_img, h_img)
        bearing = bearing_from_x(cx, w_img, self.a.hfov_deg)
        bearing_deg = math.degrees(bearing)
        self._viz_range = rng; self._viz_bearing = bearing_deg   # for the annotated stream

        # OPS follow-range geofence -- a PRECONDITION gate, NOT spliced into the control law.
        # FAR-side geofence ONLY (field fix 2026-07-03): STAND when the target is beyond
        # --max-follow-range (a sprint / far depth glitch), with --range-hysteresis so it can't
        # chatter at the boundary. The CLOSE side is deliberately NO LONGER a stand: standing
        # when the person is within --min-safe-range FROZE the follow -- a marker seeds you in
        # close (~0.36m), so the geofence stood the robot 130+ frames and never resumed until
        # you backed past min_safe+hys (0.9m). Close-range safety is the per-frame forward-vx
        # floor below (forbids driving TOWARD a too-close person) PLUS the standoff control law,
        # which BACKS OFF to restore standoff -- the correct, non-frozen behavior. Depth-only
        # (a bboxH guess can never trip it). --max-follow-range defaults 0 (off) unless the
        # app's Range-fence toggle sets it, so by default this block is inert.
        if rsrc == "depth" and rng is not None and self.a.max_follow_range > 0.0:
            m = max(self.a.range_hysteresis, 0.0)
            if self._range_gated:
                self._range_gated = rng > (self.a.max_follow_range - m)
            else:
                self._range_gated = rng > self.a.max_follow_range
            if self._range_gated:
                if self.a.stand_on_loss:
                    self._stand()
                else:
                    self._hold()
                log("RANGE-GATE rng=%.2f[depth] > max_follow=%.2f hys=%.2f -> %s, not pursuing%s"
                    % (rng, self.a.max_follow_range, m,
                       "stand" if self.a.stand_on_loss else "hold",
                       "" if (self.drive and self.walking) else " [preview]"))
                return

        # If a fail-safe stand put us in kPrepare (stall/error/hard-loss) and we
        # have now re-found the ANCHORED target, re-enter walking before driving.
        if self.drive and self.standing:
            if not self._resume_walk():
                self._hold()   # re-walk failed -> stay standing, do not drive
                return
        # Stage 2: hoist diag/jump ABOVE the hiconf gate so both the EMA update and
        # gallery.admit (which must fire on sub-hiconf back-pose/shadow frames -- the
        # exact frames Stage 2 targets) can use them. (In Stage 1 they lived inside
        # the hiconf block, which would NameError the gallery admit.)
        diag = math.hypot(w_img, h_img)
        jump = math.hypot(cx - self.seed_prev_centroid[0],
                          cy - self.seed_prev_centroid[1]) / max(diag, 1.0)

        # Color EMA only on a CONFIDENT, NON-TELEPORTING match -- gate on the seed.hist
        # self-sim (NOT the gallery max), so the signature can never ratchet onto a stranger.
        if sim >= self.a.hiconf and jump <= self.a.ema_max_jump:
            new_hist = best_hist   # reuse the hist _associate already computed
            if new_hist is not None and self.seed.hist is not None:
                self.seed.hist = ((1.0 - self.a.color_ema) * self.seed.hist
                                  + self.a.color_ema * new_hist)
            elif new_hist is not None:
                self.seed.hist = new_hist

        # Stage 2: gallery maintenance -- additive, crash-safe, never blocks control.
        # Admit the accepted target's view when ISOLATED (admit also enforces conf /
        # non-teleport / spacing / bank_floor); bank every OTHER isolated person whose
        # track_id != the target as a distractor. Isolation gating means a crossing or
        # overlap can never poison the gallery NOR bank the target against itself.
        if self.seed.gallery is not None:
            try:
                gal = self.seed.gallery
                bi = next((i for i, q in enumerate(persons) if q is best), None)
                iso_best = (bi is not None and
                            max_iou_other(persons, bi, self.seed.track_id) < self.a.bbox_iou_isolate)
                if gal.admit(best_hist, self.seed.confidence, jump,
                             self.a.ema_max_jump, iso_best, self._frame_idx):
                    a_s, _g, _d = gal.score(best_hist)
                    log("GALLERY-ADMIT f=%d id=%s anchor_sim=%.2f conf=%.2f jump=%.3f g=%d"
                        % (self._frame_idx, self.seed.track_id, a_s,
                           self.seed.confidence, jump, len(gal.gallery)))
                for j, q in enumerate(persons):
                    if q is best:
                        continue
                    if q.get("track_id") == self.seed.track_id:   # never bank the target itself
                        continue
                    qh = q.get("_ph")
                    if qh is None:
                        qh = self.feat_fn(frame, q["box"])
                    iso_q = max_iou_other(persons, j) < self.a.bbox_iou_isolate
                    if gal.add_distractor(qh, iso_q):
                        a_q, _g, _d = gal.score(qh)
                        log("GALLERY-BANK f=%d id=%s anchor_sim=%.2f iso=%s d=%d"
                            % (self._frame_idx, q.get("track_id"), a_q, iso_q, len(gal.distractors)))
            except Exception as e:  # noqa: BLE001 -- gallery must never break the loop
                log("GALLERY-ERR %s (anchor-only this frame)" % e)

        # Range + bearing were computed up-front (for the geofence) and are reused here.
        # SAME control law + HARD clamps used throughout.
        vyaw = self._slew(self._prev_vyaw, -self.a.k_yaw * bearing, self.vyaw_slew)
        vyaw = clamp(vyaw, self.vyaw_min, self.vyaw_max)
        if rng is not None:
            err = rng - self.a.standoff_m
            vx = self.a.k_vx * err if abs(err) > self.a.deadband_m else 0.0
        else:
            # No range estimate at all -> don't drive forward/back; just turn.
            vx = 0.0
        # SAFETY (depth keystone): only a DEPTH-validated range may authorize FORWARD
        # motion. The bbox-height pinhole (rsrc=="bboxH") is unreliable AND still returns
        # a number during a depth blackout, so it must NEVER drive the robot toward the
        # target -- it may only ever back off (vx<=0). Forward follow resumes the instant
        # depth returns. This makes a depth dropout degrade to safe turn-only, not creep.
        # Forward vx is FORBIDDEN this frame if the range is not depth-validated (keystone: a
        # bboxH guess may never drive forward), the depth-validated range is at/inside the close-
        # range floor (back off even on a correct close reading -- one tick ahead of, and additive
        # to, the hysteresis stand), or this is the first TRACK frame after an armed relock (one-
        # shot). On ANY of these the robot may only back off (vx<=0) -- SACRED. Captured as ONE
        # flag so it is RE-APPLIED AFTER the slew: an accel-limited ramp from a high baseline
        # toward 0 (e.g. _slew(+0.18,0,0.06)=+0.12) would otherwise re-leak a forbidden forward
        # command the clamp cannot pull back to 0 (INV-1).
        forbid_forward = ((rsrc != "depth")
                          or (rng is not None and self.a.min_safe_range > 0.0
                              and rng <= self.a.min_safe_range)
                          or self._postrelock_noforward
                          # ITEM 5: sustained depth-fps below --min-depth-fps -> TURN-ONLY. Composes
                          # additively with the keystone above; re-applied after the slew below
                          # (same as the other forbid_forward causes) so no forward command leaks.
                          or self._depth_starved)
        if forbid_forward and vx > 0.0:
            vx = 0.0
        # OBSTACLE-BRAKE (Phase 3): cap forward vx by the corridor clearance -- computed ONCE here,
        # applied before the slew (so the slew ramps toward the cap) and re-applied after (like
        # forbid_forward). Only ever reduces forward vx; yaw untouched. Inert unless --obstacle-brake.
        _obs_cap, _clr = self._obstacle_vx_cap(rng)
        if _obs_cap is not None and vx > _obs_cap:
            vx = _obs_cap
        # FIX F1: keep a short window of recent VALIDATED depth ranges (its median is the glitch-
        # resistant reference for the relock jump gate), stamped for ageing. SKIP on a post-relock
        # one-shot frame so a glitched relock range can never poison the window (F1-glitch).
        if rsrc == "depth" and rng is not None and not self._postrelock_noforward:
            self._track_range_hist.append(rng)
            self._track_range_hist = self._track_range_hist[-5:]
            self._track_range_t = time.monotonic()
        self._postrelock_noforward = False   # one-shot consumed on this matched-track frame
        vx = self._slew(self._prev_vx, vx, self.vx_slew)
        if forbid_forward and vx > 0.0:       # SACRED: the slew must never re-leak a forbidden forward
            vx = 0.0
        if _obs_cap is not None and vx > _obs_cap:   # re-apply the obstacle cap after the slew (INV-1)
            vx = _obs_cap
        vx = clamp(vx, self.vx_min, self.vx_max)

        self._drive_vel(vx, 0.0, vyaw)
        # OBSTACLE-BRAKE observability: throttled CLEARANCE line + Rerun scalar when engaged.
        if self.a.obstacle_brake and _clr is not None:
            _nowm = time.monotonic()
            if _obs_cap is not None and (_nowm - self._last_clr_log) >= 1.0:
                self._last_clr_log = _nowm
                log("CLEARANCE %.2fm -> vx-cap %.2f (brake %.1f..%.1f)"
                    % (_clr, _obs_cap, self.a.obstacle_brake_stop, self.a.obstacle_brake_start))
            if rerun_sink._RR.ok:
                rerun_sink._RR.scalar("/reflex/clearance_m", _clr)
                rerun_sink._RR.scalar("/reflex/vx_cap", _obs_cap if _obs_cap is not None else self.vx_max)

        log("TRACK id=%s%s c=(%d,%d) range=%s[%s] bearing=%+05.1fdeg vx=%+.2f vyaw=%+.2f cost=%.2f/2nd=%s sim=%.2f conf=%.2f%s"
            % (best.get("track_id"), " LOCK" if track_locked else "",
               int(cx), int(cy),
               ("%.2f" % rng) if rng is not None else "n/a", rsrc,
               bearing_deg, vx, vyaw, cost,
               ("%.2f" % second_cost) if second_cost is not None else "-",
               sim, best["conf"],
               "" if (self.drive and self.walking) else " [preview]"))

        # Rerun follow scalars (Phase 2): control-loop, CHEAP signals only -- NO images here. Uses the
        # SAME truthful post-clamp vx/vyaw the robot was just commanded (self._drive_vel above) and the
        # REAL forbid_forward boolean (not the Phase-1 derivation). frame_idx/state are set once per
        # iteration in run(); this only adds the follow-specific series + the target box. Self-timed to
        # /diag/rr_track_ms so the on-Orin loop-cost gate can read p99 straight from the .rrd. Inert
        # unless --rerun (single-branch skip when rerun_sink._RR.ok is False -> byte-identical).
        if rerun_sink._RR.ok:
            _rr_t0 = time.monotonic()
            rerun_sink._RR.scalar("/follow/range", rng)
            rerun_sink._RR.scalar("/follow/range_source", 2.0 if rsrc == "depth" else (1.0 if rsrc == "bboxH" else 0.0))
            rerun_sink._RR.scalar("/follow/bearing", bearing_deg)
            rerun_sink._RR.scalar("/cmd/vx", vx)
            rerun_sink._RR.scalar("/cmd/vyaw", vyaw)
            rerun_sink._RR.scalar("/cmd/forbid_forward", 1.0 if forbid_forward else 0.0)
            rerun_sink._RR.scalar("/reid/sim", sim)
            rerun_sink._RR.scalar("/track/conf", best["conf"])
            rerun_sink._RR.scalar("/track/cost", cost)
            rerun_sink._RR.boxes("/camera/rgb/target", best["box"], best.get("track_id"))
            rerun_sink._RR.scalar("/diag/rr_track_ms", (time.monotonic() - _rr_t0) * 1000.0)

    # -- COASTING: follow the bound track's motion prediction through occlusion --
    def _try_coast(self, w_img, h_img):
        """Stage 1. When the locked person has NO accepted detection this frame but
        their bound motion track was seen within the last --coast-frames frames,
        keep the lock by following the track's PREDICTED centroid -- YAW ONLY (vx is
        forced to 0 unless validated depth is present AND --coast-vx-scale>0). This
        bridges brief occlusions/crossings without stopping or re-demanding the
        marker. Returns True if it coasted (caller must not escalate); False (no-op)
        when coasting is disabled, no track is bound, the track is being updated this
        frame (target is visible -> not an occlusion), or the coast budget is spent."""
        if (self.tracker is None or self.a.coast_frames <= 0
                or self.seed is None or self.seed.track_id is None):
            return False
        # Coasting commands motion; only meaningful while actually walking. If a
        # fail-safe stand already put us in kPrepare, the hard-loss path owns it.
        if self.drive and not self.walking:
            return False
        trk = self.tracker.get(self.seed.track_id)
        # time_since_update == 0 means a detection matched the track THIS frame (the
        # person is visible but was rejected by appearance/ambiguity) -> do NOT coast.
        # Only coast on a genuine no-detection gap, within budget.
        if trk is None or trk.time_since_update <= 0:
            return False
        if trk.time_since_update > self.a.coast_frames:
            return False

        cx, cy = trk.centroid()
        pbox = trk.predicted_box()
        self.seed.track_state = TS_COASTING
        self.seed.miss_streak = trk.time_since_update
        self.seed.pred_centroid = (cx, cy)
        self.seed.centroid = (cx, cy)            # keep the lock centred on the prediction
        self.seed.box = pbox
        frac = trk.time_since_update / float(max(self.a.coast_frames, 1))
        self.seed.confidence = clamp(1.0 - frac, 0.0, 1.0)

        synth = {"box": pbox, "cx": cx, "cy": cy,
                 "w": max(pbox[2] - pbox[0], 1.0), "h": max(pbox[3] - pbox[1], 1.0),
                 "conf": self.seed.conf}
        rng, rsrc = self._range_for(synth, cx, cy, w_img, h_img)
        bearing = bearing_from_x(cx, w_img, self.a.hfov_deg)
        bearing_deg = math.degrees(bearing)
        self._viz_range = rng; self._viz_bearing = bearing_deg

        vyaw = self._slew(self._prev_vyaw, -self.a.k_yaw * bearing, self.vyaw_slew)
        vyaw = clamp(vyaw, self.vyaw_min, self.vyaw_max)
        # SACRED: never drive forward on a predicted/extrapolated range. Forward
        # motion while coasting is OFF by default (coast_vx_scale 0.0) and, even when
        # enabled, requires a VALIDATED depth measurement -- not a bbox-height guess.
        vx = 0.0
        if self.a.coast_vx_scale > 0.0 and rng is not None and rsrc == "depth":
            err = rng - self.a.standoff_m
            if abs(err) > self.a.deadband_m:
                vx = self.a.coast_vx_scale * self.a.k_vx * err
        # Forward vx forbidden this coast frame if inside the close-range floor (validated depth
        # only) or on the first frame after an armed relock. The one-shot is honored AND consumed
        # HERE too: _try_coast runs before _track's consume, so a first-frame coast must not bypass
        # it (F1-coast). Re-applied after the slew so the ramp can't re-leak it (INV-1).
        forbid_forward = ((rng is not None and rsrc == "depth" and self.a.min_safe_range > 0.0
                           and rng <= self.a.min_safe_range)
                          or self._postrelock_noforward
                          # ITEM 5: depth-starved -> TURN-ONLY on the coast path too (coast forward vx
                          # is already gated to validated depth; this suppresses it when depth is slow).
                          or self._depth_starved)
        if forbid_forward and vx > 0.0:
            vx = 0.0
        self._postrelock_noforward = False   # one-shot consumed on the coast path too
        vx = self._slew(self._prev_vx, vx, self.vx_slew)
        if forbid_forward and vx > 0.0:       # SACRED: the slew must never re-leak a forbidden forward
            vx = 0.0
        vx = clamp(vx, self.vx_min, self.vx_max)
        self._drive_vel(vx, 0.0, vyaw)

        log("COAST id=%s %d/%d c=(%d,%d) range=%s[%s] bearing=%+05.1fdeg vx=%+.2f vyaw=%+.2f conf=%.2f%s"
            % (self.seed.track_id, trk.time_since_update, self.a.coast_frames,
               int(cx), int(cy),
               ("%.2f" % rng) if rng is not None else "n/a", rsrc,
               bearing_deg, vx, vyaw, self.seed.confidence,
               "" if (self.drive and self.walking) else " [preview]"))
        return True

    # -- RELOCALIZING: passive markerless re-acquire (Stage 3) ---------------
    def _try_reacquire(self, frame, persons, w_img, h_img):
        """STAGE 3 passive vote. Commands NO motion (no _drive_vel / yaw / walk). On
        HS self._relock_armed is False -> runs the full vote and LOGS every decision
        but NEVER re-locks (audit-only). Identity is NEVER created here (no Seed(...),
        no anchor_hist write); an armed re-lock only REFRESHES box/centroid/size/
        track_id while keeping the frozen anchor + gallery. Returns True only on an
        armed re-lock. Crash-safe -> marker-only on any fault."""
        try:
            gal = self.seed.gallery
            if gal is None or not persons:
                self._reloc_streak = 0; self._reloc_track_id = None
                return False
            if self.seed.anchor_hist is None or self.a.anchor_floor <= 0.0:
                return False   # never relock without a frozen anchor to gate on

            survivors = []   # (g_sim, anchor_sim, d_sim, idx, person, k_views, via_multi)
            # FRESH features (a stale cached TRACK-frame embedding must never feed the vote).
            # == not `is`: self.reid.embed makes a FRESH bound-method object each access, so
            # `feat_fn is self.reid.embed` is ALWAYS False (it silently forced the per-box fallback
            # below instead of the batched embed_batch path). Matches L1273/L2372; == compares
            # __self__/__func__. embed(frame,box) == embed_batch(frame,[box])[0], so the per-
            # candidate vectors are identical either way -- this is perf-only, no decision shift.
            if self.reid is not None and self.reid.ok and self.feat_fn == self.reid.embed:
                _rfeats = self.reid.embed_batch(frame, [pp["box"] for pp in persons])
                # Feed the fault watchdog from the vote path too: _embed_persons only runs in
                # TRACK, so without this a latched REID-DEGRADED could never DE-latch during
                # REACQUIRE even though fresh valid embeddings prove the engine recovered.
                self._reid_watchdog(any(f is not None for f in _rfeats))
            else:
                _rfeats = [self.feat_fn(frame, pp["box"]) for pp in persons]
            for i, p in enumerate(persons):
                ph = _rfeats[i]
                a_s, g_s, d_s, k_s = gal.relock_score(ph, self.a.reloc_view_floor,
                                                      self.a.reloc_dedup_ceiling)
                iso = max_iou_other(persons, i, p.get("track_id")) < self.a.bbox_iou_isolate
                # Slot-0 identity: the FROZEN anchor (default, byte-identical), OR -- only
                # when --reloc-gallery-gate is on -- k>=N mutually-distinct STRONG admitted
                # gallery views WITH a relaxed-but-NONZERO anchor backstop (a candidate that
                # mismatches the anchor entirely is still vetoed). Lets a turned/relit target
                # re-lock; a stranger matches NONE of the true-target views.
                pass_anchor = (a_s >= self.a.anchor_floor)
                pass_multi = (self.a.reloc_gallery_gate
                              and k_s >= self.a.reloc_min_views
                              and a_s >= self.a.reloc_anchor_backstop)
                via_multi = pass_multi and not pass_anchor
                # F2: a target lost BECAUSE it got crowded (overlapping another person) fails
                # iso exactly when re-lock is needed (run-log id=17 anchor=0.56 g=0.70 k=3 was
                # rejected on iso=False alone). Allow the iso veto to be SKIPPED only for a
                # candidate whose FROZEN-anchor match (pass_anchor, NEVER the relaxed multi-
                # view branch) is strong enough that identity is not in doubt. The two-candidate
                # AMBIGUITY gate (below) and the (g_s - d_s) separation/margin gates still stand,
                # so a crowd-MERGED blob (two equally-strong people) cannot win.
                iso_bypass = (pass_anchor and a_s >= self.a.reloc_iso_bypass_anchor)
                ok = ((pass_anchor or pass_multi)               # slot-0 (anchor OR multi-shot)
                      and g_s >= self.a.reloc_floor
                      and (g_s - d_s) >= self.a.reloc_margin
                      and (g_s - d_s) >= self.a.reloc_sep
                      and (iso or iso_bypass))
                if ok:
                    survivors.append((g_s, a_s, d_s, i, p, k_s, via_multi))
                else:
                    # FR-4: per-frame per-person REJECT lines flood the log in a crowd (10-45/s).
                    # Throttle to ~1/s -- the values repeat near-identically frame-to-frame, so
                    # one line a second preserves the diagnostic signal.
                    _rn = time.monotonic()
                    if (_rn - self._reloc_log_last) >= 1.0:
                        self._reloc_log_last = _rn
                        log("RELOC-REJECT id=%s via=%s anchor=%.2f g=%.2f d=%.2f k=%d margin=%.2f iso=%s byp=%s"
                            % (p.get("track_id"), "multi" if via_multi else "anchor",
                               a_s, g_s, d_s, k_s, g_s - d_s, iso, iso_bypass))

            if not survivors:
                self._reloc_streak = 0; self._reloc_track_id = None
                return False
            survivors.sort(key=lambda t: t[0], reverse=True)     # best g_sim first
            if len(survivors) >= 2 and (survivors[0][0] - survivors[1][0]) < self.a.reloc_sep:
                self._reloc_streak = 0; self._reloc_track_id = None
                return False     # two plausible candidates -> ambiguous, do not relock

            best = survivors[0][4]
            bid = best.get("track_id")
            if bid == self._reloc_track_id:
                self._reloc_streak += 1
            else:
                self._reloc_track_id = bid
                self._reloc_streak = 1

            if self._reloc_streak < self.a.reloc_streak:
                return False
            win = survivors[0]
            best_g, best_d, best_k, best_via_multi = win[0], win[2], win[5], win[6]
            if not self._relock_armed:
                # FR-4: fires EVERY frame once the streak holds -- log the streak-threshold
                # crossing immediately, then refresh at ~1/s.
                _rn = time.monotonic()
                if self._reloc_streak == self.a.reloc_streak or (_rn - self._reloc_log_last) >= 1.0:
                    self._reloc_log_last = _rn
                    log("RELOC-VOTE-PASS id=%s via=%s streak=%d g=%.2f k=%d (audit-only, NOT re-locking)"
                        % (bid, "multi" if best_via_multi else "anchor", self._reloc_streak, best_g, best_k))
                return False     # audit-only: zero behavioral/identity change

            # ---- ARMED re-lock. DELIBERATELY STRICTER than the audit vote (it DRIVES). ----
            # (1) ITEM 4: refuse an ARMED (driving) re-lock while the mid-run OSNet fault watchdog is
            #     LATCHED. feat_fn/reid.ok are init-only (never mutate mid-run), so the old
            #     feat_fn==reid.embed re-check here guarded an IMPOSSIBLE state and never fired on a
            #     real fault. The REAL signal is _reid_degraded, latched in _embed_persons after
            #     --reid-fault-k consecutive all-None embed frames; it de-latches on a valid embed.
            #     This actually forces armed -> audit-only when re-ID is silently dead.
            if self._reid_degraded:
                log("RELOC-VOTE-PASS id=%s streak=%d (REID-DEGRADED all-none -> audit-only)"
                    % (bid, self._reloc_streak))
                return False
            # (2) longer ARMED streak + wider ARMED margin than the audit vote.
            if self._reloc_streak < self.a.reloc_arm_streak:
                return False
            if (best_g - best_d) < self.a.reloc_arm_margin:
                log("RELOC-HOLD id=%s margin=%.2f < arm=%.2f -> not re-locking"
                    % (bid, best_g - best_d, self.a.reloc_arm_margin))
                return False
            # (3) anti-teleport: a candidate admitted via the RELAXED gallery branch must be
            #     spatially + dimensionally plausible vs the last-seen target.
            if best_via_multi:
                dpx = math.hypot(best["cx"] - self.seed.centroid[0], best["cy"] - self.seed.centroid[1])
                rr = (best["w"] * best["h"]) / max(self.seed.size, 1.0)
                jlim = self.a.reloc_max_jump_frac * math.hypot(w_img, h_img)
                if dpx > jlim or not (1.0 / self.a.reloc_scale_tol <= rr <= self.a.reloc_scale_tol):
                    log("RELOC-REJECT-TELEPORT id=%s dpx=%.0f lim=%.0f scale=%.2f"
                        % (bid, dpx, jlim, rr))
                    self._reloc_streak = 0; self._reloc_track_id = None
                    return False

            # ---- F1 ARMED-relock RANGE admission gate. A depth GLITCH during a SEARCH-path
            # relock (run-log: 9.15m/7.17m for a 0.73m target) drove a full-speed lunge. Before
            # committing an armed relock, require N consecutive in-range, same-id frames on a
            # VALIDATED depth source, and reject an implausible depth-range jump vs the last
            # tracked range. SACRED depth-only: acts ONLY on rsrc=='depth'; HOLD writes nothing
            # (identity stays frozen). Inside the L2300 try (crash-safe). vx never set here. ----
            rng_c, rsrc_c = self._range_for(best, best["cx"], best["cy"], w_img, h_img)
            # Robust glitch-resistant reference: the MEDIAN of recent VALIDATED TRACK ranges, used
            # ONLY while still fresh (--reloc-range-stale-s). Once stale (after a long loss) the
            # jump check is skipped so a target who legitimately walked far can still re-lock
            # (REG-1); require-depth + the absolute cap + the N-frame streak still reject a glitch.
            ref = None
            if (self._track_range_hist and self._track_range_t is not None
                    and (time.monotonic() - self._track_range_t) <= self.a.reloc_range_stale_s):
                _sl = sorted(self._track_range_hist); ref = _sl[len(_sl) // 2]
            _bad = (
                (self.a.reloc_require_depth and rsrc_c != "depth")
                # Absolute plausibility CAP: re-ID at long range is unreliable and a follow target
                # does not teleport metres away. Rejects the 9.15m/7.17m run-log glitch outright,
                # independent of the reference (so a glitched pre-loss baseline can't authorise a
                # far relock -- EFF-2). 0 disables. (Separate from the TRACK --max-follow-range.)
                or (rng_c is not None and self.a.reloc_max_range_m > 0.0
                    and rng_c > self.a.reloc_max_range_m)
                # Aged relative JUMP vs the fresh TRACK-range median (catches mid-range glitches).
                or (rng_c is not None and rsrc_c == "depth" and ref is not None
                    and self.a.reloc_max_range_jump_m > 0.0
                    and abs(rng_c - ref) > self.a.reloc_max_range_jump_m))
            if bid == self._reacq_range_id and not _bad:
                self._reacq_range_streak += 1
            else:
                self._reacq_range_id = bid
                self._reacq_range_streak = 0 if _bad else 1
            if _bad or self._reacq_range_streak < max(self.a.reloc_range_streak, 1):
                _rn = time.monotonic()   # FR-4: ~1/s (values repeat frame-to-frame while held)
                if (_rn - self._reloc_log_last) >= 1.0:
                    self._reloc_log_last = _rn
                    log("RELOC-HOLD-RANGE id=%s rsrc=%s rng=%s ref=%s streak=%d"
                        % (bid, rsrc_c, ("%.2f" % rng_c) if rng_c is not None else None,
                           ("%.2f" % ref) if ref is not None else None, self._reacq_range_streak))
                return False
            # ---- commit: resume-THEN-commit. Robot safely STANDING during the ~5s resume. ----
            if self.drive and self.standing:
                if not self._resume_walk():          # blocks ~5s; robot safely standing
                    self._hold()
                    return False                     # resume failed -> stay S_REACQUIRE
            # identity UNCHANGED: keep anchor_hist/gallery/distractors/hist; refresh pose only.
            # F1: arm the one-shot that forbids FORWARD vx on the first TRACK frame after this
            # relock (consumed in _track), so even a residual depth glitch cannot lunge on entry.
            self._postrelock_noforward = True
            self.seed.box = best["box"]
            self.seed.centroid = (best["cx"], best["cy"])
            self.seed.size = max(best["w"] * best["h"], 1.0)
            self.seed.track_id = bid
            self.seed.track_state = TS_LOCKED
            self.seed.miss_streak = 0
            self.seed_prev_centroid = self.seed.centroid
            self.state = S_TRACK
            self.lost_count = 0
            self.marker_streak = 0
            self._reloc_streak = 0; self._reloc_track_id = None
            log("AUTO-RELOCK id=%s via=%s g=%.2f k=%d -> TRACK (anchor kept)"
                % (bid, "multi" if best_via_multi else "anchor", best_g, best_k))
            return True
        except Exception as e:  # noqa: BLE001 -- never let a re-acquire attempt kill the loop
            self._reloc_streak = 0; self._reloc_track_id = None
            log("RELOC-ERR %s -> marker-only" % e)
            return False

    # -- range from depth ROI else bbox-height pinhole ----------------------
    def _range_for(self, person, cx, cy, w_img, h_img):
        depth = self.node.latest_depth()
        if depth is not None:
            try:
                dh, dw = depth.shape[:2]
                # depth image may differ in size from rgb; scale centroid.
                u = int(clamp(cx * (dw / max(w_img, 1)), 0, dw - 1))
                v = int(clamp(cy * (dh / max(h_img, 1)), 0, dh - 1))
                rad = 4
                x1 = max(0, u - rad); x2 = min(dw, u + rad + 1)
                y1 = max(0, v - rad); y2 = min(dh, v + rad + 1)
                roi_all = depth[y1:y2, x1:x2].reshape(-1)
                roi = roi_all[np.isfinite(roi_all)]
                roi = roi[roi > 0.05]   # ignore zeros / invalid
                # ITEM 7(a): STRICTER 'depth' gate. Keep the existing >=3-pixel / >0.05m floor,
                # then ALSO require a min valid-pixel FRACTION of the ROI and a dispersion ceiling
                # (median absolute deviation) before authorising forward drive. A sparse (95%-hole
                # patch reading off 3 edge pixels) or bimodal (foreground+background) ROI falls
                # through to the bboxH pinhole (turn-only). This ONLY makes 'depth' harder to earn,
                # never easier -- SAFE, and composes with every rsrc=='depth' consumer (the control
                # law's forbid_forward keystone, the close-range floor, the depth-fps floor, and
                # the F1 relock gate). Both thresholds default >0 (SAFE-ON); 0 restores today's gate.
                if roi.size >= 3:
                    min_frac = float(getattr(self.a, "depth_min_valid_frac", 0.0))
                    max_disp = float(getattr(self.a, "depth_max_dispersion_m", 0.0))
                    n_total = max(int(roi_all.size), 1)
                    frac_ok = (min_frac <= 0.0) or ((roi.size / n_total) >= min_frac)
                    med = float(np.median(roi))
                    if max_disp > 0.0:
                        mad = float(np.median(np.abs(roi - med)))
                        disp_ok = mad <= max_disp
                    else:
                        disp_ok = True
                    # ITEM 7(b): RGB<->depth temporal skew. Reject a depth frame more than
                    # --depth-max-skew-ms OLDER than the RGB frame being projected -> bboxH.
                    # (depth newer than RGB is fine.) Off (0) or missing stamps -> pass.
                    skew_ok = True
                    max_skew = float(getattr(self.a, "depth_max_skew_ms", 0.0))
                    if max_skew > 0.0:
                        try:
                            ds = self.node.depth_stamp(); rs = self.node.rgb_stamp()
                            if ds > 0.0 and rs > 0.0 and (rs - ds) * 1000.0 > max_skew:
                                skew_ok = False
                        except Exception:  # noqa: BLE001 -- stamp fault -> do not block on skew alone
                            skew_ok = True
                    if frac_ok and disp_ok and skew_ok:
                        return med, "depth"
                    # else: fall through to the bboxH pinhole (turn-only) below.
            except Exception:  # noqa: BLE001
                pass
        # Fallback: pinhole on bbox height.
        rng = range_from_bbox_height(person["h"], h_img, self.a.hfov_deg,
                                     self.a.person_h_m)
        if rng is not None and rng > 0:
            return rng, "bboxH"
        return None, "none"

    # -- cleanup ------------------------------------------------------------
    def _cleanup(self):
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
        if self.bridge is not None:
            log("CLEANUP stop+quit bridge")
            self.bridge.shutdown()


# ---------------------------------------------------------------------------
# Config layer (P1.1): YAML defaults + optional --profile overlay, then CLI wins.
# The node FAIL-CLOSES (refuses to start) on a missing/invalid config -- a config
# error must never silently degrade to a weaker set of safety knobs. The 7 floors
# stay null in defaults.yaml so the appearance-resolution below still runs.
# ---------------------------------------------------------------------------
_FLOOR_KEYS = ("hiconf", "anchor_floor", "bank_floor", "reloc_floor",
               "reloc_view_floor", "reloc_iso_bypass_anchor", "reloc_anchor_backstop")


def _config_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _load_yaml(path):
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        raise SystemExit("CONFIG-ERROR: cannot read %s (%s)" % (path, e))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit("CONFIG-ERROR: %s must be a YAML mapping (got %s)"
                         % (path, type(data).__name__))
    return data


def _merged_config(parser, profile):
    """defaults.yaml <- optional profile overlay. Validates the key set (defaults must
    EXACTLY match the parser dests; a profile must be a subset), enforces choices parity
    with argparse, keeps the appearance floors null, and coerces overlay values to the
    argument type. Fail-closed on any mismatch."""
    dests = {a.dest: a for a in parser._actions if a.dest not in ("help", "profile")}
    valid = set(dests)

    defaults_path = os.path.join(_config_dir(), "defaults.yaml")
    if not os.path.isfile(defaults_path):
        raise SystemExit("CONFIG-ERROR: missing %s -- cannot start (fail-closed). "
                         "Deploy config/ next to the node." % defaults_path)
    base = _load_yaml(defaults_path)
    missing, unknown = valid - set(base), set(base) - valid
    if missing or unknown:
        raise SystemExit("CONFIG-ERROR: %s key set mismatch -- missing=%s unknown=%s"
                         % (defaults_path, sorted(missing), sorted(unknown)))
    for k in _FLOOR_KEYS:
        if base.get(k) is not None:
            raise SystemExit("CONFIG-ERROR: %s must leave '%s' null (appearance-resolved); "
                             "got %r" % (defaults_path, k, base[k]))

    if profile:
        prof_path = profile if (os.sep in profile or profile.endswith((".yaml", ".yml"))) \
            else os.path.join(_config_dir(), profile + ".yaml")
        if not os.path.isfile(prof_path):
            raise SystemExit("CONFIG-ERROR: --profile %s: no such config file %s"
                             % (profile, prof_path))
        overlay = _load_yaml(prof_path)
        bad = set(overlay) - valid
        if bad:
            raise SystemExit("CONFIG-ERROR: profile %s has unknown key(s) %s"
                             % (prof_path, sorted(bad)))
        for k in _FLOOR_KEYS:                           # floors stay appearance-resolved in EVERY layer
            if overlay.get(k) is not None:
                raise SystemExit("CONFIG-ERROR: profile %s must leave '%s' null (appearance-resolved); "
                                 "got %r" % (prof_path, k, overlay[k]))
        base.update(overlay)

    # Coerce every value to the arg type the SAME way argparse coerces a CLI token -- via the
    # STRING (act.type(str(v))) -- so defaults.yaml and profiles are symmetric and type-robust:
    # an int-written float becomes float (byte-identical), and a non-integer float for an int key
    # RAISES like argparse rather than silently truncating. Bool flags must be real YAML bools.
    bool_dests = {d for d, a in dests.items()
                  if isinstance(a, argparse.BooleanOptionalAction) or getattr(a, "nargs", None) == 0}
    for k, act in dests.items():
        v = base.get(k)
        if v is None:
            continue
        if k in bool_dests:
            if not isinstance(v, bool):
                raise SystemExit("CONFIG-ERROR: '%s' must be a YAML bool (true/false), got %r" % (k, v))
        elif getattr(act, "type", None) is not None:
            try:
                base[k] = act.type(str(v))
            except Exception as e:  # noqa: BLE001
                raise SystemExit("CONFIG-ERROR: '%s'=%r not %s (%s)" % (k, v, act.type.__name__, e))
        else:
            base[k] = str(v)                           # plain string args -> match a CLI string token

    for k, act in dests.items():                       # choices parity (YAML bypasses argparse)
        ch = getattr(act, "choices", None)
        if ch is not None and base.get(k) not in ch:
            raise SystemExit("CONFIG-ERROR: '%s'=%r not in choices %s"
                             % (k, base.get(k), list(ch)))
    return base


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="K1 lock-and-handoff person-follow (preview by default; --drive to walk).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true",
                      help="detect + print only, never drive (DEFAULT)")
    mode.add_argument("--drive", action="store_true",
                      help="actually walk: spawn bridge and stream velocities")

    p.add_argument("--topic", default=DEF_TOPIC)
    p.add_argument("--depth-topic", default="/boostercamera/head/depth",
                   help="depth image topic ('none' to disable)")
    p.add_argument("--bridge", default="./loco_follow_bridge",
                   help="path to compiled loco_follow_bridge (drive mode)")
    p.add_argument("--yolo-path", default=DEF_YOLO_PATH,
                   help="YOLO11n ONNX person-detection model")

    # control law / geometry
    p.add_argument("--standoff-m", type=float, default=DEF_STANDOFF_M)
    p.add_argument("--deadband-m", type=float, default=DEF_DEADBAND_M)
    p.add_argument("--hfov-deg", type=float, default=DEF_HFOV_DEG)
    p.add_argument("--person-h-m", type=float, default=DEF_PERSON_H_M,
                   help="assumed standing height for bbox-height range fallback")

    p.add_argument("--k-yaw", type=float, default=DEF_K_YAW)
    p.add_argument("--k-vx", type=float, default=DEF_K_VX)

    # HARD clamps
    p.add_argument("--vx-min", type=float, default=DEF_VX_MIN)
    p.add_argument("--vx-max", type=float, default=DEF_VX_MAX)
    p.add_argument("--vyaw-min", type=float, default=DEF_VYAW_MIN)
    p.add_argument("--vyaw-max", type=float, default=DEF_VYAW_MAX)
    # accel (slew) limiter -- per-tick cap, applied BEFORE the HARD clamps (never widens)
    p.add_argument("--vx-slew", type=float, default=DEF_VX_SLEW,
                   help="max change in forward vx per control tick (m/s/tick) -- accel limiter so "
                        "the command cannot step 0->max in one frame (humanoid balance, esp. the "
                        "first frames after a relock/resume). Applied before the HARD vx clamp; <=0 disables")
    p.add_argument("--vyaw-slew", type=float, default=DEF_VYAW_SLEW,
                   help="max change in yaw rate per control tick (rad/s/tick) -- accel limiter applied "
                        "before the HARD vyaw clamp; <=0 disables")

    # detection / association
    p.add_argument("--conf", type=float, default=DEF_CONF,
                   help="YOLO person confidence threshold")
    p.add_argument("--seed-frames", type=int, default=DEF_SEED_FRAMES,
                   help="consecutive marker frames to seed/re-seed")
    p.add_argument("--seed-near-frac", type=float, default=DEF_SEED_NEAR_FRAC,
                   help="marker->person centroid gate at seed (frac of img diagonal)")
    p.add_argument("--w-centroid", type=float, default=DEF_W_CENTROID)
    p.add_argument("--w-color", type=float, default=DEF_W_COLOR)
    p.add_argument("--w-size", type=float, default=DEF_W_SIZE)
    p.add_argument("--gate", type=float, default=DEF_GATE,
                   help="max acceptable association cost (above -> not the target)")
    p.add_argument("--color-ema", type=float, default=DEF_COLOR_EMA,
                   help="EMA rate for slowly updating the color signature")
    p.add_argument("--hiconf", type=float, default=None,
                   help="min hist-similarity to allow appearance EMA update")
    p.add_argument("--stream", action="store_true",
                   help="emit annotated JPEG frames on stdout (Tracker page); "
                        "status text is routed to stderr")
    p.add_argument("--stream-quality", type=int, default=70)

    # --- Rerun (rerun.io) observability (Phase 2). DEFAULT-OFF + byte-identical when off. ---
    p.add_argument("--rerun", action="store_true",
                   help="record a scrubbable Rerun .rrd (follow scalars + FSM + events on the "
                        "control loop; RGB/depth on the cam-spin thread). Off by default and "
                        "byte-identical to today when off. Needs k1_rerun.py + rerun-sdk on-robot. "
                        "GATE: do NOT combine with --drive until the on-Orin loop-cost probe passes "
                        "(see RERUN_PLAN.md loop-safety gate).")
    p.add_argument("--rerun-mode", choices=["save", "connect"], default="save",
                   help="save = timestamped .rrd on --rerun-dir (crash-safe); "
                        "connect = stream to a laptop viewer at --rerun-addr (supervised DRIVE only)")
    p.add_argument("--rerun-addr", default=None,
                   help="--rerun-mode connect: gRPC address of the laptop viewer, e.g. 1.2.3.4:9876")
    p.add_argument("--rerun-dir", default="/home/booster/rerun",
                   help="--rerun-mode save: directory for the timestamped .rrd")
    p.add_argument("--rerun-image-every-n", type=int, default=3,
                   help="log 1 in N camera frames to keep the recording (and loop) light (default 3)")
    p.add_argument("--rerun-overrun-frames", type=int, default=8,
                   help="auto-disable Rerun after this many CONSECUTIVE over-budget control-loop "
                        "frames while --rerun is on (loop-safety backstop; default 8)")
    p.add_argument("--rerun-warmup-s", type=float, default=15.0,
                   help="grace window (s from node start) during which over-budget frames do NOT count "
                        "toward the RERUN-DISABLED-SLOW shed -- covers model/TensorRT warmup (robot not "
                        "walking yet) so the backstop can't kill the recording during startup (default 15)")

    # --- OBSTACLE-BRAKE reflex (Phase 3 obstacle-avoidance). DEFAULT-OFF + byte-identical when off. ---
    p.add_argument("--obstacle-brake", action="store_true",
                   help="grade forward vx down as an obstacle enters the forward DEPTH corridor "
                        "(geometry-triggered forward-clearance reflex; yaw untouched; only ever "
                        "REDUCES vx). Off by default. Composes with the forbid_forward keystone.")
    p.add_argument("--obstacle-brake-start", type=float, default=1.5,
                   help="corridor clearance (m) at/below which vx starts grading down")
    p.add_argument("--obstacle-brake-stop", type=float, default=0.7,
                   help="corridor clearance (m) at/below which forward vx is capped to 0 (turn/back only)")
    p.add_argument("--obstacle-corridor-frac", type=float, default=0.35,
                   help="central fraction of image WIDTH treated as the forward corridor")
    p.add_argument("--obstacle-band-top", type=float, default=0.30,
                   help="top of the vertical band scanned (frac of height; skips the ceiling)")
    p.add_argument("--obstacle-band-bot", type=float, default=0.68,
                   help="bottom of the vertical band scanned (frac of height; skips the near floor)")
    p.add_argument("--obstacle-max-m", type=float, default=5.0,
                   help="ignore corridor depth beyond this (m)")
    p.add_argument("--obstacle-pctile", type=float, default=8.0,
                   help="robust-near percentile of corridor depth (not raw min -> glitch-resistant)")
    p.add_argument("--obstacle-min-valid", type=int, default=40,
                   help="min valid corridor depth pixels to trust a clearance reading")
    p.add_argument("--obstacle-aged", type=int, default=3,
                   help="aged-median window (frames) over the clearance -> a single glitch can't brake")
    p.add_argument("--obstacle-target-margin", type=float, default=0.4,
                   help="don't brake unless the corridor obstacle is at least this much CLOSER than "
                        "the followed target (m) -- so the reflex ignores the operator it's following")
    p.add_argument("--assoc-margin", type=float, default=0.15,
                   help="min cost gap between best and runner-up person; below "
                        "this the frame is treated as no-match (prefer stop)")
    p.add_argument("--ema-max-jump", type=float, default=0.12,
                   help="max normalized centroid jump (frac of diag) to still "
                        "allow a color-EMA appearance update")

    # Stage 1: pixel-space motion tracking + occlusion coasting
    p.add_argument("--track", action=argparse.BooleanOptionalAction, default=True,
                   help="Stage 1 pixel-space Kalman tracker -> stable per-person "
                        "track ids + motion prediction. Lets the lock prefer the SAME "
                        "person across frames and coast through brief occlusions. "
                        "--no-track reproduces the pre-Stage-1 stateless behavior.")
    p.add_argument("--coast-frames", type=int, default=0,
                   help="frames to COAST on the bound track's motion prediction when "
                        "the locked person is briefly occluded (0 = disabled = today's "
                        "immediate loss handling; try 6-8 ~ 0.6-0.8s at 10Hz)")
    p.add_argument("--coast-vx-scale", type=float, default=0.0,
                   help="forward-speed scale while COASTING. DEFAULT 0.0 = yaw-only "
                        "coast (never translate toward an unseen target). >0 also "
                        "REQUIRES a validated depth range, never a bbox-height guess.")
    p.add_argument("--iou-min", type=float, default=0.2,
                   help="min IoU to associate a detection to an existing track")
    p.add_argument("--max-coast", type=int, default=10,
                   help="frames a track survives with no detection before it is "
                        "retired (should be >= --coast-frames)")
    p.add_argument("--idsw-debounce-frames", type=int, default=3,
                   help="Phase 2.4 ID-stability debounce: when the accepted target carries a DIFFERENT "
                        "ByteTrack id than the held lock, hold on the last-good centroid until the new id "
                        "persists this many frames before committing the switch (anti wrong-person-lock on "
                        "a crossing). 1 = switch on frame 1 (the pre-2.4 behavior).")

    # Stage 2: anchored appearance gallery + distractor bank (CPU; no new model).
    p.add_argument("--gallery-size", type=int, default=8,
                   help="max same-person appearance views kept (slot-0 = frozen anchor, "
                        "pinned and never evicted). 1 disables the gallery (== Stage-1).")
    p.add_argument("--distractor-size", type=int, default=32,
                   help="max confidently-other-person views banked as negatives")
    p.add_argument("--bank-floor", type=float, default=None,
                   help="min similarity to the FROZEN anchor for a view to be ADMITTED "
                        "to the gallery (keep >= --anchor-floor)")
    p.add_argument("--admit-conf", type=float, default=0.6,
                   help="min track confidence to admit a gallery view")
    p.add_argument("--admit-spacing", type=int, default=5,
                   help="min processed-frames between gallery admissions")
    p.add_argument("--bbox-iou-isolate", type=float, default=0.1,
                   help="a candidate is 'isolated' (eligible to admit/bank) iff its max "
                        "IoU with any other person is below this -- guards crossings")
    p.add_argument("--reloc-iso-bypass-anchor", type=float, default=None,
                   help="markerless re-acquire ONLY: a candidate whose FROZEN-anchor match "
                        "a_s is at/above this may re-lock even when it OVERLAPS another "
                        "person (the --bbox-iou-isolate veto is skipped), because at this "
                        "anchor strength its identity is not in doubt. SCOPED to the re-lock "
                        "vote -- does NOT touch the gallery admit/bank isolation. Must be "
                        "> --anchor-floor (else ignored by the floor ladder). Default "
                        "per-appearance (global 0.65, striped 0.55, osnet 0.55).")
    p.add_argument("--w-gallery", type=float, default=0.3,
                   help="cost discount for matching a gallery view (bounded; can never "
                        "overpower the slot-0 anchor veto)")
    p.add_argument("--w-distractor", type=float, default=0.5,
                   help="cost penalty (clamped) for resembling a banked distractor")

    # sticky target-lock (feature 1) + fail-safe stand (feature 2)
    p.add_argument("--anchor-floor", type=float, default=None,
                   help="min color-similarity to the FROZEN lock anchor for a TRACK "
                        "match to be accepted; below this the match is vetoed and the "
                        "robot treats it as a loss -- it NEVER switches to another "
                        "person/object. 0 disables the sticky-lock veto.")
    p.add_argument("--reseed-anchor-floor", type=float, default=0.5,
                   help="min color-similarity to the original anchor for a marker "
                        "RE-SEED to re-lock (strict same-person recovery). 0 disables.")
    p.add_argument("--stand-on-loss", action=argparse.BooleanOptionalAction, default=True,
                   help="on hard target loss / camera stall / frame error, command "
                        "kPrepare (stable stand) instead of only holding the walking gait")

    # Stage 3: autonomous markerless re-acquire (PASSIVE; default OFF; audit-only on HS)
    p.add_argument("--auto-reacquire", action=argparse.BooleanOptionalAction, default=False,
                   help="in REACQUIRE, while STOOD, run a strict appearance vote to re-find "
                        "the SAME person WITHOUT the marker. DEFAULT OFF. On HS features the "
                        "re-lock is audit-only (votes+logs, never re-locks) -- trustworthy only "
                        "on Stage-4 embeddings. The marker always remains the fallback.")
    p.add_argument("--reloc-floor", type=float, default=None,
                   help="min gallery similarity (g_sim) for a re-acquire candidate (> anchor-floor)")
    p.add_argument("--reloc-margin", type=float, default=0.20,
                   help="min (g_sim - distractor_sim) for a re-acquire candidate")
    p.add_argument("--reloc-sep", type=float, default=0.15,
                   help="min separation of the best candidate over the runner-up survivor")
    p.add_argument("--reloc-streak", type=int, default=5,
                   help="consecutive frames the SAME track must win the vote before re-lock")
    p.add_argument("--reloc-seconds", type=float, default=6.0,
                   help="seconds after a hard loss during which auto-reacquire may run")

    # Stage 4 (MODEL-FREE): part-based appearance descriptor + opt-in armed re-acquire
    p.add_argument("--appearance", choices=["global", "striped", "osnet"], default="global",
                   help="appearance feature. 'global' = single HS histogram (DEFAULT; Stages "
                        "1-3 behavior verbatim). 'striped' = model-free part-based vertical-band "
                        "HS descriptor (separates COLOR-LAYOUT look-alikes). 'osnet' = deep "
                        "person-ReID embedding (Stage-4, via --reid-engine) -- strongest; makes A "
                        "robust and armed E trustworthy in crowds. NOTE: striped/osnet use cosine "
                        "(vs correlation) -- re-tune --anchor-floor/--bank-floor/--reloc-floor/"
                        "--hiconf from the audit logs. osnet falls back to 'global' if the engine "
                        "won't load.")
    p.add_argument("--reid-engine", type=str, default="",
                   help="path to the person-ReID ONNX (e.g. osnet_x0_25_msmt17.onnx) for "
                        "--appearance osnet. Served via onnxruntime's TensorRT EP at FP16 "
                        "(--reid-fp16); the engine is built+cached on the device on first run.")
    p.add_argument("--reid-input", type=str, default="256x128",
                   help="ReID model input HxW (default 256x128, the OSNet convention)")
    p.add_argument("--reid-fp16", action=argparse.BooleanOptionalAction, default=True,
                   help="enable TensorRT FP16 for the ReID engine (default on)")
    p.add_argument("--reid-letterbox", action=argparse.BooleanOptionalAction, default=True,
                   help="aspect-preserving letterbox crop for the ReID model (default on; off = the "
                        "old stretch-to-WxH). Re-tune the cosine floors if you toggle this.")
    p.add_argument("--reid-batch", action=argparse.BooleanOptionalAction, default=True,
                   help="run all person crops in ONE ReID inference per frame (default on; only "
                        "batches if the ONNX has a dynamic batch axis, else falls back to per-row)")
    p.add_argument("--reid-every-n", type=int, default=1,
                   help="recompute the ReID embedding every N frames per track (1 = every frame; "
                        ">1 caches between, but always re-embeds the target + overlapping people)")
    p.add_argument("--reid-fault-k", type=int, default=5,
                   help="OSNet health watchdog: after this many CONSECUTIVE frames where the batched "
                        "embed returns all-None over a NON-EMPTY person set, latch REID-DEGRADED and "
                        "force any ARMED markerless re-lock to audit-only until a valid embedding "
                        "returns. 0 disables the mid-run watchdog (today's behavior).")
    p.add_argument("--audit-cosine", action=argparse.BooleanOptionalAction, default=False,
                   help="log a per-frame AUDIT line: cosine of the bound target vs the best other "
                        "person to the frozen anchor -- to tune --anchor-floor/--bank-floor for cosine")
    p.add_argument("--arm-reacquire", action=argparse.BooleanOptionalAction, default=False,
                   help="DANGER (default OFF): let --auto-reacquire actually RE-LOCK and drive "
                        "toward the voted person, instead of audit-only. REQUIRES --appearance "
                        "osnet (refused on global/striped); only AFTER validating audit-only that "
                        "the vote picks YOU and never a bystander, and NEVER in identical-uniform "
                        "crowds. No effect without --drive --auto-reacquire.")
    p.add_argument("--reloc-gallery-gate", action=argparse.BooleanOptionalAction, default=False,
                   help="markerless re-acquire: also let a TURNED/RELIT returning target re-lock via "
                        ">=N strictly-admitted, mutually-distinct gallery views (multi-shot re-ID), in "
                        "ADDITION to the frozen anchor. Default OFF = anchor-only (byte-identical). "
                        "Strictly stricter than the TRACK veto; tune with --audit-cosine before arming.")
    p.add_argument("--reloc-min-views", type=int, default=3,
                   help="OR-branch: min mutually-distinct STRONG gallery views to vouch a re-lock")
    p.add_argument("--reloc-view-floor", type=float, default=None,
                   help="OR-branch: cosine a gallery view must clear (to BOTH the anchor and the "
                        "candidate) to count (default per-appearance, >= bank_floor; global disables it)")
    p.add_argument("--reloc-anchor-backstop", type=float, default=None,
                   help="OR-branch: relaxed-but-NONZERO frozen-anchor floor still required on the "
                        "multi-view branch (default 0.8*anchor_floor; a total anchor mismatch is vetoed)")
    p.add_argument("--reloc-dedup-ceiling", type=float, default=0.85,
                   help="OR-branch: two gallery views with cosine >= this count as ONE viewpoint")
    p.add_argument("--reloc-arm-streak", type=int, default=8,
                   help="ARMED re-lock needs this many consecutive same-id passes (>= --reloc-streak)")
    p.add_argument("--reloc-arm-margin", type=float, default=0.30,
                   help="ARMED re-lock needs (g_sim - distractor) >= this, wider than --reloc-margin")
    p.add_argument("--reloc-max-jump-frac", type=float, default=0.5,
                   help="anti-teleport: an OR-branch re-lock must be within this fraction of the frame "
                        "diagonal of the last-seen centroid")
    p.add_argument("--reloc-range-streak", type=int, default=2,
                   help="ARMED relock: N consecutive in-range depth same-id frames required before "
                        "committing a re-lock (rejects a one-frame depth glitch)")
    p.add_argument("--reloc-max-range-jump-m", type=float, default=2.5,
                   help="ARMED relock: reject a depth-range jump larger than this (m) vs the MEDIAN "
                        "of recent tracked depth ranges, while that reference is still fresh "
                        "(--reloc-range-stale-s); 0 disables the jump check")
    p.add_argument("--reloc-range-stale-s", type=float, default=10.0,
                   help="ARMED relock: the tracked-range reference for the jump check is trusted "
                        "only this many seconds after the last validated TRACK range. Older = the "
                        "jump check is skipped (a target who walked far during a long loss can "
                        "still re-lock); the absolute cap + streak still reject a glitch.")
    p.add_argument("--reloc-max-range-m", type=float, default=5.0,
                   help="ARMED relock: absolute ceiling (m) on a relock candidate's depth range -- "
                        "re-ID at long range is unreliable and a follow target does not teleport "
                        "metres away, so a candidate beyond this is rejected regardless of the "
                        "reference (catches a glitched-high relock). 0 disables.")
    p.add_argument("--reloc-require-depth", action=argparse.BooleanOptionalAction, default=True,
                   help="ARMED relock: require a VALIDATED depth range (rsrc=='depth') at the "
                        "candidate before commit, else HOLD (no relock this frame)")
    p.add_argument("--reloc-scale-tol", type=float, default=1.8,
                   help="anti-teleport: an OR-branch re-lock apparent size within this ratio of the "
                        "last-seen size")
    p.add_argument("--require-heartbeat", action=argparse.BooleanOptionalAction, default=False,
                   help="UNTETHERED DEADMAN (default OFF): require a fresh operator-heartbeat file to "
                        "drive. No heartbeat -> velocity gated to zero (this node) AND the bridge "
                        "watchdog stands the robot (kPrepare). Default off = tethered byte-identical.")
    p.add_argument("--hb-file", type=str, default="/tmp/k1_hb",
                   help="operator-heartbeat file; the operator's relay touches its mtime at ~10Hz "
                        "(both this node and the bridge read it). Missing/stale mtime = absent = STOP.")
    p.add_argument("--hb-stale-ms", type=int, default=400,
                   help="heartbeat older than this (ms) = operator absent -> stop (this node's gate; "
                        "the bridge applies its own HB_STALE_MS/HB_PREP_MS tiers independently)")
    p.add_argument("--allow-untethered-unsafe", action=argparse.BooleanOptionalAction, default=False,
                   help="DANGEROUS override: permit --drive WITHOUT --require-heartbeat (no operator "
                        "deadman). Default off -> such a config is REFUSED at startup. Only for a "
                        "tethered bench/demo with a human on the gamepad e-stop.")

    # low-light enhancement
    p.add_argument("--low-light", action=argparse.BooleanOptionalAction, default=True,
                   help="adaptive low-light boost (CLAHE+gamma on luma) before detection; "
                        "auto-engages only when the frame is dark")
    p.add_argument("--ll-dark-thresh", type=float, default=LL_DARK_THRESH,
                   help="mean luma (0-255) below which the low-light boost engages")

    # watchdogs / loop
    p.add_argument("--lost-grace", type=int, default=DEF_LOST_GRACE)
    p.add_argument("--rate-hz", type=float, default=DEF_RATE_HZ)
    p.add_argument("--stall-seconds", type=float, default=DEF_STALL_SECONDS)
    p.add_argument("--max-seconds", type=float, default=DEF_MAX_SECONDS)
    p.add_argument("--reacquire-timeout", type=float, default=DEF_REACQUIRE_TIMEOUT,
                   help="seconds in REACQUIRE with no marker before parking (-> S_PARKED)")
    p.add_argument("--park-timeout", type=float, default=0.0,
                   help="seconds in PARKED with no marker before a CLEAN exit (stop+kPrepare "
                        "via bridge EOF). 0 (default) = stay parked until --max-seconds.")
    # SEARCH-on-loss: yaw-only rotate to re-find a lost target before standing (DRIVE only)
    p.add_argument("--search-on-loss", action=argparse.BooleanOptionalAction, default=True,
                   help="on hard loss (DRIVE), rotate yaw-only toward the last-known bearing to "
                        "re-find the person before standing; falls back to REACQUIRE after --search-timeout")
    p.add_argument("--search-timeout", type=float, default=8.0,
                   help="max seconds to yaw-scan for the lost person before standing (-> REACQUIRE)")
    p.add_argument("--search-yaw", type=float, default=0.30,
                   help="yaw rate (rad/s) for the SEARCH scan (clamped to the vyaw limits)")
    p.add_argument("--search-dwell", action=argparse.BooleanOptionalAction, default=True,
                   help="SEARCH: when a person is in frame, HOLD the yaw scan (dwell) so the "
                        "re-lock vote gets a stable multi-frame look instead of sweeping past "
                        "them (field fix: full-yaw rotation blurred embeddings + churned track "
                        "ids -> vote rejected at g=0.51-0.54 vs the 0.55 floor). Bounded by "
                        "--search-timeout. --no-search-dwell restores the continuous sweep.")
    # OPS follow-range geofence (depth-validated; 0 disables -> today's behavior)
    p.add_argument("--max-follow-range", type=float, default=0.0,
                   help="if >0, STAND (don't pursue) when the DEPTH-validated range to the "
                        "target exceeds this (m) -- guards a depth glitch / sprinting target "
                        "from driving at max vx indefinitely. Set comfortably beyond --standoff-m.")
    p.add_argument("--min-safe-range", type=float, default=DEF_MIN_SAFE_RANGE,
                   help="if >0, STAND (via the depth-validated hysteresis geofence) AND forbid "
                        "forward vx when the DEPTH-validated range is at/closer than this (m). "
                        "Always-on by default so even a correct close reading backs off; never "
                        "trips on a bbox-height guess (rsrc!='depth'). 0 disables (today's behavior)")
    p.add_argument("--range-hysteresis", type=float, default=0.3,
                   help="follow-range geofence release margin (m): once range-gated, resume "
                        "only when the range returns inside the fence by this much (anti-chatter)")
    p.add_argument("--min-depth-fps", type=float, default=5.0,
                   help="depth-fps floor: when the sustained depth publish rate falls below this "
                        "(fps), force TURN-ONLY (suppress forward vx, vx<=0) until it recovers above "
                        "the floor + --depth-starved-margin. Composes with the rsrc=='depth' keystone; "
                        "adds no new stand. 0 disables (today's behavior byte-for-byte).")
    p.add_argument("--depth-starved-margin", type=float, default=1.0,
                   help="hysteresis (fps) for --min-depth-fps: once depth-starved, clear only when "
                        "depth fps rises above (--min-depth-fps + this). 0 = clear exactly at the "
                        "floor. Ignored when --min-depth-fps is 0.")
    p.add_argument("--startup-frame-wait", type=float, default=25.0,
                   help="drive: max seconds to wait for first frame before walk")
    # P1 ITEM 7: stricter depth validation + RGB<->depth skew + DRIVE precondition. All SAFE-ON;
    # each has a 0/off value byte-identical to today. These only ever make forward drive HARDER.
    p.add_argument("--depth-min-valid-frac", type=float, default=0.30,
                   help="P1: min FRACTION of the ~9x9 centroid depth ROI that must be finite/>0.05m "
                        "before a reading is labelled 'depth' (which alone authorises forward drive); "
                        "a sparse patch (e.g. 3 edge pixels of a 95%-hole ROI) falls back to the "
                        "bbox-height pinhole (turn-only). Keeps the >=3-pixel floor. 0 disables (today).")
    p.add_argument("--depth-max-dispersion-m", type=float, default=0.5,
                   help="P1: reject a depth reading whose valid ROI samples disperse more than this "
                        "(median absolute deviation, metres) -> bbox-height pinhole (turn-only). Guards "
                        "a bimodal foreground/background patch. 0 disables (today).")
    p.add_argument("--depth-max-skew-ms", type=float, default=250.0,
                   help="P1: reject a depth frame more than this many ms OLDER than the RGB frame whose "
                        "centroid is projected -> treat as bbox-height (turn-only), so stale depth never "
                        "authorises forward drive against a moved target. Default 250 stays coherent with "
                        "--min-depth-fps 5 (healthy-at-the-floor depth ages up to 200ms between frames -- "
                        "a tighter skew would flicker forward vx on a healthy stream). 0 disables (today).")
    p.add_argument("--drive-min-fps", type=float, default=8.0,
                   help="P1 DRIVE precondition: require BOTH RGB and depth publishing at >= this fps, "
                        "sustained for --drive-ready-secs, before transitioning to kWalking. Bounded by "
                        "--startup-frame-wait; on timeout the node REFUSES drive (DRIVE-ABORT), never "
                        "walks blind. 0 disables the precondition (today's single-frame gate, byte-for-byte).")
    p.add_argument("--drive-ready-secs", type=float, default=2.0,
                   help="P1: how long BOTH RGB and depth must sustain >= --drive-min-fps before "
                        "DRIVE-READY -> walk. Ignored when --drive-min-fps is 0.")

    # --- lock trigger (acquisition primitive) -- SCOPE_lock_trigger_gesture.md ---
    p.add_argument("--lock-trigger", choices=["aruco", "gesture", "both"], default="aruco",
                   help="acquisition trigger that seeds identity. 'aruco' (DEFAULT) = today's "
                        "marker, byte-identical. 'gesture' = raised-hand via a YOLO11n-pose model. "
                        "'both' = ArUco drives + gesture AUDIT-ONLY (the A/B harness). The gesture "
                        "model is loaded ONLY when != aruco.")
    p.add_argument("--gesture-model", type=str, default="yolo11n-pose.onnx",
                   help="YOLO11n-POSE model path (loaded only when --lock-trigger != aruco). .onnx runs "
                        "via onnxruntime (CUDA EP, fast, same backend as OSNet); a .pt is slower (torch); "
                        "a TRT .engine built on the Orin is fastest.")
    p.add_argument("--gesture-hold", type=int, default=6,
                   help="qualifying pose frames a raised hand must hold (keyed on tracker id, "
                        "with --gesture-miss-tol forgiveness) before it can seed; composes with "
                        "--seed-frames. 6 @ every-n 3 ~= 1.8s of deliberate hand-up. Used only when "
                        "--gesture-hold-s 0 (else the wall-clock gate governs, with the floor below).")
    p.add_argument("--gesture-hold-s", type=float, default=0.9,
                   help="Phase 2.1: continuous wall-clock SECONDS a hand must be up to seed -- makes "
                        "time-to-lock loop-rate-independent (vs a raw frame count that stretches under a "
                        "slow loop). 0 = disable and fall back to the pure --gesture-hold frame count.")
    p.add_argument("--gesture-hold-floor", type=int, default=3,
                   help="Phase 2.1: min qualifying pose frames required ALONGSIDE --gesture-hold-s, so a "
                        "single fluke frame that spans the dwell window cannot seed (anti-fluke floor).")
    p.add_argument("--gesture-kp-conf", type=float, default=0.35,
                   help="min per-keypoint confidence for the wrist/shoulder raised-hand test "
                        "(0.35: nano-pose wrist confidence at 3-4m commonly sits 0.3-0.5, so the "
                        "old 0.5 randomly dropped real raises; the geometry margin still gates)")
    p.add_argument("--gesture-kp-margin-frac", type=float, default=0.05,
                   help="wrist must be above the same-side shoulder by this fraction of bbox height "
                        "(0.05 ~= half a head: a natural bent-elbow hand-up passes; the old 0.10 "
                        "needed a fully extended arm)")
    p.add_argument("--gesture-miss-tol", type=int, default=1,
                   help="hold survives this many CONSECUTIVE missed pose frames before resetting "
                        "(a miss never increments the hold). 0 = the old hard-reset-on-any-miss.")
    p.add_argument("--gesture-owner-margin", type=float, default=0.15,
                   help="OWNER-AUTHORITY: min IoU gap the best pose->person match must beat the "
                        "2nd-best by, else the bind is AMBIGUOUS and refused (prevents locking an "
                        "overlapping bystander who never raised a hand). 0 = disable the ambiguity gate.")
    p.add_argument("--gesture-min-sep-frac", type=float, default=0.12,
                   help="refuse a gesture seed if another person is within this fraction of the "
                        "image diagonal of the lock point (bystander-adjacency guard, SCOPE G4)")
    p.add_argument("--gesture-overrun-frames", type=int, default=5,
                   help="consecutive over-budget frames WITH pose inference active before the "
                        "gesture trigger auto-disables (degrade before the C++ watchdog must safe)")
    p.add_argument("--gesture-every-n", type=int, default=3,
                   help="in --lock-trigger both, run pose inference every Nth frame (audit "
                        "decimation so it cannot starve the live ArUco control loop)")
    p.add_argument("--gesture-stop", action=argparse.BooleanOptionalAction, default=False,
                   help="in-follow STOP gesture: the FOLLOWED person raising BOTH hands commands "
                        "WAIT (HOLD). DE-ESCALATING only (never motion). Runs pose DECIMATED in "
                        "S_TRACK (deliberately re-enabling pose in a driving state -- justified "
                        "because the gesture only ever stops). Needs --lock-trigger gesture|both.")
    p.add_argument("--gesture-stop-every-n", type=int, default=5,
                   help="run the in-follow STOP-gesture pose check every Nth frame (decimation so "
                        "it can't starve the control loop; the auto-disable + C++ watchdog still bound it)")
    p.add_argument("--gesture-stop-hold", type=int, default=4,
                   help="consecutive STOP-gesture checks the followed person must hold both hands "
                        "up before WAIT is commanded")
    p.add_argument("--gesture-debug", action="store_true",
                   help="per-frame gesture acquisition-chain tracing (GDBG lines, throttled ~1/s per "
                        "tag): #persons, #pose-dets, raised?, IoU match, hold counts, state skips. "
                        "Cheap; OFF by default. Turn on to see exactly why a raised hand isn't locking.")

    # --- Tier-1 command channel -- SCOPE_nl_command_layer.md ---
    p.add_argument("--commands", action=argparse.BooleanOptionalAction, default=False,
                   help="enable the watched-file command channel (STATUS/HOLD/PARK/STOP this "
                        "slice). DEFAULT OFF = byte-identical (no drain, no commanded_hold).")
    p.add_argument("--cmd-file", type=str, default="/tmp/k1_cmd",
                   help="path the host writes one command token to (drained once per tick)")
    p.add_argument("--profile",
                   help="config profile name (config/<name>.yaml) or a path, overlaid on "
                        "defaults.yaml; CLI flags still override. Omit = defaults only.")

    # --- P1.1 config layer: defaults.yaml <- --profile <- CLI (CLI wins). Parse CLI with
    # SUPPRESS defaults so ONLY explicitly-passed flags land in the namespace -- that lets a
    # CLI value equal to the node default still beat a profile-set value. -------------------
    for _a in p._actions:
        if _a.dest != "help":
            _a.default = argparse.SUPPRESS
    cli = vars(p.parse_args(argv))
    cfg = _merged_config(p, cli.pop("profile", None))
    cfg.update(cli)                                    # CLI overrides defaults/profile
    args = argparse.Namespace(**cfg)
    if not args.drive:
        args.preview = True
    # Appearance-aware similarity floors. striped/osnet use COSINE, not the histogram
    # CORRELATION the original floors were tuned for -- a 0.5 anchor floor in cosine space
    # vetoes the TRUE target every frame (-> empty candidate set -> instant LOST -> stand,
    # i.e. "striped doesn't even work"). Fill any floor the user left UNSET (None) with
    # cosine-appropriate defaults; an explicit --flag still wins. 'global' keeps the
    # original correlation values byte-for-byte. These are STARTING values biased toward
    # keeping the lock -- tighten them from the GALLERY-*/RELOC-* audit logs once it follows.
    _floor_defaults = {
        "global":  {"anchor_floor": 0.50, "bank_floor": 0.55, "reloc_floor": 0.70, "hiconf": DEF_HICONF},
        "striped": {"anchor_floor": 0.30, "bank_floor": 0.30, "reloc_floor": 0.45, "hiconf": 0.30},
        "osnet":   {"anchor_floor": 0.35, "bank_floor": 0.40, "reloc_floor": 0.55, "hiconf": 0.40},
    }
    _fd = _floor_defaults.get(args.appearance, _floor_defaults["global"])
    for _k, _v in _fd.items():
        if getattr(args, _k) is None:
            setattr(args, _k, _v)
    # Re-lock OR-branch floors (resolved AFTER the base floors above). 'global' -> 1.01 makes
    # the multi-shot OR-branch UNREACHABLE (HS correlation is too weak to relax the anchor).
    if args.reloc_view_floor is None:
        args.reloc_view_floor = {"osnet": 0.55, "striped": 0.55}.get(args.appearance, 1.01)
    if args.reloc_anchor_backstop is None:
        args.reloc_anchor_backstop = round(0.8 * args.anchor_floor, 4)
    # F2 re-lock iso-bypass anchor strength (resolved AFTER the base floors). A candidate may
    # skip the crowd-isolation veto only at THIS frozen-anchor strength -- chosen clearly
    # ABOVE --anchor-floor so an overlapping STRANGER (who never matches the true anchor)
    # can never bypass it. Per-appearance because cosine (striped/osnet) and correlation
    # (global) live on different scales. global 0.65 is intentionally strict (global cannot
    # arm, so this only affects audit logs); osnet/striped 0.55 is a clear same-person match.
    if args.reloc_iso_bypass_anchor is None:
        args.reloc_iso_bypass_anchor = {"striped": 0.55, "osnet": 0.55}.get(args.appearance, 0.65)

    # Lock-trigger refusals (mirror the ARM-REFUSED discipline -- fail LOUD, never silent).
    if args.lock_trigger in ("gesture", "both"):
        if not args.track:
            p.error("--lock-trigger %s requires --track: the gesture hold is keyed on the tracker "
                    "id; --no-track makes it untrustworthy" % args.lock_trigger)
        if args.reseed_anchor_floor <= 0.0:
            p.error("--lock-trigger %s requires --reseed-anchor-floor > 0: it is the sticky-lock "
                    "stranger defense the gesture seed relies on" % args.lock_trigger)
        # AMBIG owner-margin invariant (audit 2026-07-04): a LONE clear raiser has second_iou=0, so the
        # ambiguity gate (best_iou - second_iou < owner_margin -> refuse) passes only if
        # best_iou >= owner_margin. best_iou is already >= iou_min (the earlier match gate), so setting
        # owner_margin > iou_min opens a dead-zone where a solo raiser with modest overlap is refused and
        # gesture can NEVER lock. Clamp to iou_min and warn (fail loud) rather than silently trap.
        if args.gesture_owner_margin > args.iou_min:
            log("WARN --gesture-owner-margin %.2f > --iou-min %.2f can refuse a lone raiser -> "
                "clamping owner-margin to %.2f" % (args.gesture_owner_margin, args.iou_min, args.iou_min))
            args.gesture_owner_margin = args.iou_min
    # Normalize the depth-topic spelling ONCE so the subscription decision (run()) and the DRIVE
    # precondition (_need_depth) share one test: ""/whitespace/any-case-"none" == depth disabled.
    args.depth_topic = (args.depth_topic or "").strip()

    # P1.2 FAIL-CLOSED drive gate: --drive without the operator deadman is refused unless an
    # explicit unsafe override. Fires HERE (parse_args) before Follower/rclpy.init/CamNode/YOLO/
    # bridge -- so no motion path is ever spawned for an untethered-unsafe config. Inverts today's
    # dangerous default (drive silently ran with the deadman OFF). Preview is unaffected.
    if args.drive and not args.require_heartbeat and not args.allow_untethered_unsafe:
        p.error("REFUSING TO DRIVE: --drive without --require-heartbeat is untethered-unsafe "
                "(no operator deadman -- a lost operator cannot stop the robot). Add "
                "--require-heartbeat to arm the deadman, or pass --allow-untethered-unsafe to "
                "drive with NO deadman on a tethered bench (human on the gamepad e-stop).")
    return args


# init_rerun -> rerun_sink.py (P3.4).


def main():
    args = parse_args(sys.argv[1:])
    if args.stream:
        common._STREAM = True   # status -> stderr, annotated frames -> stdout (P3.0b)
    rerun_sink.init_rerun(args)     # flips rerun_sink._RR on only when --rerun; inert otherwise
    f = Follower(args)
    f.install_signal_handlers()
    f.run()


if __name__ == "__main__":
    main()
