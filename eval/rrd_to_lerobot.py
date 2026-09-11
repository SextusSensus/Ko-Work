#!/usr/bin/env python3
"""rrd_to_lerobot.py -- Phase 4 (PIPELINE-PROOF ONLY): a K1-follow Rerun .rrd -> a LeRobot-v3-shaped
episode (observation.state + action + head_rgb video).

HONEST SCOPE (read RERUN_PLAN.md thesis-fit before pitching this):
  * This is an INTERNAL QA / labeling / pipeline-plumbing tool. It proves the .rrd -> LeRobot ingest
    path works end-to-end, de-risking the ingest for the REAL rig.
  * The follow episode is MONO head-RGB + (optional) flaky head-depth + a 2-DOF twist from a
    hand-written P-controller. It is NOT the dual-POV + LiDAR + skeletal thesis data on any axis.
    Do NOT present the output as "we capture training data" -- one DD question collapses that.
  * There is NO native rerun->LeRobot exporter in rerun 0.33 (checked). So we read the .rrd ourselves
    via the chunk-stream API and map it. When the `lerobot` library is importable this script hands
    off to its canonical writer (format-correct by construction); otherwise it writes a documented
    RAW layout (parquet + mp4 + meta json) that mirrors LeRobot v3 field-for-field for inspection.

Schema mapping (see LEROBOT_EXPORT.md):
  observation.state = [range_m, bearing_deg, rsrc_is_depth, anchor_sim, conf, depth_fps, fsm_state_id]
  action            = [vx, vyaw]                         (vy == 0, 2-DOF twist)
  observation.images.head_rgb  <- /camera/rgb            (Image -> mp4)
  (depth: /camera/depth IS read out of live .rrds, but depth EXPORT is not implemented yet --
   --with-depth exits with an error rather than silently dropping it; the .rrd stays the
   depth artifact of record)

Usage:
  python3 rrd_to_lerobot.py <in.rrd> --out <dir> [--fps 10] [--task "follow the locked person"]
                                     [--with-depth] [--repo-id local/k1_follow]
"""
import argparse, json, os, sys

# ---- entity -> feature mapping ---------------------------------------------
# observation.state components, in order, each an entity that logs rr.Scalars.
STATE_ENTITIES = [
    ("/follow/range",        "range_m"),
    ("/follow/bearing",      "bearing_deg"),
    ("/follow/range_source", "rsrc_is_depth"),   # 2=depth->1.0, else 0.0 (remapped below)
    ("/reid/sim",            "anchor_sim"),
    ("/track/conf",          "conf"),
    ("/health/depth_fps",    "depth_fps"),        # LIVE .rrd only; 0.0-filled for replay .rrds
    ("/fsm/state_id",        "fsm_state_id"),
]
ACTION_ENTITIES = [("/cmd/vx", "vx"), ("/cmd/vyaw", "vyaw")]
RGB_ENTITY = "/camera/rgb"
DEPTH_ENTITY = "/camera/depth"


def _load_store(path):
    from rerun.experimental import RrdReader
    return RrdReader(path).stream().collect()


def _col(rb, name):
    i = rb.schema.get_field_index(name)
    return rb.column(i) if i >= 0 else None


def _read_rrd_dataframe(path, depth_only=False):
    """read_rrd for rerun 0.23.x, which has no rerun.experimental.RrdReader.

    Returns the SAME (scalars, images, depth) structure as read_rrd. The GPU analysis venv pins
    rerun-sdk 0.23.1 to match the writer on the robot, and this is how that venv reads recordings.
    Pixels are sliced straight out of Arrow rather than round-tripped through Python lists.
    """
    import os as _os
    import sys as _sys
    import numpy as np
    import rerun as rr
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from depth_replay import _first_col, _image_cells, iter_depth_batches

    rec = rr.dataframe.load_recording(path)
    cols = [(str(c.entity_path),
             str(getattr(c, "component", None) or getattr(c, "component_name", "")))
            for c in rec.schema().component_columns()]
    ents = {e for e, _c in cols}

    scalars = {}
    for ent in sorted({e for e, c in cols if c.endswith("Scalar")}):
        try:
            tbl = rec.view(index="frame_idx", contents={ent: ["Scalar"]}).select().read_all()
        except Exception:  # noqa: BLE001 -- one odd entity must not sink the whole read
            continue
        ci, vc = _first_col(tbl, "frame_idx"), _first_col(tbl, "Scalar")
        if not ci or not vc:
            continue
        d = scalars.setdefault(ent, {})
        for fi, v in zip(tbl.column(ci).to_pylist(), tbl.column(vc).to_pylist()):
            if fi is None or v is None:
                continue
            if isinstance(v, (list, tuple)):
                if not v:
                    continue
                v = v[0]
            d[int(fi)] = float(v)

    images = {}
    if not depth_only and RGB_ENTITY in ents:
        tbl = rec.view(index="frame_idx",
                       contents={RGB_ENTITY: ["ImageBuffer", "ImageFormat"]}).select().read_all()
        ci = _first_col(tbl, "frame_idx")
        cb = _first_col(tbl, "ImageBuffer")
        cf = _first_col(tbl, "ImageFormat")
        if ci and cb:
            fmts = tbl.column(cf).to_pylist() if cf else []
            last = None
            for i, (fi, cell) in enumerate(zip(tbl.column(ci).to_pylist(),
                                               _image_cells(tbl.column(cb)))):
                f = fmts[i] if i < len(fmts) else None
                if f:
                    last = f[0] if isinstance(f, list) and f else f
                if fi is None or cell is None or not last:
                    continue
                w, h = int(last["width"]), int(last["height"])
                if w * h == 0:
                    continue
                c = max(1, cell.size // (w * h))
                img = cell[: w * h * c].reshape(h, w, c)
                if c == 1:
                    img = np.repeat(img, 3, axis=2)
                images[int(fi)] = np.ascontiguousarray(img[:, :, :3])

    depth = {}
    if DEPTH_ENTITY in ents:
        for batch in iter_depth_batches(rec, 1 << 30):
            for fi, img in batch:
                depth[fi] = img
    return scalars, images, depth


def read_rrd(path, depth_only=False):
    """Return (scalars, images, depth) where scalars[entity] = {frame_idx: value},
    images = {frame_idx: HxWx3 uint8}, depth = {frame_idx: HxW float32}.
    depth_only=True skips the RGB decode entirely (the ingest depth-sanity path never uses RGB;
    decoding hundreds of full frames it throws away is a needless OOM risk on a long capture)."""
    try:
        from rerun.experimental import RrdReader  # noqa: F401 -- newer chunk-stream API
    except ImportError:
        # rerun 0.23.x (the version that matches the robot's writer) has no RrdReader; read the
        # same recording through the dataframe API instead. Same return structure.
        return _read_rrd_dataframe(path, depth_only)
    import numpy as np
    store = _load_store(path)
    scalars = {}
    images = {}
    depth = {}
    for ch in store.stream():
        if ch.is_static or ch.is_empty:
            continue
        ep = str(ch.entity_path)
        rb = ch.to_record_batch()
        if "frame_idx" not in rb.schema.names:
            continue
        fidx = _col(rb, "frame_idx").to_pylist()

        sc = _col(rb, "Scalars:scalars")
        if sc is not None:
            vals = sc.to_pylist()
            d = scalars.setdefault(ep, {})
            for fi, v in zip(fidx, vals):
                if fi is None or v is None:
                    continue
                d[int(fi)] = float(v[0]) if isinstance(v, (list, tuple)) else float(v)
            continue

        buf = _col(rb, "Image:buffer")
        fmt = _col(rb, "Image:format")
        if buf is not None and fmt is not None:
            if depth_only:
                continue                             # RGB not needed -> skip the decode (OOM guard)
            bl, fl = buf.to_pylist(), fmt.to_pylist()
            for fi, b, f in zip(fidx, bl, fl):
                if fi is None or not b or not f:
                    continue
                raw = b[0] if (isinstance(b, list) and b and isinstance(b[0], list)) else b
                meta = f[0] if isinstance(f, list) else f
                w, h = int(meta["width"]), int(meta["height"])
                arr = np.frombuffer(bytes(bytearray(raw)), dtype=np.uint8)
                if w * h == 0:
                    continue
                c = max(1, arr.size // (w * h))
                img = arr[: w * h * c].reshape(h, w, c)
                if ep == RGB_ENTITY:
                    if c == 1:
                        img = np.repeat(img, 3, axis=2)
                    images[int(fi)] = np.ascontiguousarray(img[:, :, :3])
            continue

        # DepthImage lands as a separate blob archetype ("DepthImage:buffer"); best-effort
        dbuf = _col(rb, "DepthImage:buffer")
        dfmt = _col(rb, "DepthImage:format")
        if dbuf is not None and dfmt is not None:
            bl, fl = dbuf.to_pylist(), dfmt.to_pylist()
            for fi, b, f in zip(fidx, bl, fl):
                if fi is None or not b or not f:
                    continue
                raw = b[0] if (isinstance(b, list) and b and isinstance(b[0], list)) else b
                meta = f[0] if isinstance(f, list) else f
                w, h = int(meta["width"]), int(meta["height"])
                arr = np.frombuffer(bytes(bytearray(raw)), dtype=np.float32)
                if w * h and arr.size >= w * h:
                    depth[int(fi)] = arr[: w * h].reshape(h, w)
    return scalars, images, depth


def _wall_secs(v):
    """A rerun 'wall' timeline cell -> float epoch seconds. Rerun stores it as a timestamp; to_pylist()
    yields a datetime (tz-aware) or an int (nanoseconds) depending on version -- handle both."""
    if v is None:
        return None
    if hasattr(v, "timestamp"):          # datetime
        return float(v.timestamp())
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return iv / 1e9 if iv > 1_000_000_000_000 else float(iv)   # ns since epoch -> s (heuristic)


def read_rrd_frames(path):
    """Richer decode for the P6G.2 geometry stages (desktop/recon/geom.py): per-frame WALL timestamps
    for RGB and depth, plus the /camera/rgb/target Boxes2D. The geometry stages pair RGBD by WALL time
    (RECON_CONTRACT doctrine 4 -- depth's frame_idx is best-effort), which read_rrd's frame_idx-only view
    cannot support; and they mask the followed operator using the target box (doctrine 5). Kept SEPARATE
    from read_rrd (whose 3-tuple signature has many callers).

    Returns (rgb_stream, depth_stream) where
      rgb_stream   = [(wall_t, HxWx3 uint8, box_or_None)]   box = (x0,y0,x1,y1) px from /camera/rgb/target
      depth_stream = [(wall_t, HxW float32 metres)]
    both sorted by wall time. A run that logged no target box yields box=None on every RGB frame (masking
    then no-ops) -- the box path is VERIFY ON DESKTOP (synth fixtures don't log it)."""
    import numpy as np
    store = _load_store(path)
    rgb, depth, boxes_by_fi = [], [], {}
    for ch in store.stream():
        if ch.is_static or ch.is_empty:
            continue
        ep = str(ch.entity_path)
        rb = ch.to_record_batch()
        names = rb.schema.names
        if "frame_idx" not in names:
            continue
        fidx = _col(rb, "frame_idx").to_pylist()
        wallc = _col(rb, "wall")
        wall = wallc.to_pylist() if wallc is not None else [None] * len(fidx)

        if ep == RGB_ENTITY:
            buf, fmt = _col(rb, "Image:buffer"), _col(rb, "Image:format")
            if buf is None or fmt is None:
                continue
            for fi, wv, b, f in zip(fidx, wall, buf.to_pylist(), fmt.to_pylist()):
                if not b or not f:
                    continue
                raw = b[0] if (isinstance(b, list) and b and isinstance(b[0], list)) else b
                meta = f[0] if isinstance(f, list) else f
                w, h = int(meta["width"]), int(meta["height"])
                if w * h == 0:
                    continue
                arr = np.frombuffer(bytes(bytearray(raw)), dtype=np.uint8)
                c = max(1, arr.size // (w * h))
                img = arr[: w * h * c].reshape(h, w, c)
                if c == 1:
                    img = np.repeat(img, 3, axis=2)
                rgb.append([_wall_secs(wv), np.ascontiguousarray(img[:, :, :3]), int(fi) if fi is not None else None])
        elif ep == DEPTH_ENTITY:
            dbuf, dfmt = _col(rb, "DepthImage:buffer"), _col(rb, "DepthImage:format")
            if dbuf is None or dfmt is None:
                continue
            for fi, wv, b, f in zip(fidx, wall, dbuf.to_pylist(), dfmt.to_pylist()):
                if not b or not f:
                    continue
                raw = b[0] if (isinstance(b, list) and b and isinstance(b[0], list)) else b
                meta = f[0] if isinstance(f, list) else f
                w, h = int(meta["width"]), int(meta["height"])
                if w * h == 0:
                    continue
                arr = np.frombuffer(bytes(bytearray(raw)), dtype=np.float32)
                if arr.size >= w * h:
                    depth.append((_wall_secs(wv), arr[: w * h].reshape(h, w)))
        elif ep == "/camera/rgb/target":
            box = _boxes2d_row(rb)          # (x0,y0,x1,y1) per frame_idx, or {}
            for fi, bx in zip(fidx, box):
                if fi is not None and bx is not None:
                    boxes_by_fi[int(fi)] = bx

    # attach the target box to each RGB frame by its frame_idx (best-effort; None if absent)
    rgb_stream = [(t, img, boxes_by_fi.get(fi)) for (t, img, fi) in rgb if t is not None]
    rgb_stream.sort(key=lambda r: r[0])
    depth_stream = sorted([(t, d) for (t, d) in depth if t is not None], key=lambda r: r[0])
    return rgb_stream, depth_stream


def _boxes2d_row(rb):
    """Decode a /camera/rgb/target Boxes2D chunk -> list of (x0,y0,x1,y1) aligned to frame_idx rows.
    The sink logs a single box via Boxes2D(mins=, sizes=); rerun stores it as centers+half_sizes. Try
    both column spellings (VERIFY ON DESKTOP -- synth fixtures don't log this entity)."""
    cen, half = _col(rb, "Boxes2D:centers"), _col(rb, "Boxes2D:half_sizes")
    if cen is not None and half is not None:
        cl, hl = cen.to_pylist(), half.to_pylist()
        out = []
        for c, hh in zip(cl, hl):
            if not c or not hh:
                out.append(None); continue
            cx, cy = c[0]; hx, hy = hh[0]
            out.append((cx - hx, cy - hy, cx + hx, cy + hy))
        return out
    mins, sizes = _col(rb, "Boxes2D:mins"), _col(rb, "Boxes2D:sizes")
    if mins is not None and sizes is not None:
        ml, sl = mins.to_pylist(), sizes.to_pylist()
        out = []
        for m, s in zip(ml, sl):
            if not m or not s:
                out.append(None); continue
            x0, y0 = m[0]; sw, sh = s[0]
            out.append((x0, y0, x0 + sw, y0 + sh))
        return out
    return [None] * len(_col(rb, "frame_idx").to_pylist())


def _ffill(series_by_fi, frames):
    """Forward-fill a {frame_idx: value} map onto the sorted `frames` list (0.0 before first)."""
    out, last = [], 0.0
    for fi in frames:
        if fi in series_by_fi:
            last = series_by_fi[fi]
        out.append(last)
    return out


def assemble_episode(scalars, images, allow_no_track=False):
    """Build the aligned per-frame (state, action, rgb) episode on the union of scalar frame_idx.
    RGB frames are decimated in the .rrd (1/N), so each state frame takes the NEAREST earlier image.

    Review fixes 2026-07-04:
      * The episode is TRIMMED to start at the first frame with real follow data (/follow/range) --
        _ffill's 0.0 seed would otherwise FABRICATE range=0.0/sim=0.0/conf=0.0 rows for every
        pre-lock SEARCH frame (range 0.0 m reads as "AT the robot") and pollute stats.json. Pass
        allow_no_track=True to export a TRACK-less .rrd anyway (health/FSM QA only).
      * An image is only paired with a frame it precedes-or-equals -- pre-first-image frames get
        None, never a FUTURE image (temporal causality for anything training-shaped).
    Council 2026-09-11 #2:
      * Robot now emits /follow/range EVERY tick (finite when locked, NaN when unlocked). Keep
        only finite-range frames so unlocked NaNs cannot enter training via _ffill, while old
        TRACK-only recordings (all range samples finite) stay byte-compatible."""
    import math
    import numpy as np
    frames = sorted({fi for e in scalars for fi in scalars[e]})
    if not frames:
        raise SystemExit("no scalar frames in the .rrd -- was it recorded with --rerun on a real run?")

    def _finite(v):
        try:
            return v is not None and math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    rng_fis = scalars.get("/follow/range", {})
    finite_rng = {fi: v for fi, v in rng_fis.items() if _finite(v)}
    if finite_rng:
        anchor = min(finite_rng)
        # Drop pre-lock AND unlocked (NaN) ticks -- train only on real lock observations.
        frames = [fi for fi in frames if fi >= anchor and fi in finite_rng]
    elif not allow_no_track:
        raise SystemExit("no TRACK frames (/follow/range) in the .rrd -- nothing to export as an "
                         "episode. Re-run with a locked follow, or pass --allow-no-track for a "
                         "health/FSM-only export (state cols will be 0-filled).")

    state_cols = []
    for ent, _name in STATE_ENTITIES:
        col = _ffill(scalars.get(ent, {}), frames)
        if ent == "/follow/range_source":            # 2=depth -> 1.0 (is-depth), else 0.0
            col = [1.0 if abs(v - 2.0) < 0.5 else 0.0 for v in col]
        state_cols.append(col)
    state = np.array(state_cols, dtype=np.float32).T          # (T, 7)

    action = np.array([_ffill(scalars.get(e, {}), frames) for e, _ in ACTION_ENTITIES],
                      dtype=np.float32).T                     # (T, 2)

    # nearest-EARLIER RGB for each state frame (never a future image; None before the first image)
    img_fis = sorted(images)
    rgb = []
    j = 0
    for fi in frames:
        while j + 1 < len(img_fis) and img_fis[j + 1] <= fi:
            j += 1
        rgb.append(images[img_fis[j]] if (img_fis and img_fis[j] <= fi) else None)
    return frames, state, action, rgb


# ---- writers ---------------------------------------------------------------
def _stats(arr):
    import numpy as np
    a = np.asarray(arr, dtype=np.float64)
    return {"min": a.min(0).tolist(), "max": a.max(0).tolist(),
            "mean": a.mean(0).tolist(), "std": a.std(0).tolist(),
            "count": [int(a.shape[0])]}


def write_raw(out, frames, state, action, rgb, fps, task, repo_id, with_depth):
    """Documented RAW LeRobot-v3-shaped layout (used when the `lerobot` lib is absent). Field names
    and directory shape mirror LeRobot v3; use the lerobot library for a loader-conformant dataset."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    os.makedirs(os.path.join(out, "meta"), exist_ok=True)
    os.makedirs(os.path.join(out, "data", "chunk-000"), exist_ok=True)
    vid_dir = os.path.join(out, "videos", "chunk-000", "observation.images.head_rgb")
    os.makedirs(vid_dir, exist_ok=True)

    T = len(frames)
    timestamp = [i / float(fps) for i in range(T)]
    table = pa.table({
        "observation.state": pa.array(state.tolist(), type=pa.list_(pa.float32())),
        "action":            pa.array(action.tolist(), type=pa.list_(pa.float32())),
        "timestamp":         pa.array(timestamp, type=pa.float32()),
        "frame_index":       pa.array(list(range(T)), type=pa.int64()),
        "episode_index":     pa.array([0] * T, type=pa.int64()),
        "index":             pa.array(list(range(T)), type=pa.int64()),
        "task_index":        pa.array([0] * T, type=pa.int64()),
    })
    pq.write_table(table, os.path.join(out, "data", "chunk-000", "episode_000000.parquet"))

    # RGB video (imageio+ffmpeg if available, else a PNG folder fallback + note)
    video_written = _write_video(os.path.join(vid_dir, "episode_000000.mp4"), rgb, fps)

    h = w = 0
    for im in rgb:
        if im is not None:
            h, w = int(im.shape[0]), int(im.shape[1]); break

    info = {
        "codebase_version": "v3.0",
        "note": "INTERNAL QA/pipeline-proof export from a K1 follow Rerun .rrd. NOT dual-POV/LiDAR/"
                "skeletal thesis data. RAW layout -- use the lerobot library for a loader-conformant "
                "dataset. See LEROBOT_EXPORT.md.",
        "repo_id": repo_id,
        "robot_type": "booster_k1_follow",
        "fps": fps,
        "total_episodes": 1,
        "total_frames": T,
        "total_tasks": 1,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [state.shape[1]],
                                  "names": [n for _, n in STATE_ENTITIES]},
            "action": {"dtype": "float32", "shape": [action.shape[1]],
                       "names": [n for _, n in ACTION_ENTITIES]},
            "observation.images.head_rgb": {"dtype": "video", "shape": [h, w, 3],
                                            "names": ["height", "width", "channel"],
                                            "video_info": {"video.fps": fps, "video.codec": "h264"}},
            "timestamp": {"dtype": "float32", "shape": [1]},
            "frame_index": {"dtype": "int64", "shape": [1]},
            "episode_index": {"dtype": "int64", "shape": [1]},
            "index": {"dtype": "int64", "shape": [1]},
            "task_index": {"dtype": "int64", "shape": [1]},
        },
        "video_backend": ("mp4" if video_written else "png-fallback (mp4 encode unavailable or failed)"),
    }
    with open(os.path.join(out, "meta", "info.json"), "w") as f:
        json.dump(info, f, indent=2)
    with open(os.path.join(out, "meta", "tasks.jsonl"), "w") as f:
        f.write(json.dumps({"task_index": 0, "task": task}) + "\n")
    with open(os.path.join(out, "meta", "episodes.jsonl"), "w") as f:
        f.write(json.dumps({"episode_index": 0, "tasks": [task], "length": T}) + "\n")
    with open(os.path.join(out, "meta", "stats.json"), "w") as f:
        json.dump({"observation.state": _stats(state), "action": _stats(action)}, f, indent=2)
    return info


def _write_video(path, rgb, fps):
    frames = [im for im in rgb if im is not None]
    if not frames:
        return False
    try:
        import imageio
        # Force the FFMPEG plugin so a missing ffmpeg RAISES here (-> PNG fallback) instead of
        # imageio silently picking an image writer (e.g. tiff) that can't do video.
        with imageio.get_writer(path, format="FFMPEG", fps=int(max(1, fps)),
                                macro_block_size=1, codec="libx264") as w:
            for im in rgb:
                if im is not None:
                    w.append_data(im)
        return True
    except Exception as e:  # noqa: BLE001 -- ffmpeg absent/failed -> PNG folder fallback (still inspectable)
        print("RRD2LR video note: mp4 encode failed (%s) -> writing PNG frames" % e)
        import os as _os
        try:  # review fix: never leave a partial mp4 at the canonical video_path -- a downstream
            if _os.path.exists(path):  # loader would resolve the template, find it, and die on decode
                _os.remove(path)
        except OSError:
            pass
        try:
            import imageio
            d = path[:-4] + "_frames"
            _os.makedirs(d, exist_ok=True)
            for i, im in enumerate(rgb):
                if im is not None:
                    imageio.imwrite(_os.path.join(d, "f%06d.png" % i), im)
            return False
        except Exception as e2:  # noqa: BLE001
            print("RRD2LR video note: PNG fallback also failed: %s" % e2)
            return False


def try_write_lerobot(out, frames, state, action, rgb, fps, task, repo_id):
    """Canonical path: hand off to the lerobot library when importable (format-correct by
    construction). Returns True on success, False if lerobot is unavailable (caller uses write_raw)."""
    try:
        from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
    except Exception:
        return False
    import numpy as np
    # Review fix: all-None rgb would declare a (0,0,3) video feature no frame ever supplies ->
    # fall back to write_raw (which handles imageless episodes). Trim any image-less prefix so
    # every retained frame HAS an image and the shape comes from a real one.
    if not any(im is not None for im in rgb):
        return False
    first = next(i for i, im in enumerate(rgb) if im is not None)
    frames, state, action, rgb = frames[first:], state[first:], action[first:], rgb[first:]
    features = {
        "observation.state": {"dtype": "float32", "shape": (state.shape[1],),
                              "names": [n for _, n in STATE_ENTITIES]},
        "action": {"dtype": "float32", "shape": (action.shape[1],),
                   "names": [n for _, n in ACTION_ENTITIES]},
        "observation.images.head_rgb": {"dtype": "video", "shape": tuple(rgb[0].shape),
                                        "names": ["height", "width", "channel"]},
    }
    ds = LeRobotDataset.create(repo_id=repo_id, fps=int(fps), root=out, features=features)
    for i in range(len(frames)):
        frame = {"observation.state": state[i], "action": action[i]}
        if rgb[i] is not None:
            frame["observation.images.head_rgb"] = rgb[i]
        ds.add_frame(frame, task=task)
    ds.save_episode()
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rrd", help="input .rrd (from --rerun or replay_eval --rerun)")
    ap.add_argument("--out", required=True, help="output dataset directory")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--task", default="follow the locked person")
    ap.add_argument("--repo-id", default="local/k1_follow")
    ap.add_argument("--with-depth", action="store_true",
                    help="NOT IMPLEMENTED: depth frames are read but a head_depth export does not "
                         "exist yet -- this flag exits with an error instead of silently no-opping")
    ap.add_argument("--allow-no-track", action="store_true",
                    help="export a TRACK-less .rrd anyway (health/FSM QA only; follow state cols "
                         "are 0-filled). Default: refuse, so fabricated range=0.0 rows can't ship")
    ap.add_argument("--raw", action="store_true", help="force the RAW layout (skip the lerobot lib)")
    a = ap.parse_args()

    if a.with_depth:  # review fix: this was a silently-dead flag; fail loudly until implemented
        raise SystemExit("--with-depth: depth export not implemented yet (depth frames are read "
                         "but not written; the .rrd is the depth artifact of record). "
                         "Re-run without it.")
    if not os.path.exists(a.rrd):
        raise SystemExit("no such .rrd: %s" % a.rrd)
    print("reading %s ..." % a.rrd)
    scalars, images, depth = read_rrd(a.rrd)
    print("  entities: %d scalar, %d rgb frames, %d depth frames"
          % (len(scalars), len(images), len(depth)))
    frames, state, action, rgb = assemble_episode(scalars, images, allow_no_track=a.allow_no_track)
    n_rgb = sum(1 for im in rgb if im is not None)
    print("  episode: T=%d frames, state%s, action%s, rgb=%d/%d"
          % (len(frames), tuple(state.shape), tuple(action.shape), n_rgb, len(frames)))

    if not a.raw and try_write_lerobot(a.out, frames, state, action, rgb, a.fps, a.task, a.repo_id):
        print("WROTE canonical LeRobot dataset (lerobot lib) -> %s" % a.out)
        return 0
    info = write_raw(a.out, frames, state, action, rgb, a.fps, a.task, a.repo_id, a.with_depth)
    print("WROTE raw LeRobot-v3-shaped dataset -> %s  (video_backend=%s)"
          % (a.out, info["video_backend"]))
    print("NOTE: internal QA/pipeline-proof artifact -- NOT thesis training data (see LEROBOT_EXPORT.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
