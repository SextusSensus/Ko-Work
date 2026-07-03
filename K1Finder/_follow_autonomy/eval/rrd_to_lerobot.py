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
  (observation.images.head_depth <- /camera/depth is available in LIVE .rrds; --with-depth to include)

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


def read_rrd(path):
    """Return (scalars, images, depth) where scalars[entity] = {frame_idx: value},
    images = {frame_idx: HxWx3 uint8}, depth = {frame_idx: HxW float32}."""
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


def _ffill(series_by_fi, frames):
    """Forward-fill a {frame_idx: value} map onto the sorted `frames` list (0.0 before first)."""
    out, last = [], 0.0
    for fi in frames:
        if fi in series_by_fi:
            last = series_by_fi[fi]
        out.append(last)
    return out


def assemble_episode(scalars, images):
    """Build the aligned per-frame (state, action, rgb) episode on the union of scalar frame_idx.
    RGB frames are decimated in the .rrd (1/N), so each state frame takes the NEAREST earlier image."""
    import numpy as np
    frames = sorted({fi for e in scalars for fi in scalars[e]})
    if not frames:
        raise SystemExit("no scalar frames in the .rrd -- was it recorded with --rerun on a real run?")

    state_cols = []
    for ent, _name in STATE_ENTITIES:
        col = _ffill(scalars.get(ent, {}), frames)
        if ent == "/follow/range_source":            # 2=depth -> 1.0 (is-depth), else 0.0
            col = [1.0 if abs(v - 2.0) < 0.5 else 0.0 for v in col]
        state_cols.append(col)
    state = np.array(state_cols, dtype=np.float32).T          # (T, 7)

    action = np.array([_ffill(scalars.get(e, {}), frames) for e, _ in ACTION_ENTITIES],
                      dtype=np.float32).T                     # (T, 2)

    # nearest-earlier RGB for each state frame
    img_fis = sorted(images)
    rgb = []
    j = 0
    for fi in frames:
        while j + 1 < len(img_fis) and img_fis[j + 1] <= fi:
            j += 1
        rgb.append(images[img_fis[j]] if img_fis else None)
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
        "video_backend": ("mp4" if video_written else "png-fallback (ffmpeg not found)"),
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
    except Exception as e:  # noqa: BLE001 -- ffmpeg absent -> PNG folder fallback (still inspectable)
        print("RRD2LR video note: mp4 encode failed (%s) -> writing PNG frames" % e)
        try:
            import imageio, os as _os
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
    features = {
        "observation.state": {"dtype": "float32", "shape": (state.shape[1],),
                              "names": [n for _, n in STATE_ENTITIES]},
        "action": {"dtype": "float32", "shape": (action.shape[1],),
                   "names": [n for _, n in ACTION_ENTITIES]},
        "observation.images.head_rgb": {"dtype": "video", "shape": (rgb[0].shape if rgb and rgb[0] is not None else (0, 0, 3)),
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
    ap.add_argument("--with-depth", action="store_true", help="also export head_depth if present")
    ap.add_argument("--raw", action="store_true", help="force the RAW layout (skip the lerobot lib)")
    a = ap.parse_args()

    if not os.path.exists(a.rrd):
        raise SystemExit("no such .rrd: %s" % a.rrd)
    print("reading %s ..." % a.rrd)
    scalars, images, depth = read_rrd(a.rrd)
    print("  entities: %d scalar, %d rgb frames, %d depth frames"
          % (len(scalars), len(images), len(depth)))
    frames, state, action, rgb = assemble_episode(scalars, images)
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
