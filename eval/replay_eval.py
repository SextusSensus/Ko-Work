#!/usr/bin/env python3
"""Replay eval harness (P2 #14) -- the --drive regression gate OPS_BEHAVIOR_EVAL.md specifies.

Feeds a recorded clip (mp4 or a directory of frames) through the REAL deployed follow stack
(YOLO + appearance + identity + FSM) headless ON THE ROBOT, captures the node's own decision-log
lines, and scores them. Motion-free by construction: args are forced to --preview and the
Follower never gets a bridge. A synthetic monotonic clock (1/fps per frame) replaces wall time,
so identical inputs + flags produce identical decision streams -> byte-level compares work.

Modes
  run      --clip X [--flags "..."]                 one config -> decision log + summary
  compare  --clip X --flags-a "..." --flags-b "..." assert IDENTICAL decision streams
           (the rollback-equivalence / no-op-flag machine check; exit 1 on divergence)
  expect   --manifest clips.json                    per-clip pattern predicates + Wilson score
           manifest: {"clips":[{"clip":p,"flags":s,"checks":[{"require":pat}|{"forbid":pat}|
                      {"max":{"pattern":pat,"n":N}}]}]}   (pat = python regex over log lines)
  selftest                                          synth no-person clip + determinism compare

Caveats (honest): TRT FP16 inference is deterministic in practice but not guaranteed bitwise;
log values are rounded so real-clip compares are stable, and the no-person selftest is exactly
deterministic. Byte-identity to HISTORICAL builds (true Stage-1 rollback) needs the old file --
this tool proves equivalence between two flag-sets of the CURRENT build.
"""
import argparse, importlib.util, json, math, os, re, sys, time as _time

NODE_PATH_DEFAULT = "/home/booster/follow_person_k1.py"
_REAL_MONOTONIC = _time.monotonic


class FakeClock:
    """Deterministic replay clock: starts at t0, advances only via tick()."""
    def __init__(self, t0=1000.0):
        self.now = t0
    def tick(self, dt):
        self.now += dt
    def monotonic(self):
        return self.now


class StubNode:
    """Serves exactly the accessor surface Follower._process_frame touches. By DEFAULT depth is OFF
    (latest_depth None -> rsrc='bboxH' -> forward drive impossible), so the identity/FSM layer is
    scored in isolation -- byte-identical to the original stub. P5.2: set _depth_range (metres) to
    inject a uniform synthetic depth PLANE so the depth-dependent paths run OFFLINE -- forward-drive-
    on-depth, the obstacle brake (_corridor_clearance), and the ARMED-relock range-admission gate
    (the 9 m-lunge class). A uniform plane makes both the corridor band and the target box read that
    range; the replay loop updates _frame_hw + _depth_range per frame from a schedule (glitch-able)."""
    def __init__(self, clock, fps):
        self.clock = clock
        self.fps = float(fps)
        self.frames_total = 0
        self._depth_count = 0
        self._depth_range = None       # None -> depth OFF (original byte-inert behavior)
        self._frame_hw = (480, 640)    # HxW of the current frame; set per-frame when depth is on
    def latest_depth(self, max_age=0.5):
        if self._depth_range is None:
            return None
        import numpy as np
        h, w = self._frame_hw
        return np.full((int(h), int(w)), float(self._depth_range), dtype=np.float32)
    def depth_health(self, now=None):
        if self._depth_range is None:
            return "WARMING", 0.0     # WARMING => the depth-starved latch stays byte-inert
        return "FRESH", self.fps      # injected depth -> the depth paths activate
    def rgb_fps(self):
        return self.fps
    def rgb_stamp(self):
        return self.clock.now     # always 'fresh' relative to the fake clock
    def depth_stamp(self):
        return self.clock.now if self._depth_range is not None else 0.0
    def destroy_node(self):
        pass


def load_follow(node_path):
    # The node imports sibling modules (P3: common.py, and later bridge/tracking/... ) -- add its
    # dir to sys.path so exec_module resolves them, mirroring how the robot runs it as a script.
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def frames_from(clip):
    """Yield BGR frames from an mp4/avi or a directory of images (sorted)."""
    import cv2
    if os.path.isdir(clip):
        names = sorted(n for n in os.listdir(clip)
                       if n.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")))
        if not names:
            raise SystemExit("no image frames in dir: %s" % clip)
        for n in names:
            img = cv2.imread(os.path.join(clip, n))
            if img is not None:
                yield img
        return
    cap = cv2.VideoCapture(clip)
    if not cap.isOpened():
        raise SystemExit("cannot open clip: %s" % clip)
    while True:
        ok, img = cap.read()
        if not ok:
            break
        yield img
    cap.release()


def _make_sink(node_path, args, rerun_out, image_every):
    """Build a k1_rerun.RerunSink (Phase 1). Import is best-effort: a missing k1_rerun.py or
    rerun-sdk degrades to a no-op sink -- an eval is NEVER blocked by Rerun (same contract the
    node uses). k1_rerun.py sits next to the node (both deploy to /home/booster/)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
        import k1_rerun
    except Exception as e:  # noqa: BLE001
        print("RERUN-SKIP cannot import k1_rerun (%s) -> no .rrd" % e)
        return None
    return k1_rerun.RerunSink(
        enabled=True, path=rerun_out, image_every_n=image_every,
        min_safe=getattr(args, "min_safe_range", 0.0),
        standoff=getattr(args, "standoff_m", 0.0),
        max_follow=getattr(args, "max_follow_range", 0.0))


def replay(node_path, clip, flags, fps=10.0, rerun_out=None, rerun_image_every=3, depth=None):
    """Run one clip through the follow stack; return the captured decision-log lines.

    depth (P5.2): None -> depth OFF (original). Else a callable(frame_idx)->range_m|None that
    injects a synthetic depth plane per frame, so the depth-dependent paths run offline (feed a
    glitch range at chosen frames to exercise the relock range-admission gate).

    When rerun_out is set, also build a scrubbable .rrd via k1_rerun.RerunSink (Phase 1): RGB +
    target box + FSM state per frame, plus every TRACK/RANGE-GATE/AUTO-RELOCK line parsed into
    scalars/events. ZERO node change -- the .rrd is built from the mod.log tap + the tapped seed."""
    mod = load_follow(node_path)
    clock = FakeClock()
    _time.monotonic = clock.monotonic          # deterministic time for BOTH harness + module
    sink = None
    try:
        argv = ["--preview", "--topic", "/replay"] + [t for t in flags.split() if t]
        if "--drive" in argv:
            raise SystemExit("replay refuses --drive (motion-free gate)")
        args = mod.parse_args(argv)
        decisions = []
        mod.log = lambda msg: decisions.append(str(msg))   # capture the node's own telemetry
        f = mod.Follower(args)
        f.node = StubNode(clock, fps)
        f.det = mod.PersonDetector(args.yolo_path, args.conf)
        if not f.det.ok:
            raise SystemExit("YOLO failed to load (%s) -- run on the robot with ROS env sourced"
                             % args.yolo_path)
        if rerun_out:
            sink = _make_sink(node_path, args, rerun_out, rerun_image_every)
            if sink is not None:
                sink.refs_once()
        n = 0
        for frame in frames_from(clip):
            clock.tick(1.0 / fps)
            f.node.frames_total += 1
            if depth is not None:                       # P5.2: inject this frame's synthetic depth
                f.node._frame_hw = frame.shape[:2]
                f.node._depth_range = depth(n)
            prev = len(decisions)
            f._process_frame(frame)
            if sink is not None:
                sink.frame(n, clock.now)
                sink.image("/camera/rgb", frame, seq=n)
                seed = getattr(f, "seed", None)
                if seed is not None and getattr(seed, "box", None) is not None:
                    sink.boxes("/camera/rgb/target", seed.box, getattr(seed, "track_id", None))
                sink.state("/fsm/state", getattr(f, "state", "?"))
                for ln in decisions[prev:]:               # only THIS frame's lines
                    sink.feed_log_line(ln)
            n += 1
        decisions.append("REPLAY-END frames=%d" % n)
        if sink is not None and sink.ok:
            decisions.append("RERUN-SAVED %s" % sink.path)
        return decisions
    finally:
        if sink is not None:
            sink.close()
        _time.monotonic = _REAL_MONOTONIC


def wilson(k, n, z=1.96):
    """95% Wilson interval (same math as sim-eval's scorecard)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def check_clip(log_lines, checks):
    """Apply pattern predicates; return (passed, list-of-failure-strings)."""
    fails = []
    for ch in checks:
        if "require" in ch:
            if not any(re.search(ch["require"], ln) for ln in log_lines):
                fails.append("REQUIRE missing: %s" % ch["require"])
        elif "forbid" in ch:
            hits = [ln for ln in log_lines if re.search(ch["forbid"], ln)]
            if hits:
                fails.append("FORBID hit %dx: %s (first: %s)" % (len(hits), ch["forbid"], hits[0]))
        elif "max" in ch:
            pat, nmax = ch["max"]["pattern"], int(ch["max"]["n"])
            c = sum(1 for ln in log_lines if re.search(pat, ln))
            if c > nmax:
                fails.append("MAX exceeded: %s -> %d > %d" % (pat, c, nmax))
    return (not fails), fails


# TRACK line -> (id, range_m, range_src, vx). Format:
#   "TRACK id=1 LOCK c=(273,231) range=1.40[depth] bearing=+00.3deg vx=+0.00 vyaw=... sim=..."
_TRACK_RE = re.compile(r"TRACK id=(\d+)\s+LOCK\s+.*?range=([\d.]+)\[(\w+)\].*?\svx=([+-]?[\d.]+)")


def score_outcome(log_lines, oc):
    """P5.1: score TASK OUTCOME from the decision stream (not log-pattern predicates). Returns
    (passed, metrics). Metrics over TRACK/LOCK frames:
      - track_frac            : TRACK frames / total frames                (>= min_track_frac)
      - standoff_in_band_frac : |range - standoff_m| <= standoff_band_m    (>= min_standoff_in_band_frac)
      - forbidden_forward     : forward vx (> forward_eps) with NO depth range (bboxH) -> lunge risk  (<= max_forbidden_forward)
      - geofence_breach       : range > geofence_m while locked            (<= max_geofence_breach)
      - id_switches           : locked track_id changes frame-to-frame     (<= max_id_switches)
      - operator_retention    : frac of TRACK frames on the labeled operator_id (>= min_operator_retention)
    ID CORRECTNESS through crossings needs a labeled operator_id; without it only ID STABILITY
    (no churn) is scored -- the honest split between what the log proves and what a label proves."""
    total = 0
    for ln in log_lines:
        m = re.search(r"REPLAY-END frames=(\d+)", ln)
        if m:
            total = int(m.group(1))
    tracks = [(int(m.group(1)), float(m.group(2)), m.group(3), float(m.group(4)))
              for ln in log_lines for m in [_TRACK_RE.search(ln)] if m]
    n = len(tracks)
    metrics = {"frames": total, "track_frames": n}
    fails = []
    if n == 0:
        if oc.get("min_track_frac", 0.0) > 0.0:
            fails.append("no TRACK/LOCK frames (min_track_frac=%.2f)" % oc["min_track_frac"])
        metrics["fails"] = fails
        return (not fails), metrics

    if total > 0 and "min_track_frac" in oc:
        tf = n / total
        metrics["track_frac"] = round(tf, 3)
        if tf < oc["min_track_frac"]:
            fails.append("track_frac %.2f < %.2f" % (tf, oc["min_track_frac"]))

    so, band = oc.get("standoff_m"), oc.get("standoff_band_m")
    if so is not None and band is not None:
        frac = sum(1 for (_, r, _, _) in tracks if abs(r - so) <= band) / n
        metrics["standoff_in_band_frac"] = round(frac, 3)
        if frac < oc.get("min_standoff_in_band_frac", 0.0):
            fails.append("standoff_in_band %.2f < %.2f" % (frac, oc.get("min_standoff_in_band_frac", 0.0)))

    eps = oc.get("forward_eps", 0.02)
    ff = sum(1 for (_, _, src, vx) in tracks if vx > eps and src != "depth")
    metrics["forbidden_forward"] = ff
    if ff > oc.get("max_forbidden_forward", 10 ** 9):
        fails.append("forbidden_forward %d > %d" % (ff, oc["max_forbidden_forward"]))

    gf = oc.get("geofence_m")
    if gf is not None:
        gb = sum(1 for (_, r, _, _) in tracks if r > gf)
        metrics["geofence_breach"] = gb
        if gb > oc.get("max_geofence_breach", 0):
            fails.append("geofence_breach %d > %d" % (gb, oc.get("max_geofence_breach", 0)))

    ids = [t[0] for t in tracks]
    switches = sum(1 for i in range(1, len(ids)) if ids[i] != ids[i - 1])
    metrics["id_switches"] = switches
    op = oc.get("operator_id")
    if op is not None:
        ret = sum(1 for i in ids if i == op) / n
        metrics["operator_retention"] = round(ret, 3)
        if ret < oc.get("min_operator_retention", 1.0):
            fails.append("operator_retention %.2f < %.2f" % (ret, oc.get("min_operator_retention", 1.0)))
    elif "max_id_switches" in oc and switches > oc["max_id_switches"]:
        fails.append("id_switches %d > %d" % (switches, oc["max_id_switches"]))

    metrics["fails"] = fails
    return (not fails), metrics


def make_selftest_clip(path, n=40, w=640, h=480):
    """No-person synthetic frames (noise + a moving box): exactly deterministic decisions."""
    import numpy as np, cv2
    os.makedirs(path, exist_ok=True)
    rng = np.random.RandomState(42)
    for i in range(n):
        img = rng.randint(0, 60, (h, w, 3), dtype=np.uint8)
        x = 40 + i * 8
        cv2.rectangle(img, (x, 200), (x + 60, 280), (0, 160, 255), -1)
        cv2.imwrite(os.path.join(path, "f%03d.png" % i), img)
    return path


def _depth_schedule(base_range, glitch_spec):
    """P5.2: build a per-frame depth-range function from a base range + optional per-frame glitches
    ('i=G,i=G', e.g. '30=9.0' injects a 9 m read at frame 30 to trip the relock range gate). Returns
    None (depth OFF) when base_range is None -- so the harness default is byte-identical."""
    if base_range is None:
        return None
    glitches = {}
    for tok in (glitch_spec or "").split(","):
        tok = tok.strip()
        if "=" in tok:
            i, g = tok.split("=", 1)
            glitches[int(i.strip())] = float(g.strip())
    return lambda i: glitches.get(i, float(base_range))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["run", "compare", "expect", "selftest"])
    ap.add_argument("--node", default=NODE_PATH_DEFAULT, help="deployed follow_person_k1.py")
    ap.add_argument("--clip", help="mp4/avi or directory of frames")
    ap.add_argument("--flags", default="", help="extra node flags (run/expect default)")
    ap.add_argument("--flags-a", default="", help="compare: first flag-set")
    ap.add_argument("--flags-b", default="", help="compare: second flag-set")
    ap.add_argument("--manifest", help="expect: clips.json")
    ap.add_argument("--fps", type=float, default=10.0, help="replay clock rate")
    ap.add_argument("--dump", help="write the decision log(s) to this file/prefix")
    ap.add_argument("--rerun", help="run mode: also write a scrubbable Rerun .rrd to this path "
                                    "(needs k1_rerun.py next to the node + rerun-sdk)")
    ap.add_argument("--rerun-image-every", type=int, default=3,
                    help="log 1 in N RGB frames to the .rrd (default 3)")
    ap.add_argument("--depth-range", type=float, default=None,
                    help="P5.2: inject a uniform depth plane at R metres so the depth paths run "
                         "offline (default: depth OFF, original identity/FSM-only scoring)")
    ap.add_argument("--depth-glitch", default="",
                    help="P5.2: override the depth range at frames 'i=G,i=G' (e.g. 30=9.0 -> a 9 m "
                         "glitch at frame 30, to exercise the ARMED-relock range-admission gate)")
    a = ap.parse_args()
    cli_depth = _depth_schedule(a.depth_range, a.depth_glitch)

    if a.mode == "run":
        if not a.clip:
            ap.error("run needs --clip")
        lines = replay(a.node, a.clip, a.flags, a.fps,
                       rerun_out=a.rerun, rerun_image_every=a.rerun_image_every, depth=cli_depth)
        for ln in lines:
            print(ln)
        if a.dump:
            open(a.dump, "w").write("\n".join(lines) + "\n")
        return 0

    if a.mode == "compare":
        if not a.clip:
            ap.error("compare needs --clip")
        la = replay(a.node, a.clip, a.flags_a, a.fps)
        lb = replay(a.node, a.clip, a.flags_b, a.fps)
        if a.dump:
            open(a.dump + ".a", "w").write("\n".join(la) + "\n")
            open(a.dump + ".b", "w").write("\n".join(lb) + "\n")
        if la == lb:
            print("COMPARE-OK identical decision streams (%d lines) [A=%r] [B=%r]"
                  % (len(la), a.flags_a, a.flags_b))
            return 0
        print("COMPARE-DIVERGED lines A=%d B=%d" % (len(la), len(lb)))
        for i, (x, y) in enumerate(zip(la, lb)):
            if x != y:
                print("  first diff @%d:\n    A: %s\n    B: %s" % (i, x, y))
                break
        return 1

    if a.mode == "expect":
        if not a.manifest:
            ap.error("expect needs --manifest")
        man = json.load(open(a.manifest))
        results = []
        for c in man["clips"]:
            cdepth = _depth_schedule(c.get("depth_range", a.depth_range), c.get("depth_glitch", a.depth_glitch))
            lines = replay(a.node, c["clip"], c.get("flags", a.flags), a.fps, depth=cdepth)
            ok, fails = check_clip(lines, c.get("checks", []))
            metrics = None
            if "outcome" in c:                     # P5.1: objective task-success on top of patterns
                ook, metrics = score_outcome(lines, c["outcome"])
                ok = ok and ook
                fails += ["outcome: " + f for f in metrics.get("fails", [])]
            results.append(ok)
            print("%s  %s" % ("PASS" if ok else "FAIL", c["clip"]))
            if metrics is not None:
                print("    metrics: " + json.dumps({k2: v for k2, v in metrics.items() if k2 != "fails"}))
            for fmsg in fails:
                print("    " + fmsg)
        k, n = sum(results), len(results)
        lo, hi = wilson(k, n)
        print("TASK-SUCCESS %d/%d = %.1f%%  wilson95=[%.2f, %.2f]" % (k, n, (100.0 * k / n if n else 0.0), lo, hi))
        return 0 if k == n else 1

    if a.mode == "selftest":
        clip = make_selftest_clip("/tmp/replay_selftest")
        # determinism: the same config twice MUST be byte-identical under the fake clock
        la = replay(a.node, clip, "", a.fps)
        lb = replay(a.node, clip, "", a.fps)
        if la != lb:
            print("SELFTEST-FAIL nondeterministic decisions on identical input")
            return 1
        # no-op-flag equivalence on a no-person clip: the REID fault watchdog can't change
        # decisions when no embeddings ever run
        lc = replay(a.node, clip, "--reid-fault-k 0", a.fps)
        if la != lc:
            print("SELFTEST-FAIL --reid-fault-k 0 changed a no-person decision stream")
            return 1
        # P5.2: depth injection must be deterministic AND actually served (so the depth-dependent
        # paths -- obstacle brake, relock range gate -- can run offline). The no-person clip has no
        # target, so this asserts the MECHANISM (determinism + a served plane), not a follow outcome;
        # the full relock-glitch reproduction needs a labeled person clip (VERIFY-with-clips).
        ld = replay(a.node, clip, "--obstacle-brake", a.fps, depth=lambda i: 1.5)
        le = replay(a.node, clip, "--obstacle-brake", a.fps, depth=lambda i: 1.5)
        if ld != le:
            print("SELFTEST-FAIL depth-injected replay nondeterministic")
            return 1
        _sn = StubNode(FakeClock(), a.fps); _sn._depth_range = 1.5; _sn._frame_hw = (8, 8)
        if _sn.latest_depth() is None or _sn.latest_depth().shape != (8, 8) or _sn.depth_health()[0] != "FRESH":
            print("SELFTEST-FAIL depth stub did not serve an injected 1.5m plane")
            return 1
        print("SELFTEST-OK deterministic (%d lines) + no-op-flag + depth-inject hold" % len(la))
        return 0


if __name__ == "__main__":
    sys.exit(main())
