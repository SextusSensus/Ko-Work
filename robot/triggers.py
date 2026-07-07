"""Acquisition triggers (P3.6): the ArUco marker detector + the lock-trigger classes
(LockTrigger base, ArucoTrigger [default], GestureTrigger [raised-hand YOLO-pose], CompositeTrigger
[A/B]). A trigger returns an optional LockHint -- a lock point/owner the single _try_seed funnel
consumes; it never creates identity or drives. Extracted verbatim (pure move).
"""
import numpy as np
import cv2

from common import (
    log, gdbg, iou_xyxy, LockHint,
    KP_L_SHOULDER, KP_R_SHOULDER, KP_L_WRIST, KP_R_WRIST,
    S_SEARCH, S_REACQUIRE, S_PARKED,
)

ARUCO_DICT = cv2.aruco.DICT_4X4_50

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
