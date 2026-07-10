#!/usr/bin/env python3
"""synth_fixtures.py -- charter item S6 (docs/SCAFFOLD_CHARTER.md): the ONE deterministic synthetic
fixture source every blind-authored scaffold self-tests against (S1 batch_ingest, S2 Recon.ps1's
bundle shape, the deferred S5 train contract). Three ad-hoc fabricators would drift; this file is
the single off-robot source of offload-bundle-shaped truth.

What it emits (MIRRORS shipped producers, never invents -- every shape below was read from the
producer named next to it):

  runs/<run_id>/                       bundle shape per robot/offload_run.sh (P6.2)
    manifest.json                      offload_run.sh L143-162 schema: run_id/created_utc/
                                       git_version/profile/duration_s/file_count/total_bytes/
                                       files[{name,bytes,sha256}], json indent=2 sort_keys,
                                       manifest.json itself EXCLUDED from files[]. Labeled bundles
                                       additionally carry the `label` block + top-level `outcome`
                                       exactly as eval/label_run.py writes them (we call the REAL
                                       label_run.label_bundle -- outcome vocabulary is derived,
                                       never enumerated here).
    events.jsonl                       per-tick {t,fsm,walk,vx,vy,vyaw,loop_ms,seed,hb_req} in that
                                       key order (robot/follow_person_k1.py run() ->
                                       common.EventLog.tick; note `seed` is the BOOL "a lock seed
                                       exists", not an RNG seed).
    k1_follow.err                      the node CONFIG line, TRACK decision lines matching BOTH
                                       replay_eval._TRACK_RE (the P5.1 scorer) and
                                       k1_rerun._TRACK_RE (the strict runtime parser) -- asserted
                                       at generation time -- plus a final REPLAY-END frames=N line.
    config/defaults.yaml               flat `key: value` scalars readable by label_run._yaml_scalar.
    k1_follow_<epoch>.rrd              capture bundles only, when rerun-sdk is importable: written
    intrinsics.json                    through the REAL robot/k1_rerun.RerunSink (entity paths
                                       /follow/*, /cmd/*, /reid/sim, /track/conf, /track/cost,
                                       /health/depth_fps, /fsm/state_id, decimated /camera/rgb,
                                       /camera/depth DepthImage(meter=1.0), static Pinhole + range
                                       ref lines, dual timelines frame_idx + wall). intrinsics.json
                                       mirrors robot/perception.pinhole_intrinsics (approximate:
                                       true, fy:=fx) so the S3 fail-closed intrinsics gate has a
                                       real target.

Planted edge cases (charter S6, required not optional -- see BUNDLE_SPECS):
  pass_a / pass_b / pass_c   3 valid capture bundles the REAL scorer labels `pass` (asserted; a
                             mislabel is a loud SystemExit = scorer/threshold drift alarm). pass_b
                             carries one id switch + a SEARCHING gap; pass_c carries range=n/a
                             TRACK frames (the no-depth class) + a PARKED tail.
  unlabeled                  a capture bundle with NO label block / NO manifest.outcome (S1 must
                             exclude it loudly as `unlabeled`).
  no_rrd                     a non-capture bundle: events.jsonl + k1_follow.err + config only, per
                             offload_run.sh L59 (pixels/depth are opt-in). Labeled `pass` so it
                             specifically exercises S1's `no-rrd-not-a-capture-run` exclusion.
  unmapped_fsm               mid-run frames in an unknown FSM state ("WANDER") -> /fsm/state_id
                             logs -1.0 (k1_rerun._STATE_CODE fallback) and events.jsonl carries the
                             raw string. S1's FSM assertion MUST abort a mint that includes it.
  under_floor                a `pass` bundle whose REACQUIRE occupancy is exactly 2 frames --
                             under any plausible per-state INSUFFICIENT floor (S5's val report).

Determinism contract (charter: non-negotiable, S1's re-mint==same-content_hash gate sits on it):
  * ALL entropy comes from numpy default_rng([seed, bundle_idx]) -- never time/random. Run stamps,
    epochs, run_ids and labeled_utc are fixed constants (run_ids are SEED-INDEPENDENT identities so
    consumers can key the edge cases; only the data values vary with seed).
  * events.jsonl / k1_follow.err / config / manifest are byte-identical per (seed) -- text files
    are written with newline="\\n" so Windows and Linux produce the same bytes.
  * The .rrd is VALUE-deterministic (the logged scalars/images are exact functions of the seed) but
    its container bytes are NOT claimed identical across regenerations (the SDK embeds store ids).
    S1's determinism gate hashes DECODED dataset bytes, which is what this guarantees.

Pinned cross-file interfaces (multiple agents code against these exact shapes):
  make_episodes(seed=0) -> [ {run_id, frames:list[int], state:(T,7) float32, action:(T,2) float32,
                              rgb:list[HxWx3 uint8 | None],
                              meta:{profile,outcome,git_version,scorer_version,duration_s,
                                    fsm_counts}} ]   -- NO rerun import on this path. Episodes are
    built by the REAL eval/rrd_to_lerobot.assemble_episode over the same raw scalar/image maps the
    .rrd writer logs, so the in-memory seam and the desktop .rrd-decode path can never drift.
    state columns = rrd_to_lerobot.STATE_ENTITIES order:
    [range_m, bearing_deg, rsrc_is_depth, anchor_sim, conf, depth_fps, fsm_state_id].
  make_bundles(out_dir, seed=0, with_rrd=True) -> list of run_id strings (BUNDLE_SPECS order ==
    sorted(run_id) ascending). If rerun-sdk is not importable and with_rrd=True this raises
    SystemExit loudly -- it NEVER silently skips the .rrd. With with_rrd=False every bundle is
    written rrd-less (for rerun-less environments; the no_rrd-vs-capture distinction collapses and
    the .rrd-borne unmapped-FSM abort case is only reachable via make_episodes / with_rrd=True).

Self-test: correctness is asserted by the CONSUMERS (charter S6: S1's
`python eval/batch_ingest.py selftest` reproduces the same content_hash twice over this output).
Own smoke run (also exercises the planted-label asserts against the REAL label_run scorer and an
inline same-seed determinism double-run):

    python eval/synth_fixtures.py --out <fresh-dir> --seed 0            -> SYNTH-FIXTURES-OK
    python eval/synth_fixtures.py --out <fresh-dir> --seed 0 --no-rrd   (no rerun-sdk available)

VERIFY ON DESKTOP: this file ships unexecuted (the authoring laptop has no Python). First desktop
checks: the smoke run above, and that sequential RerunSink init/save per bundle in ONE process
yields 6 independent readable .rrds (the runtime only ever opens one recording per process).
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np

# Sibling-import bootstrap -- same idiom as label_run.py. This file lives in eval/ next to
# replay_eval.py / label_run.py / rrd_to_lerobot.py and needs robot/k1_rerun.py (the runtime .rrd
# writer). All of these are stdlib-only at import time (rerun/numpy/pyarrow load lazily inside
# functions), so importing them here never pulls a robot dependency onto a consumer.
_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
_ROBOT_DIR = os.path.normpath(os.path.join(_EVAL_DIR, os.pardir, "robot"))
for _p in (_EVAL_DIR, _ROBOT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Reuse, never re-type (charter doctrine): the REAL episode assembler (trim-to-first-range +
# nearest-EARLIER image rules are load-bearing review fixes), the REAL runtime state->id map and
# TRACK-line shape, the REAL scorer-side TRACK regex, and the REAL labeler.
from rrd_to_lerobot import assemble_episode              # noqa: E402
from k1_rerun import RerunSink, _STATE_CODE              # noqa: E402
from k1_rerun import _TRACK_RE as _RUNTIME_TRACK_RE     # noqa: E402  strict, anchored at ^
from replay_eval import _TRACK_RE as _SCORER_TRACK_RE   # noqa: E402  what score_outcome parses
import label_run                                         # noqa: E402  label vocabulary + labeler


# ---------------------------------------------------------------------------
# Fixed fixture constants. Stamps/epochs are CONSTANTS, never now() -- byte-determinism.
# ---------------------------------------------------------------------------
_FPS = 10.0                # nominal control tick rate (matches the replay harness default)
_IMG_EVERY = 3             # RGB/depth decimation, mirrors RerunSink image_every_n default
_W, _H = 64, 48            # tiny frames keep the fixture set small; shape is what matters
_HFOV_DEG = 90.0           # fx = (W/2)/tan(hfov/2) -> exactly 32.0 px (clean, deterministic)
_BASE_EPOCH = 1767225600   # 2026-01-01T00:00:00Z; bundle i starts at _BASE_EPOCH + i*3600
_GIT = "5eedc0de"          # fake deploy short-SHA (offload_run.sh: alnum from DEPLOY_VERSION)
_LABELED_UTC = "20260101T120000Z"  # pinned label stamp (see _label: the one wall-clock field)
_STANDOFF_M, _MIN_SAFE_M, _MAX_FOLLOW_M = 1.2, 0.6, 3.0


def _mkspec(idx, kind, **kw):
    """One planted-bundle spec. run_id = <stamp>_<sha> exactly like offload_run.sh builds it; idx
    orders the stamps, so BUNDLE_SPECS order == sorted(run_id) ascending (S1's episode order)."""
    stamp = "20260101T%02d0000Z" % idx
    d = dict(idx=idx, kind=kind, stamp=stamp, epoch=_BASE_EPOCH + idx * 3600,
             run_id="%s_%s" % (stamp, _GIT), profile="capture",
             labeled=True, expect="pass", rrd=True,
             pre=6,           # pre-lock SEARCH_MARKER frames (dropped by assemble_episode's trim)
             t1=20,           # first TRACK stretch
             mid=None, midn=0,  # optional mid-run non-TRACK state (SEARCHING/REACQUIRE/WANDER)
             t2=18,           # second TRACK stretch
             parked=0,        # PARKED tail frames
             id1=3, id2=None,  # locked track id (id2 != id1 plants an id switch)
             na=())           # indices WITHIN the track sequence that log range=n/a[bboxH]
    d.update(kw)
    return d


# The seven charter-required planted bundles. Do not reorder: idx encodes the run_id stamp.
BUNDLE_SPECS = [
    _mkspec(0, "pass_a", pre=8, t1=30, t2=22),
    _mkspec(1, "pass_b", mid="SEARCHING", midn=6, t2=24, id2=5),          # 1 id switch (<= max 8)
    _mkspec(2, "pass_c", pre=5, t1=26, parked=4, id1=4, na=(7, 15)),      # n/a-range TRACK frames
    _mkspec(3, "unlabeled", labeled=False, expect=None, t1=28, t2=6, id1=2),
    _mkspec(4, "no_rrd", rrd=False, profile="dev", t1=24, t2=0),
    _mkspec(5, "unmapped_fsm", t1=18, mid="WANDER", midn=3, t2=16),       # -> /fsm/state_id -1.0
    _mkspec(6, "under_floor", mid="REACQUIRE", midn=2, id1=6),            # 2 frames: under floor
]

# .rrd scalar entities in a fixed log order (excludes /fsm/state_id -- RerunSink.state() owns it,
# logging the numeric series AND the /fsm/state text stream exactly like the live node).
_RRD_SCALAR_ORDER = (
    "/follow/range", "/follow/bearing", "/follow/range_source",
    "/reid/sim", "/track/conf", "/track/cost",
    "/cmd/vx", "/cmd/vyaw", "/cmd/forbid_forward",
    "/health/depth_fps", "/health/rgb_fps",
)

# Flat scalars only: label_run._yaml_scalar parses `key: value` lines, no pyyaml. Values agree
# with the CONFIG line planted in k1_follow.err (that line is label_run's AUTHORITATIVE overlay).
_DEFAULTS_YAML = """\
# Synthetic fixture profile (eval/synth_fixtures.py, charter S6).
# Flat `key: value` scalars only -- readable by label_run._yaml_scalar without pyyaml.
standoff_m: 1.2
min_safe_range: 0.6
max_follow_range: 3.0
coast_frames: 10
max_seconds: 900
require_heartbeat: 1
"""


# ---------------------------------------------------------------------------
# FSM id -> canonical name (ground truth for meta.fsm_counts)
# ---------------------------------------------------------------------------
_FSM_JSON = os.path.join(_EVAL_DIR, "fsm_states.json")
_ID_TO_NAME_CACHE = None


def _id_to_name():
    """Reverse map from the FROZEN eval/fsm_states.json with the single SEARCH/SEARCH_MARKER rule
    (id 1.0 reports as canonical SEARCH_MARKER; plain SEARCH is its documented alias; SEARCHING=1.5
    is distinct). A2's eval/fsm_groups.py is the shared consumer-side helper, but S6 authors FIRST
    (charter ordering), so only the 3-line alias rule is restated here -- the DATA comes from the
    same frozen json, and a drift against the runtime encoder is a loud abort, not a skew."""
    global _ID_TO_NAME_CACHE
    if _ID_TO_NAME_CACHE is not None:
        return _ID_TO_NAME_CACHE
    with open(_FSM_JSON) as f:
        data = json.load(f)
    states = data["states"]
    for name, val in states.items():   # fsm_states.json MUST mirror k1_rerun._STATE_CODE (frozen)
        if abs(_STATE_CODE.get(name, float(data.get("unmapped_id", -1.0))) - float(val)) > 1e-9:
            raise SystemExit("synth_fixtures: eval/fsm_states.json out of sync with "
                             "robot/k1_rerun._STATE_CODE at %r -- fix the drift before generating "
                             "fixtures (the charter freezes these two as mirrors)" % name)
    m = {}
    for name, val in states.items():
        if name == "SEARCH":           # alias of SEARCH_MARKER (same id) -- canonical name wins
            continue
        m[float(val)] = name
    _ID_TO_NAME_CACHE = m
    return m


def _canon(vid, tol=1e-6):
    """Canonical state name for a recorded /fsm/state_id value; UNMAPPED == data bug (incl. -1.0)."""
    for k, name in _id_to_name().items():
        if abs(float(vid) - k) <= tol:
            return name
    return "UNMAPPED"


# ---------------------------------------------------------------------------
# Deterministic raw-content generator (the single source both surfaces feed from)
# ---------------------------------------------------------------------------
def _r(v, nd):
    """round() through float() -- numpy scalars must never leak into json.dumps (not serializable)."""
    return round(float(v), nd)


def _clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _gen_raw(spec, seed):
    """All of one bundle's content: raw .rrd scalar/image/depth maps (exactly what the sink logs),
    events.jsonl lines, k1_follow.err lines, duration. make_episodes() and make_bundles() BOTH feed
    from this one generator, so the in-memory episodes and the on-disk bundles cannot drift.

    Entropy: numpy default_rng([seed, idx]) only -- per-bundle independent streams, so editing one
    spec never reshuffles another bundle's values."""
    if int(seed) < 0:
        raise SystemExit("synth_fixtures: seed must be a non-negative integer")
    rng = np.random.default_rng([int(seed), int(spec["idx"])])

    plan = [("SEARCH_MARKER", None)] * spec["pre"]
    plan += [("TRACK", spec["id1"])] * spec["t1"]
    if spec["midn"]:
        plan += [(spec["mid"], None)] * spec["midn"]
    plan += [("TRACK", spec["id2"] if spec["id2"] is not None else spec["id1"])] * spec["t2"]
    plan += [("PARKED", None)] * spec["parked"]

    scalars, images, depths = {}, {}, {}

    def put(ent, i, v):
        scalars.setdefault(ent, {})[i] = float(v)

    events, err = [], []
    # The node's effective-threshold line (follow_person_k1.py:995) -- label_run.build_oc's
    # AUTHORITATIVE overlay. Values match config/defaults.yaml above by construction.
    err.append("CONFIG standoff_m=%.3f max_follow_range=%.3f min_safe_range=%.3f"
               % (_STANDOFF_M, _MAX_FOLLOW_M, _MIN_SAFE_M))

    first_track = spec["pre"]
    tseq = 0            # index within the TRACK-frame sequence (keys spec["na"])
    last_range = 2.0    # depth-plane seed pre-lock; stays inside S3's 0.15-15 m sanity band
    for i, (fsm, tid) in enumerate(plan):
        wall = spec["epoch"] + i / _FPS
        put("/fsm/state_id", i, _STATE_CODE.get(fsm, -1.0))   # unknown string -> -1.0 (UNMAPPED)
        put("/health/depth_fps", i, 15.0)
        put("/health/rgb_fps", i, 10.0)

        vx = vyaw = 0.0
        if tid is not None:
            na = tseq in spec["na"]
            # Engineered to PASS label_run's oc: ranges inside |r-1.2|<=0.4 (band), < 3.0
            # (geofence); forward vx only with rsrc=depth (forbidden_forward stays 0). The
            # planted-label assert in _label() turns any threshold drift into a loud failure.
            rng_m = (None if na else
                     _r(_clip(1.2 + 0.18 * math.sin(i / 5.0) + rng.normal(0.0, 0.03), 0.9, 1.5), 2))
            bearing = _r(_clip(6.0 * math.sin(i / 7.0) + rng.normal(0.0, 0.8), -15.0, 15.0), 1)
            vx = 0.0 if rng_m is None else _r(_clip(0.5 * (rng_m - _STANDOFF_M), -0.10, 0.12), 2)
            vyaw = _r(_clip(-0.02 * bearing, -0.20, 0.20), 2)
            sim = _r(_clip(rng.normal(0.94, 0.02), 0.85, 0.99), 2)
            conf = _r(_clip(rng.normal(0.90, 0.03), 0.70, 0.99), 2)
            cost = _r(-abs(rng.normal(0.06, 0.02)), 2)
            cx = int(_clip(32 + 14 * math.sin(i / 4.0) + rng.normal(0.0, 2.0), 4, 60))
            cy = int(_clip(24 + rng.normal(0.0, 2.0), 4, 44))
            rsrc = "bboxH" if na else "depth"
            if rng_m is not None:
                put("/follow/range", i, rng_m)
                last_range = rng_m
            put("/follow/range_source", i, 1.0 if na else 2.0)   # k1_rerun._RSRC_CODE
            put("/follow/bearing", i, bearing)
            put("/cmd/vx", i, vx)
            put("/cmd/vyaw", i, vyaw)
            put("/cmd/forbid_forward", i, 1.0 if na else 0.0)    # real-flag semantics: no depth -> 1
            put("/reid/sim", i, sim)
            put("/track/conf", i, conf)
            put("/track/cost", i, cost)
            line = ("TRACK id=%d LOCK c=(%d,%d) range=%s[%s] bearing=%+05.1fdeg "
                    "vx=%+.2f vyaw=%+.2f cost=%+.2f/2nd=- sim=%.2f conf=%.2f"
                    % (tid, cx, cy, ("n/a" if rng_m is None else "%.2f" % rng_m), rsrc,
                       bearing, vx, vyaw, cost, sim, conf))
            # Charter hard requirement: every planted line parses under BOTH shipped regexes.
            if not _SCORER_TRACK_RE.search(line) or not _RUNTIME_TRACK_RE.match(line):
                raise SystemExit("synth_fixtures: generated TRACK line rejected by the shipped "
                                 "parsers (replay_eval/k1_rerun) -- line: %r" % line)
            err.append(line)
            tseq += 1

        if i % _IMG_EVERY == 0:   # decimated exactly like the sink would (seq %% image_every_n)
            img = rng.integers(0, 60, size=(_H, _W, 3), dtype=np.uint8)
            x0 = (4 + 2 * i) % (_W - 12)
            img[18:30, x0:x0 + 10] = np.array([40, 200, 255], dtype=np.uint8)
            images[i] = img       # canonical RGB; the writer hands the sink BGR so the .rrd == this
            depths[i] = np.full((_H, _W), float(_clip(last_range, 0.5, 5.0)), dtype=np.float32)

        loop_ms = _r(30.0 + abs(rng.normal(0.0, 6.0)), 1)
        # EXACT tick shape + key order of follow_person_k1.py run() -> EventLog.tick (L1134-1137).
        # `seed` is the node's "a lock seed exists" BOOLEAN; vy is identically 0.0 (2-DOF twist).
        events.append(json.dumps(
            {"t": _r(wall, 3), "fsm": fsm, "walk": fsm in ("TRACK", "SEARCHING"),
             "vx": _r(vx, 4), "vy": 0.0, "vyaw": _r(vyaw, 4), "loop_ms": loop_ms,
             "seed": i >= first_track, "hb_req": True}, separators=(",", ":")))

    err.append("REPLAY-END frames=%d" % len(plan))   # score_outcome's total-frames denominator
    # Same duration rule as offload_run.sh (first/last events t, 1 decimal).
    dur = _r((len(plan) - 1) / _FPS, 1)
    return {"spec": spec, "plan": plan, "scalars": scalars, "images": images, "depths": depths,
            "events": events, "err": err, "duration_s": dur}


# ---------------------------------------------------------------------------
# Pinned interface 1: in-memory episodes (no rerun anywhere on this path)
# ---------------------------------------------------------------------------
def make_episodes(seed=0):
    """The injected-episode seam S1's selftest consumes (pinned interface -- see module docstring).
    Episodes are produced by the REAL rrd_to_lerobot.assemble_episode over the same raw maps the
    .rrd writer logs: trim-to-first-/follow/range and nearest-EARLIER-image included, so this
    exactly equals what read_rrd()+assemble_episode() yields from the written .rrd.

    meta.fsm_counts is the POST-TRIM per-canonical-state frame count (ground truth for S1's card
    assert; pre-lock SEARCH frames are dropped by the trim, hence near-zero SEARCH_MARKER counts --
    the honest note S1's card carries). meta.outcome is the PLANTED expectation (None == unlabeled);
    make_bundles() asserts it against the real labeler. The no_rrd episode is the in-memory twin of
    a bundle that never recorded pixels: rgb is [None]*T."""
    eps = []
    for spec in BUNDLE_SPECS:
        raw = _gen_raw(spec, seed)
        images = raw["images"] if spec["rrd"] else {}
        frames, state, action, rgb = assemble_episode(raw["scalars"], images)
        counts = {}
        for v in state[:, 6].tolist():
            name = _canon(v)
            counts[name] = counts.get(name, 0) + 1
        eps.append({
            "run_id": spec["run_id"],
            "frames": [int(f) for f in frames],
            "state": state,                     # (T,7) float32, STATE_ENTITIES order
            "action": action,                   # (T,2) float32, [vx, vyaw]
            "rgb": rgb,                         # HxWx3 uint8 or None per frame
            "meta": {"profile": spec["profile"],
                     "outcome": spec["expect"],
                     "git_version": _GIT,
                     "scorer_version": label_run.SCORER_VERSION,
                     "duration_s": raw["duration_s"],
                     "fsm_counts": counts},
        })
    return eps


def spec_for(run_id):
    """Convenience for consumers selecting edge cases by identity (run_ids are seed-independent)."""
    for s in BUNDLE_SPECS:
        if s["run_id"] == run_id:
            return dict(s)
    raise KeyError("synth_fixtures: unknown run_id %r" % run_id)


# ---------------------------------------------------------------------------
# Pinned interface 2: on-disk offload_run.sh-shaped bundles
# ---------------------------------------------------------------------------
def _write_text(path, text):
    # newline="\n" everywhere: Windows text mode would otherwise write \r\n and break the
    # cross-platform byte-determinism the charter demands.
    with open(path, "w", newline="\n") as f:
        f.write(text)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write_rrd(run_dir, raw):
    """Write the capture .rrd + intrinsics.json through the REAL runtime sink (k1_rerun.RerunSink),
    so entity paths / timelines / archetypes are correct by construction, not by imitation. The
    runtime sink swallows faults BY DESIGN (it must never perturb the follow) -- a fixture writer
    must do the opposite, so every silent-failure path is re-checked loudly here."""
    try:
        import rerun  # noqa: F401 -- presence check only; RerunSink imports it itself
    except Exception as e:  # noqa: BLE001
        raise SystemExit("synth_fixtures: rerun-sdk is not importable (%s) but with_rrd=True. "
                         "Install rerun-sdk (the ingest dep, >=0.23) or pass --no-rrd. Refusing "
                         "to silently emit rrd-less 'capture' bundles." % e)
    spec = raw["spec"]
    rrd_path = os.path.join(run_dir, "k1_follow_%d.rrd" % spec["epoch"])
    sink = RerunSink(enabled=True, app_id="k1_follow", mode="save", path=rrd_path,
                     image_every_n=_IMG_EVERY, min_safe=_MIN_SAFE_M, standoff=_STANDOFF_M,
                     max_follow=_MAX_FOLLOW_M)
    if not sink.ok:
        raise SystemExit("synth_fixtures: RerunSink failed to open %s (see RERUN-FAULT above)"
                         % rrd_path)
    sink.refs_once()   # static min_safe/standoff/max reference lines, like a live capture run
    # Approximate pinhole exactly like the node's first framed tick (perception.pinhole_intrinsics:
    # fx from hfov, fy:=fx, centre principal point, approximate:true -- S3's intrinsics gate target).
    fx = (_W / 2.0) / math.tan(math.radians(_HFOV_DEG) / 2.0)
    sink.pinhole("/camera/rgb", _W, _H, fx, fx, _W / 2.0, _H / 2.0)
    sink.write_intrinsics({
        "model": "pinhole_from_hfov", "approximate": True,
        "width": _W, "height": _H, "hfov_deg": _HFOV_DEG,
        "fx": fx, "fy": fx, "cx": _W / 2.0, "cy": _H / 2.0,
        "note": "synthetic fixture (S6); mirrors robot/perception.pinhole_intrinsics fy:=fx",
    })
    for i, (fsm, _tid) in enumerate(raw["plan"]):
        sink.frame(i, spec["epoch"] + i / _FPS)          # dual timelines: frame_idx + wall
        if i in raw["images"]:
            # The sink COPIES + converts BGR->RGB; hand it BGR so the stored RGB == our canonical
            # array (and read_rrd()+assemble_episode() reproduces make_episodes() exactly).
            sink.image("/camera/rgb", raw["images"][i][:, :, ::-1], seq=i)
            sink.depth("/camera/depth", raw["depths"][i], seq=i)   # DepthImage(meter=1.0)
        sink.state("/fsm/state", fsm)   # numeric /fsm/state_id series + /fsm/state text, like live
        for ent in _RRD_SCALAR_ORDER:
            vals = raw["scalars"].get(ent)
            if vals is not None and i in vals:
                sink.scalar(ent, vals[i])
    sink.close()
    if sink._faults:   # the latch hid something -> the .rrd is silently incomplete: refuse
        raise SystemExit("synth_fixtures: RerunSink recorded %d fault(s) writing %s -- the .rrd "
                         "is not trustworthy; fix before consumers use it" % (sink._faults, rrd_path))
    # rerun streams the .rrd on a background thread; sink.close()'s flush() does NOT guarantee the file
    # is FULLY finalized in-process (a footer lands on recording drop). In production, offload_run.sh
    # hashes the .rrd AFTER the node process has exited, so the bytes are settled -- but this fixture
    # writer hashes it in-process for the manifest, so it must force a full finalize FIRST, or the
    # manifest sha won't match the settled file and batch_ingest's integrity check rejects every
    # fixture bundle. rerun_shutdown() drains + tears down all recordings; the next bundle re-inits.
    try:
        import rerun as _rr
        _rr.rerun_shutdown()
    except Exception:  # noqa: BLE001 -- best-effort finalize; the size/stability check below is the gate
        pass
    if not os.path.isfile(rrd_path) or os.path.getsize(rrd_path) == 0:
        raise SystemExit("synth_fixtures: %s missing/empty after close()" % rrd_path)


def _write_manifest(run_dir, raw):
    """manifest.json exactly as offload_run.sh's embedded python builds it: walk the bundle,
    sha256 every file EXCEPT manifest.json itself, json indent=2 sort_keys. dirs.sort() is added
    for cross-platform walk determinism (offload's single-subdir layout makes it a no-op there)."""
    spec = raw["spec"]
    files = []
    for root, dirs, names in os.walk(run_dir):
        dirs.sort()
        for n in sorted(names):
            if n == "manifest.json":
                continue
            p = os.path.join(root, n)
            files.append({"name": os.path.relpath(p, run_dir).replace(os.sep, "/"),
                          "bytes": os.path.getsize(p), "sha256": _sha256(p)})
    man = {"run_id": spec["run_id"], "created_utc": spec["stamp"], "git_version": _GIT,
           "profile": spec["profile"], "duration_s": raw["duration_s"],
           "file_count": len(files), "total_bytes": sum(f["bytes"] for f in files),
           "files": files}
    with open(os.path.join(run_dir, "manifest.json"), "w", newline="\n") as f:
        json.dump(man, f, indent=2, sort_keys=True)


def _label(run_dir, spec):
    """Label through the REAL eval/label_run.label_bundle (P5.1 scorer over the bundle's own
    k1_follow.err + build_oc from its config/CONFIG line) -- the outcome vocabulary is DERIVED from
    the shipped labeler, never enumerated here. Two follow-ups:
      * assert the outcome matches the planted expectation -- a mismatch means the fixture values
        or the scorer thresholds drifted, and MUST fail loudly, not ship a mislabeled fixture;
      * pin labeled_utc (label_run stamps wall-clock NOW -- the single nondeterministic byte in an
        otherwise seed-pure bundle) so same-seed regeneration is byte-identical."""
    got = label_run.label_bundle(run_dir)
    if got != spec["expect"]:
        raise SystemExit("synth_fixtures: planted %r bundle %s labeled %r by the real scorer "
                         "(%s) -- fixture values vs scorer thresholds drifted; fix before any "
                         "consumer trusts this fixture set"
                         % (spec["expect"], spec["run_id"], got, label_run.SCORER_VERSION))
    mp = os.path.join(run_dir, "manifest.json")
    with open(mp) as f:
        man = json.load(f)
    man["label"]["labeled_utc"] = _LABELED_UTC
    with open(mp, "w", newline="\n") as f:   # also normalizes label_run's platform newlines to LF
        json.dump(man, f, indent=2, sort_keys=True)


def make_bundles(out_dir, seed=0, with_rrd=True):
    """Pinned interface -- see module docstring. Writes runs/<run_id>/ bundles for every spec in
    BUNDLE_SPECS and returns the run_ids in spec order (== sorted ascending). Refuses to overwrite
    an existing bundle dir (mirror of S1's never-overwrite rule): regenerate into a fresh --out."""
    if with_rrd:
        try:
            import rerun  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise SystemExit("synth_fixtures: rerun-sdk is not importable (%s) but with_rrd=True. "
                             "Install rerun-sdk or pass --no-rrd / with_rrd=False. NOT silently "
                             "skipping the .rrd -- consumers must know what shape they got." % e)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    run_ids = []
    for spec in BUNDLE_SPECS:
        raw = _gen_raw(spec, seed)
        rd = os.path.join(out_dir, spec["run_id"])
        if os.path.exists(rd):
            raise SystemExit("synth_fixtures: %s already exists -- refusing to overwrite a bundle; "
                             "use a fresh --out dir" % rd)
        os.makedirs(os.path.join(rd, "config"))
        _write_text(os.path.join(rd, "config", "defaults.yaml"), _DEFAULTS_YAML)
        _write_text(os.path.join(rd, "events.jsonl"), "\n".join(raw["events"]) + "\n")
        _write_text(os.path.join(rd, "k1_follow.err"), "\n".join(raw["err"]) + "\n")
        if spec["rrd"] and with_rrd:
            _write_rrd(rd, raw)   # also drops intrinsics.json beside the .rrd (offload copies both)
        _write_manifest(rd, raw)  # AFTER all payload files exist -- hashes must match on-disk bytes
        if spec["labeled"]:
            _label(rd, spec)      # manifest.json is excluded from files[], so the label rewrite
                                  # never invalidates the recorded hashes (same as production flow)
        run_ids.append(spec["run_id"])
    return run_ids


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _episodes_equal(a, b):
    if a["run_id"] != b["run_id"] or a["frames"] != b["frames"] or a["meta"] != b["meta"]:
        return False
    if not (np.array_equal(a["state"], b["state"]) and np.array_equal(a["action"], b["action"])):
        return False
    for p, q in zip(a["rgb"], b["rgb"]):
        if (p is None) != (q is None):
            return False
        if p is not None and not np.array_equal(p, q):
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="target runs/ dir (each bundle -> <out>/<run_id>/)")
    ap.add_argument("--seed", type=int, default=0, help="determinism seed (default 0)")
    ap.add_argument("--no-rrd", action="store_true", dest="no_rrd",
                    help="write every bundle rrd-less (for environments without rerun-sdk)")
    a = ap.parse_args()

    # Inline determinism double-run: the same seed MUST reproduce identical episodes. Catches any
    # future edit that draws entropy outside default_rng (the exact failure S1's gate depends on).
    e1, e2 = make_episodes(a.seed), make_episodes(a.seed)
    for x, y in zip(e1, e2):
        if not _episodes_equal(x, y):
            raise SystemExit("synth_fixtures: DETERMINISM FAILURE on %s -- some code path draws "
                             "entropy outside numpy default_rng(seed)" % x["run_id"])

    run_ids = make_bundles(a.out, seed=a.seed, with_rrd=not a.no_rrd)
    for spec, rid in zip(BUNDLE_SPECS, run_ids):
        print("SYNTH %-13s %s labeled=%s rrd=%s"
              % (spec["kind"], rid, spec["labeled"], bool(spec["rrd"] and not a.no_rrd)))
    print("SYNTH-FIXTURES-OK n=%d seed=%d out=%s" % (len(run_ids), a.seed, os.path.abspath(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
