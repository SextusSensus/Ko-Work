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
    """Serves exactly the accessor surface Follower._process_frame touches. Depth is OFF
    (latest_depth None -> rsrc='bboxH' -> forward drive impossible), so the gate scores the
    identity/FSM layer -- which is what the OPS_BEHAVIOR_EVAL clips are about."""
    def __init__(self, clock, fps):
        self.clock = clock
        self.fps = float(fps)
        self.frames_total = 0
        self._depth_count = 0
    def latest_depth(self, max_age=0.5):
        return None
    def depth_health(self, now=None):
        return "WARMING", 0.0     # WARMING => the depth-starved latch stays byte-inert
    def rgb_fps(self):
        return self.fps
    def rgb_stamp(self):
        return self.clock.now     # always 'fresh' relative to the fake clock
    def depth_stamp(self):
        return 0.0
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


def replay(node_path, clip, flags, fps=10.0, rerun_out=None, rerun_image_every=3):
    """Run one clip through the follow stack; return the captured decision-log lines.

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
    a = ap.parse_args()

    if a.mode == "run":
        if not a.clip:
            ap.error("run needs --clip")
        lines = replay(a.node, a.clip, a.flags, a.fps,
                       rerun_out=a.rerun, rerun_image_every=a.rerun_image_every)
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
            lines = replay(a.node, c["clip"], c.get("flags", a.flags), a.fps)
            ok, fails = check_clip(lines, c.get("checks", []))
            results.append(ok)
            print("%s  %s" % ("PASS" if ok else "FAIL", c["clip"]))
            for fmsg in fails:
                print("    " + fmsg)
        k, n = sum(results), len(results)
        lo, hi = wilson(k, n)
        print("SCORE %d/%d  wilson95=[%.2f, %.2f]" % (k, n, lo, hi))
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
        print("SELFTEST-OK deterministic (%d lines) + no-op-flag equivalence hold" % len(la))
        return 0


if __name__ == "__main__":
    sys.exit(main())
