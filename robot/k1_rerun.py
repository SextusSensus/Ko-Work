#!/usr/bin/env python3
"""k1_rerun.py -- crash-safe Rerun (rerun.io) sink for the K1 follow stack.

Dependency-light: **rerun + numpy ONLY** (no rclpy / cv2 / torch), so it unit-tests off-robot and
is imported by BOTH the live node (follow_person_k1.py, Phase 2) and the offline replay harness
(replay_eval.py, Phase 1).

CONTRACT (load-bearing -- Rerun must NEVER perturb the follow):
  * default construction (enabled=False) is FULLY INERT -- every method returns immediately.
  * every logging method is wrapped in try/except and no-ops on ANY exception.
  * a run of faults latches the sink OFF (so a persistent per-frame exception can't spam the log
    or steal loop time) -- this backstops the Phase-2 loop-safety gate.
  * image/depth methods COPY the caller's buffer (BGR->RGB / dtype cast) -- never alias a frame
    another thread reads under a lock.

The node (Phase 2) calls the direct methods (frame/scalar/boxes/text/state/image/depth). The replay
harness (Phase 1) additionally uses feed_log_line() to build a .rrd from the node's own telemetry
lines with ZERO node change.

Verified against rerun-sdk 0.33.1 (laptop) AND 0.23.1 (robot): Scalars(v) (plural), set_time(timeline,
sequence=/timestamp=), save(path), connect_grpc(addr), Boxes2D(mins=/sizes=/labels=), Image(rgb),
DepthImage(img, meter=), TextLog(text, level=), log(..., static=), send_blueprint. The robot is PINNED
to rerun 0.23.1 -- the newest release that allows numpy 1.x; >=0.23.2 requires numpy>=2 which would
break the numpy-1.26-ABI follow stack (onnxruntime/cv2/rclpy). close() uses a no-arg flush() that
blocks on both. The API is identical across both versions for everything this sink uses.
"""
import json
import os
import re
import time as _time

# /follow/range_source step encoding -- the bug hinge (depth vs bbox-height range).
_RSRC_CODE = {"depth": 2.0, "bboxH": 1.0}

# FSM state -> scrubbable numeric series (alongside the text log). Keys are the EXACT node S_*
# string values (follow_person_k1.py:1722-1726) -- note the search state is "SEARCH_MARKER", not
# "SEARCH" (a plain "SEARCH" here logged -1 for the most common state in every .rrd).
# SOURCE OF TRUTH for the FROZEN categorical encoding of the P7 dataset's fsm_state_id feature: keep
# this in exact sync with eval/fsm_states.json (P7.1 ingest asserts coverage against it).
_STATE_CODE = {"TRACK": 3.0, "REACQUIRE": 2.0, "SEARCHING": 1.5,
               "SEARCH_MARKER": 1.0, "SEARCH": 1.0, "PARKED": 0.0}

# TRACK telemetry line (follow_person_k1.py:3511). Example:
#   TRACK id=6 LOCK c=(256,222) range=0.84[depth] bearing=-02.3deg vx=-0.06 vyaw=+0.04 \
#         cost=-0.05/2nd=- sim=0.97 conf=0.91 [preview]
_TRACK_RE = re.compile(
    r"^TRACK id=(?P<id>\S+?)(?P<lock> LOCK)? c=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"range=(?P<rng>[0-9.]+|n/a)\[(?P<rsrc>\w+)\] "
    r"bearing=(?P<bearing>[-+][0-9.]+)deg "
    r"vx=(?P<vx>[-+][0-9.]+) vyaw=(?P<vyaw>[-+][0-9.]+) "
    r"cost=(?P<cost>[-+]?[0-9.]+)/2nd=(?P<cost2>\S+) "
    r"sim=(?P<sim>[0-9.]+) conf=(?P<conf>[0-9.]+)"
)
_RANGEGATE_RE = re.compile(r"^RANGE-GATE rng=(?P<rng>[0-9.]+)\[(?P<rsrc>\w+)\]")

# Node log prefixes worth surfacing on the /events text stream (mirror, do not invent).
_EVENT_PREFIXES = ("AUTO-RELOCK", "RANGE-GATE", "DEPTH-STARVED", "DRIVE-ABORT", "DRIVE-ACTIVE",
                   "GALLERY", "RELOC-ERR", "TRACK-ERR", "SLOW-LOOP", "REID", "BRIDGE")


def default_blueprint():
    """The 'scrub and see' layout: camera+target left; the bug-hinge scalars stacked center
    (range vs the 0.6/1.2 ref lines, vx, forbid_forward, range_source); FSM + events + reid right.
    Returns a rrb.Blueprint or None if the blueprint API is unavailable."""
    try:
        import rerun.blueprint as rrb
    except Exception:
        return None
    return rrb.Blueprint(
        rrb.Horizontal(
            rrb.Spatial2DView(origin="/camera/rgb", name="camera + target box"),
            rrb.Vertical(
                rrb.TimeSeriesView(origin="/follow/range",
                                   name="range m  (+0.6 min-safe / 1.2 standoff / max)"),
                rrb.TimeSeriesView(origin="/cmd/vx", name="vx forward  (lunge = spike)"),
                rrb.TimeSeriesView(origin="/cmd/forbid_forward",
                                   name="forbid_forward 0/1  (freeze = latched 1)"),
                rrb.TimeSeriesView(origin="/follow/range_source",
                                   name="range_source  2=depth 1=bboxH 0=none"),
            ),
            rrb.Vertical(
                rrb.TimeSeriesView(origin="/fsm/state_id", name="FSM state  3T 2R 1.5Sg 1S 0P"),
                rrb.TextLogView(origin="/events", name="events"),
                rrb.TimeSeriesView(contents=["/reid/**", "/track/**"], name="reid sim / conf / cost"),
            ),
            column_shares=[2, 3, 2],
        ),
        collapse_panels=True,
    )


def _warn(msg):
    try:
        print("RERUN-FAULT %s" % msg)
    except Exception:
        pass


def parse_track_line(line):
    """A TRACK decision line -> dict of parsed values, or None if it is not a TRACK line.

    Pure (no Rerun dependency) so it is trivially unit-testable."""
    m = _TRACK_RE.match(line)
    if not m:
        return None
    g = m.groupdict()

    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    return {
        "track_id": g["id"],
        "locked": bool(g["lock"]),
        "cx": _f(g["cx"]), "cy": _f(g["cy"]),
        "range": None if g["rng"] == "n/a" else _f(g["rng"]),
        "rsrc": g["rsrc"],
        "bearing": _f(g["bearing"]),
        "vx": _f(g["vx"]), "vyaw": _f(g["vyaw"]),
        "cost": _f(g["cost"]),
        "sim": _f(g["sim"]), "conf": _f(g["conf"]),
    }


class RerunSink:
    """Inert-by-default Rerun recording sink. Construct with enabled=True to record."""

    def __init__(self, enabled=False, app_id="k1_follow", mode="save", path=None,
                 addr=None, image_every_n=3, min_safe=0.6, standoff=1.2, max_follow=0.0,
                 never_disable=False):
        self.ok = False
        self.rr = None
        self.path = None
        self._faults = 0
        self._last_fault_warn_t = 0.0
        self._never_disable = bool(never_disable)
        self._img_n = max(1, int(image_every_n or 1))
        self._min_safe = float(min_safe or 0.0)
        self._standoff = float(standoff or 0.0)
        self._max_follow = float(max_follow or 0.0)
        self._refs_done = False
        self._intr_done = False
        if not enabled:
            return
        try:
            import rerun as rr
            rr.init(app_id, spawn=False)
            if mode == "connect" and addr:
                rr.connect_grpc(addr)
                self.path = addr
            else:
                self.path = path or ("k1_follow_%d.rrd" % int(_time.time()))
                rr.save(self.path)
            self.rr = rr
            self.ok = True
            try:                                   # embed the default layout INTO the recording
                bp = default_blueprint()
                if bp is not None:
                    rr.send_blueprint(bp)
            except Exception as be:                # a missing blueprint API must not disable data
                _warn("blueprint send failed: %s (data still records)" % be)
        except Exception as e:  # noqa: BLE001 -- Rerun must never break the caller
            self.rr = None
            self.ok = False
            _warn("init failed: %s -> disabled (follow proceeds)" % e)

    # ---- internal fault latch: a broken Rerun disables itself, never spams/steals the loop ----
    # NEVER-DISABLE MODE (2026-09-09): when the sink was built with never_disable=True the
    # 20-fault auto-disable is skipped -- Rerun stays ok=True and per-call try/except still
    # swallows every exception, so the follow loop is unaffected but the recording never
    # closes mid-run. Fault-log spam is rate-limited to at most 1 warning every 5s so a
    # persistent broken sink doesn't drown the stderr.
    def _fault(self, e):
        self._faults += 1
        try:
            now = _time.monotonic()
        except Exception:  # noqa: BLE001
            now = 0.0
        if self._never_disable:
            if (now - self._last_fault_warn_t) >= 5.0:
                self._last_fault_warn_t = now
                _warn("log fault #%d: %s (never-disable: sink kept alive)" % (self._faults, e))
            return
        if self._faults <= 3:
            _warn("log fault #%d: %s" % (self._faults, e))
        if self._faults >= 20:
            self.ok = False
            _warn("disabled after %d faults (follow proceeds)" % self._faults)

    # ---------- timeline (dual: frame_idx sequence + wallclock) ----------
    def frame(self, seq, wall=None):
        if not self.ok:
            return
        try:
            self.rr.set_time("frame_idx", sequence=int(seq))
            if wall is not None:
                self.rr.set_time("wall", timestamp=float(wall))
        except Exception as e:
            self._fault(e)

    # ---------- cheap signals (control-loop safe) ----------
    def scalar(self, entity, value):
        if not self.ok or value is None:
            return
        try:
            self.rr.log(entity, self.rr.Scalars(float(value)))
        except Exception as e:
            self._fault(e)

    def text(self, entity, msg, level="INFO"):
        if not self.ok:
            return
        try:
            self.rr.log(entity, self.rr.TextLog(str(msg), level=str(level)))
        except Exception as e:
            self._fault(e)

    def boxes(self, entity, box, label=None):
        """box = (x1, y1, x2, y2) in pixels."""
        if not self.ok or not box:
            return
        try:
            x1, y1, x2, y2 = (float(v) for v in box)
            self.rr.log(entity, self.rr.Boxes2D(
                mins=[[x1, y1]], sizes=[[x2 - x1, y2 - y1]],
                labels=([str(label)] if label is not None else None)))
        except Exception as e:
            self._fault(e)

    def state(self, entity, name):
        """FSM state as BOTH a scrubbable numeric series (entity+'_id') and a text log."""
        if not self.ok:
            return
        self.scalar(entity + "_id", _STATE_CODE.get(str(name), -1.0))
        self.text(entity, name)

    # ---------- heavy signals (cam-spin thread / decimated) ----------
    def image(self, entity, bgr, seq=0):
        if not self.ok:
            return
        try:
            if (int(seq) % self._img_n) != 0:
                return
            import numpy as np
            rgb = np.ascontiguousarray(bgr[:, :, ::-1])   # BGR->RGB COPY (never alias caller buffer)
            self.rr.log(entity, self.rr.Image(rgb))
        except Exception as e:
            self._fault(e)

    def depth(self, entity, depth_m, seq=0):
        if not self.ok:
            return
        try:
            if (int(seq) % self._img_n) != 0:
                return
            import numpy as np
            d = np.ascontiguousarray(depth_m, dtype=np.float32)   # COPY + cast (never alias)
            self.rr.log(entity, self.rr.DepthImage(d, meter=1.0))
        except Exception as e:
            self._fault(e)

    # ---------- static reference lines (log once, timeless) ----------
    def refs_once(self):
        if not self.ok or self._refs_done:
            return
        self._refs_done = True
        try:
            for name, val in (("min_safe", self._min_safe),
                              ("standoff", self._standoff),
                              ("max", self._max_follow)):
                if val and val > 0:
                    self.rr.log("/follow/range/%s" % name,
                                self.rr.Scalars(float(val)), static=True)
        except Exception as e:
            self._fault(e)

    # ---------- camera intrinsics (P6.1: log once per run for P8 reconstruction) ----------
    def pinhole(self, entity, w, h, fx, fy, cx, cy):
        """Log the camera model as a static rr.Pinhole so the .rrd is self-describing for offline
        RGBD work (P8). Idempotent (once-per-run latch). rr.Pinhole(resolution/focal_length/
        principal_point) is VERIFIED on the pinned 0.23.1 (0 faults) AND read back on 0.33.1
        (docs/RERUN_COMPAT.md, eval/compat/); wrapped in the fault latch so an API mismatch would just
        no-op -- Rerun never breaks the follow -- and intrinsics.json is a sidecar backstop regardless."""
        if not self.ok or self._intr_done:
            return
        try:
            self.rr.log(entity, self.rr.Pinhole(
                resolution=[float(w), float(h)],
                focal_length=[float(fx), float(fy)],
                principal_point=[float(cx), float(cy)]), static=True)
        except Exception as e:
            self._fault(e)

    def write_intrinsics(self, data):
        """Write the intrinsics dict as an intrinsics.json sidecar beside the .rrd (P6.1), so the
        offload bundle (P6.2) and P8.1 calibration have a machine-readable camera model that does
        not depend on parsing the .rrd. Once-per-run; save-mode only (no path in connect mode);
        never raises into the caller."""
        if not self.ok or self._intr_done:
            return
        self._intr_done = True   # latch regardless: pinhole()+sidecar are a single once-per-run emit
        try:
            if not self.path or "://" in str(self.path) or ":" in os.path.basename(str(self.path)):
                return           # connect-mode addr (host:port) -> no sidecar, .rrd Pinhole still logged
            out = os.path.join(os.path.dirname(os.path.abspath(self.path)), "intrinsics.json")
            with open(out, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception as e:
            self._fault(e)

    # ---------- Phase-1 log-line adapter (replay harness only) ----------
    def feed_log_line(self, line):
        """Parse a node decision line already emitted THIS frame and log its scalars/events.

        Phase 1 only (zero node change). The box overlay is logged separately from the tapped
        seed object -- the TRACK line carries only the centroid, not the box corners.

        HONEST LIMIT: /cmd/forbid_forward is DERIVED here (rsrc != depth, or range <= min_safe)
        because the boolean is not in the TRACK line; Phase 2 logs the REAL flag
        (follow_person_k1.py:3486). In the replay harness depth is stubbed off, so rsrc is always
        'bboxH' and forbid_forward reads 1 -- honest given no depth bag (see RERUN_PLAN.md)."""
        if not self.ok or not line:
            return
        t = parse_track_line(line)
        if t is not None:
            self.scalar("/follow/range", t["range"])
            self.scalar("/follow/range_source", _RSRC_CODE.get(t["rsrc"], 0.0))
            self.scalar("/follow/bearing", t["bearing"])
            self.scalar("/cmd/vx", t["vx"])
            self.scalar("/cmd/vyaw", t["vyaw"])
            self.scalar("/reid/sim", t["sim"])
            self.scalar("/track/conf", t["conf"])
            self.scalar("/track/cost", t["cost"])
            ff = 1.0 if (t["rsrc"] != "depth" or
                         (t["range"] is not None and self._min_safe > 0.0
                          and t["range"] <= self._min_safe)) else 0.0
            self.scalar("/cmd/forbid_forward", ff)
            return
        for pre in _EVENT_PREFIXES:
            if line.startswith(pre):
                if "ERR" in line or "ABORT" in line:
                    lvl = "ERROR"
                elif "STARVED" in line or "RANGE-GATE" in line or "SLOW-LOOP" in line:
                    lvl = "WARN"
                else:
                    lvl = "INFO"
                self.text("/events", line, level=lvl)
                rg = _RANGEGATE_RE.match(line)
                if rg:
                    self.scalar("/follow/range", float(rg.group("rng")))
                    self.scalar("/follow/range_source", _RSRC_CODE.get(rg.group("rsrc"), 0.0))
                break

    # ---------- flush/close (ensure the .rrd is written) ----------
    def close(self):
        if not self.ok:
            return
        try:
            rec = self.rr.get_global_data_recording()
            if rec is not None:
                rec.flush()                  # no-arg: blocking on 0.23 (blocking=True) AND 0.33 (timeout_sec)
        except Exception:
            try:
                self.rr.rerun_shutdown()     # last-ditch: flushes all recordings
            except Exception:
                pass


def write_blueprint(path, app_id="k1_follow"):
    """Write the default layout to a standalone .rbl (checked into the repo for the app to ship)."""
    bp = default_blueprint()
    if bp is None:
        raise SystemExit("blueprint API unavailable (rerun.blueprint import failed)")
    bp.save(app_id, path)
    return path


if __name__ == "__main__":
    # `python k1_rerun.py <out.rbl>` regenerates the checked-in blueprint.
    import sys as _sys
    _out = _sys.argv[1] if len(_sys.argv) > 1 else "k1_follow.rbl"
    print("wrote %s" % write_blueprint(_out))
