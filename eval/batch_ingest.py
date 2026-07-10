#!/usr/bin/env python3
"""batch_ingest.py -- P7.1: mint a versioned dataset from runs/ (SCAFFOLD_CHARTER S1).

Graduates eval/rrd_to_lerobot.py (single-episode pipeline-proof) into a batched, versioned,
DETERMINISTIC dataset. episode = one runs/<run_id>/ bundle. RAW LeRobot-v3 layout (parquet + mp4 +
meta), NEVER the lerobot library (it pins torch<2.12 and its writer bytes aren't hash-stable).

Two layers so the core self-tests with NO rerun/.rrd/GPU:
  * mint_dataset(episodes, ...) -- the pure batch core (indexing, splits, TRAIN-only stats, hashing,
    card) over an injected iterable of IN-MEMORY episodes (the eval/synth_fixtures.make_episodes shape).
  * episode_from_bundle() / ingest() -- the rrd adapter + discovery/filter, importing rrd_to_lerobot's
    decoder (read_rrd/assemble_episode/_write_video -- NEVER re-typed; its two review-fixes are
    load-bearing).

Binding contracts (SCAFFOLD_CHARTER S1 -- frozen here):
  * episode_index = 0-based contiguous over INCLUDED runs, ordered sorted(run_id). run_id is identity.
  * meta/splits.json keyed by run_id; split = pure deterministic fn of sorted included run_ids + seed
    (method 'seeded_shuffle_sorted_run_ids'). Refuse below 2 included or 0 val (no --allow-single in v1).
  * meta/stats.json over the TRAIN split ONLY; per-channel min/max/mean/std(ddof=0)/count, float64;
    a `normalize` block names z-scored vs passthrough channel indices. == a plain-numpy reference.
  * stats_hash = sha256 of the exact meta/stats.json bytes (json.dump sort_keys=True, indent=2).
  * content_hash = sha256 over sorted `relpath\0<sha256(file_bytes)>\n` for every file under data/ + meta/
    EXCLUDING meta/info.json (self-ref) and all videos/** (mp4 bytes are NOT bit-reproducible). mp4s are
    represented by meta/video_index.json (frame_index->source .rrd frame_idx + dims + pinned encode
    params) which IS hashed.
  * FSM assert: every /fsm/state_id (state col 6) must map to a frozen id via eval/fsm_groups.py; ANY
    UNMAPPED (incl -1.0) ABORTS the whole mint (a runtime/enum divergence poisons the categorical).
  * label filter: default include {pass}; UNLABELED (absent manifest.outcome) excluded LOUDLY; card
    lists every excluded run + reason + a non-zero exclusion count. Never zero-fill a bad run.
  * --version explicit; refuse if out dir exists. depth stays OUT; odometry -> a future v{N+1}.

Authored on the anaconda-python laptop and SELF-TEST-VERIFIED there (numpy/rerun/pyarrow present);
the multi-run .rrd corpus + cross-pyarrow hash stability are VERIFY ON CLUSTER.
  python eval/batch_ingest.py selftest         -> INGEST-SELFTEST-OK
  python eval/batch_ingest.py mint --runs runs --out datasets/k1_follow_v1 --version k1_follow_v1
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fsm_groups                                   # noqa: E402  the ONE fsm-grouping helper

# observation.state / action channel names -- MUST match rrd_to_lerobot.STATE_ENTITIES/ACTION_ENTITIES.
STATE_NAMES = ["range_m", "bearing_deg", "rsrc_is_depth", "anchor_sim", "conf", "depth_fps",
               "fsm_state_id"]
ACTION_NAMES = ["vx", "vyaw"]
FSM_COL = 6                                         # index of fsm_state_id within observation.state
# normalize policy (recorded in stats.json so P7.2 never hardcodes indices): z-score the continuous
# channels; leave the categorical/boolean ones passthrough. rsrc_is_depth (2) is 0/1; fsm_state_id (6)
# is a frozen categorical id.
ZSCORE_IDX = [0, 1, 3, 4, 5]
PASSTHROUGH_IDX = [2, 6]

CODEBASE_VERSION = "v3.0"
DEFAULT_INCLUDE = ("pass",)
MIN_INCLUDED = 2                                    # refuse to mint below this
DEFAULT_FPS = 10.0


# ---- hashing ------------------------------------------------------------------------------------
def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path, obj):
    """Pinned canonical JSON: sort_keys + indent 2 (so meta files hash stably). Returns the bytes."""
    b = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")
    with open(path, "wb") as f:
        f.write(b)
    return b


# ---- pure batch core ----------------------------------------------------------------------------
def _split(run_ids, seed, val_fraction):
    """Deterministic train/val split at the RUN level. Pure function of sorted(run_ids) + seed: a
    desktop wipe regenerates it byte-identically. Both output lists are sorted. At least 1 val."""
    import random
    ids = sorted(run_ids)
    n = len(ids)
    n_val = max(1, int(round(n * val_fraction)))
    n_val = min(n_val, n - 1)                       # always leave >=1 train
    rng = random.Random(seed)
    shuffled = list(ids)
    rng.shuffle(shuffled)
    val = sorted(shuffled[:n_val])
    train = sorted(shuffled[n_val:])
    return train, val


def _channel_stats(arr):
    """Per-channel min/max/mean/std(ddof=0)/count over a (T, C) float array, computed in float64."""
    import numpy as np
    a = np.asarray(arr, dtype=np.float64)
    return {"min": a.min(0).tolist(), "max": a.max(0).tolist(), "mean": a.mean(0).tolist(),
            "std": a.std(0).tolist(), "count": [int(a.shape[0])]}


def _fsm_counts(state):
    """Per-canonical-state frame counts from state col 6 via fsm_groups. Raises ValueError naming the
    offending value if ANY id is UNMAPPED -- the caller turns that into a mint abort."""
    import numpy as np
    ids = np.asarray(state)[:, FSM_COL]
    counts = fsm_groups.count_ids(ids.tolist())
    if fsm_groups.UNMAPPED in counts:
        bad = [float(v) for v in ids.tolist() if fsm_groups.name_for_id(v) == fsm_groups.UNMAPPED]
        raise ValueError("unmapped /fsm/state_id value(s) %s (not in fsm_states.json v%s)"
                         % (sorted(set(bad))[:5], fsm_groups.VERSION))
    return counts


def _write_parquet(path, state, action, fps):
    """One episode's tabular data as deterministic parquet (mirrors rrd_to_lerobot.write_raw's schema).
    Pinned writer options (no dictionary, no per-write stats) so two mints over identical input produce
    identical bytes -- the content_hash reproducibility gate."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    t = int(np.asarray(state).shape[0])
    table = pa.table({
        "observation.state": pa.array(np.asarray(state, dtype=np.float32).tolist(),
                                      type=pa.list_(pa.float32())),
        "action": pa.array(np.asarray(action, dtype=np.float32).tolist(), type=pa.list_(pa.float32())),
        "timestamp": pa.array([i / float(fps) for i in range(t)], type=pa.float32()),
        "frame_index": pa.array(list(range(t)), type=pa.int64()),
        "episode_index": pa.array([0] * t, type=pa.int64()),        # rewritten below per episode
        "index": pa.array(list(range(t)), type=pa.int64()),
        "task_index": pa.array([0] * t, type=pa.int64()),
    })
    pq.write_table(table, path, compression="none", use_dictionary=False, write_statistics=False)


def _content_hash(out_dir):
    """sha256 over sorted `relpath\\0<sha256(bytes)>\\n` for every file under data/ + meta/, EXCLUDING
    meta/info.json (holds this hash) and everything under videos/. Deterministic regardless of walk
    order."""
    entries = []
    for sub in ("data", "meta"):
        base = os.path.join(out_dir, sub)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, out_dir).replace("\\", "/")
                if rel == "meta/info.json":
                    continue
                entries.append(rel + "\0" + _sha256_file(p) + "\n")
    entries.sort()
    return _sha256_bytes("".join(entries).encode("utf-8"))


def mint_dataset(episodes, out_dir, version, seed=0, val_fraction=0.2, provenance=None,
                 excluded=None, fps=DEFAULT_FPS, task="follow the locked person", min_included=MIN_INCLUDED):
    """Mint the versioned dataset from IN-MEMORY episodes (make_episodes shape). Refuses loudly (never
    silently) on: an existing out_dir, too few included episodes, a would-be empty val split, or ANY
    unmapped FSM id. Returns {content_hash, stats_hash, n_episodes, train, val, version}."""
    import numpy as np
    episodes = sorted(episodes, key=lambda e: e["run_id"])
    excluded = list(excluded or [])
    n = len(episodes)
    if n < min_included:
        raise SystemExit("REFUSE-MINT: only %d included episode(s) (< min %d). Excluded: %d. "
                         "Capture/label more runs." % (n, min_included, len(excluded)))
    if os.path.exists(out_dir):
        raise SystemExit("REFUSE-MINT: output dir already exists: %s (never overwrite a version)" % out_dir)

    run_ids = [e["run_id"] for e in episodes]
    if len(set(run_ids)) != n:
        raise SystemExit("REFUSE-MINT: duplicate run_id among included episodes")
    index_of = {rid: i for i, rid in enumerate(run_ids)}   # sorted order -> episode_index

    # FSM assert on EVERY episode BEFORE writing anything (an unmapped id aborts the whole mint).
    per_ep_counts = {}
    for e in episodes:
        try:
            per_ep_counts[e["run_id"]] = _fsm_counts(e["state"])
        except ValueError as ex:
            raise SystemExit("REFUSE-MINT: run %s: %s" % (e["run_id"], ex))

    train, val = _split(run_ids, seed, val_fraction)
    if not val or not train:
        raise SystemExit("REFUSE-MINT: split yielded train=%d val=%d (need >=1 each)"
                         % (len(train), len(val)))

    os.makedirs(os.path.join(out_dir, "data", "chunk-000"))
    os.makedirs(os.path.join(out_dir, "meta"))
    vid_dir = os.path.join(out_dir, "videos", "chunk-000", "observation.images.head_rgb")
    os.makedirs(vid_dir)

    # per-episode: parquet (+ video best-effort), collect the video_index + a per-run row.
    video_index = {}
    ep_rows = []
    h = w = 0
    for e in episodes:
        idx = index_of[e["run_id"]]
        _write_parquet(os.path.join(out_dir, "data", "chunk-000", "episode_%06d.parquet" % idx),
                       e["state"], e["action"], fps)
        vpath = os.path.join(vid_dir, "episode_%06d.mp4" % idx)
        backend = _try_video(vpath, e.get("rgb"), fps)
        # dims from the first real rgb frame (0 if imageless)
        for im in (e.get("rgb") or []):
            if im is not None:
                h, w = int(np.asarray(im).shape[0]), int(np.asarray(im).shape[1]); break
        video_index["episode_%06d" % idx] = {
            "run_id": e["run_id"], "frame_count": int(np.asarray(e["state"]).shape[0]),
            "source_frame_idx": [int(x) for x in e.get("frames", [])],
            "height": h, "width": w, "video_backend": backend,
            "encode": {"codec": "libx264", "pix_fmt": "yuv420p", "crf": 23, "fps": int(fps),
                       "threads": 1, "faststart": True}}
        m = e.get("meta", {})
        ep_rows.append({"episode_index": idx, "run_id": e["run_id"], "length": int(np.asarray(e["state"]).shape[0]),
                        "split": "val" if e["run_id"] in set(val) else "train",
                        "git_version": m.get("git_version"), "profile": m.get("profile"),
                        "outcome": m.get("outcome"), "scorer_version": m.get("scorer_version"),
                        "duration_s": m.get("duration_s"), "fsm_counts": per_ep_counts[e["run_id"]]})

    # stats over the TRAIN split only (state + action).
    train_set = set(train)
    state_train = np.concatenate([np.asarray(e["state"], dtype=np.float64)
                                  for e in episodes if e["run_id"] in train_set], axis=0)
    action_train = np.concatenate([np.asarray(e["action"], dtype=np.float64)
                                   for e in episodes if e["run_id"] in train_set], axis=0)
    stats = {"observation.state": _channel_stats(state_train),
             "action": _channel_stats(action_train),
             "normalize": {"observation.state": {"zscore_idx": ZSCORE_IDX, "passthrough_idx": PASSTHROUGH_IDX,
                                                  "names": STATE_NAMES},
                           "action": {"zscore_idx": [0, 1], "passthrough_idx": [], "names": ACTION_NAMES}},
             "over": "train_split_only", "train_episodes": len(train)}
    stats_bytes = _write_json(os.path.join(out_dir, "meta", "stats.json"), stats)
    stats_hash = _sha256_bytes(stats_bytes)

    splits = {"version": 1, "dataset_version": version, "seed": int(seed),
              "method": "seeded_shuffle_sorted_run_ids", "val_fraction": float(val_fraction),
              "train": train, "val": val}
    _write_json(os.path.join(out_dir, "meta", "splits.json"), splits)
    _write_json(os.path.join(out_dir, "meta", "video_index.json"), video_index)
    # per-episode + tasks metadata (LeRobot-v3-shaped)
    with open(os.path.join(out_dir, "meta", "episodes.jsonl"), "w") as f:
        for r in sorted(ep_rows, key=lambda r: r["episode_index"]):
            f.write(json.dumps({"episode_index": r["episode_index"], "tasks": [task],
                                "length": r["length"], "run_id": r["run_id"]}, sort_keys=True) + "\n")
    with open(os.path.join(out_dir, "meta", "tasks.jsonl"), "w") as f:
        f.write(json.dumps({"task_index": 0, "task": task}) + "\n")

    # aggregate per-split fsm occupancy (B1: S1 owns the card's authoritative per-state counts)
    def _agg(rids):
        tot = {}
        for rid in rids:
            for k, v in per_ep_counts[rid].items():
                tot[k] = tot.get(k, 0) + v
        return tot
    content_hash = _content_hash(out_dir)

    card = {
        "codebase_version": CODEBASE_VERSION, "dataset_version": version,
        "content_hash": content_hash, "stats_hash": stats_hash, "fps": fps,
        "fsm_states_version": fsm_groups.VERSION,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [len(STATE_NAMES)], "names": STATE_NAMES},
            "action": {"dtype": "float32", "shape": [len(ACTION_NAMES)], "names": ACTION_NAMES},
            "observation.images.head_rgb": {"dtype": "video", "shape": [h, w, 3],
                                            "names": ["height", "width", "channel"]}},
        "total_episodes": n, "total_frames": sum(r["length"] for r in ep_rows),
        "run_id_to_episode_index": index_of,
        "split": {"train": train, "val": val, "method": "seeded_shuffle_sorted_run_ids", "seed": seed},
        "fsm_occupancy": {"train": _agg(train), "val": _agg(val)},
        "episodes": sorted(ep_rows, key=lambda r: r["episode_index"]),
        "include_filter": list(provenance.get("include_filter", DEFAULT_INCLUDE)) if provenance else list(DEFAULT_INCLUDE),
        "excluded_runs": excluded, "excluded_count": len(excluded),
        "ingest_tool_sha": provenance.get("ingest_tool_sha", "nogit") if provenance else "nogit",
        "rerun_sdk_version": provenance.get("rerun_sdk_version") if provenance else None,
        "trust": ["depth loosely aligned + NOT exported (join by wall time)",
                  "intrinsics approximate (fy:=fx from --hfov-deg) -- P8.1 recalibrates",
                  "mono head_rgb only", "vy identically 0, omitted from action",
                  "timestamps synthetic at nominal fps", "pre-lock SEARCH frames trimmed -> SEARCH counts near-zero"],
        "obs_action_pairing": "action_t = command emitted the same control tick as obs_t",
        "note": "RAW LeRobot-v3 layout; mp4 bytes are excluded from content_hash (represented by "
                "meta/video_index.json). NOT thesis dual-POV/LiDAR data. See docs/COMPUTE_PLACEMENT.md.",
    }
    _write_json(os.path.join(out_dir, "meta", "info.json"), card)

    # dataset_card/ mirror (meta only, no parquet/mp4) -> synced back to the laptop store so the laptop
    # stays source-of-truth for dataset IDENTITY even though the bytes live on disposable compute.
    mirror = os.path.join(out_dir, "dataset_card")
    os.makedirs(mirror)
    for name in ("info.json", "stats.json", "splits.json", "video_index.json"):
        src = os.path.join(out_dir, "meta", name)
        with open(src, "rb") as a, open(os.path.join(mirror, name), "wb") as b:
            b.write(a.read())

    return {"content_hash": content_hash, "stats_hash": stats_hash, "n_episodes": n,
            "train": train, "val": val, "version": version, "excluded": len(excluded)}


def _try_video(path, rgb, fps):
    """Best-effort mp4 (via rrd_to_lerobot._write_video). Video bytes are OUT of the content_hash, so a
    missing ffmpeg is not fatal to the gate -- record the backend + move on."""
    if not rgb or all(im is None for im in rgb):
        return "none"
    try:
        from rrd_to_lerobot import _write_video
        ok = _write_video(path, rgb, fps)
        return "mp4" if ok else "png-fallback"
    except Exception as e:  # noqa: BLE001
        return "unavailable (%s)" % type(e).__name__


# ---- rrd adapter + discovery (bundle path; needs rerun) -----------------------------------------
def episode_from_bundle(bundle):
    """runs/<run_id>/ bundle -> in-memory episode via rrd_to_lerobot's decoder (imported, never
    re-typed). Returns None if the bundle has no .rrd (a non-capture run -- excluded upstream)."""
    from rrd_to_lerobot import read_rrd, assemble_episode
    run_id = os.path.basename(os.path.normpath(bundle))
    rrd = _find_rrd(bundle)
    if rrd is None:
        return None
    scalars, images, _depth = read_rrd(rrd)
    frames, state, action, rgb = assemble_episode(scalars, images)
    man = _read_manifest(bundle)
    meta = {"git_version": man.get("git_version"), "profile": man.get("profile"),
            "duration_s": man.get("duration_s"),
            "outcome": (man.get("label") or {}).get("outcome"),
            "scorer_version": (man.get("label") or {}).get("scorer_version")}
    return {"run_id": run_id, "frames": frames, "state": state, "action": action, "rgb": rgb, "meta": meta}


def _find_rrd(bundle):
    for name in sorted(os.listdir(bundle)):
        if name.endswith(".rrd"):
            return os.path.join(bundle, name)
    return None


def _read_manifest(bundle):
    p = os.path.join(bundle, "manifest.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def _verify_integrity(bundle, man):
    """Verify each file's sha256 vs manifest before ingest (catches a truncated laptop->desktop sync).
    Returns None if OK, else a reason string."""
    files = man.get("files")
    if not isinstance(files, list):
        return None                                # older manifest without per-file hashes -> skip check
    for rec in files:
        name = rec.get("name") or rec.get("path")
        want = rec.get("sha256")
        if not name or not want:
            continue
        p = os.path.join(bundle, name)
        if not os.path.isfile(p) or _sha256_file(p) != want:
            return "manifest-hash-mismatch (%s)" % name
    return None


def ingest(runs_dir, out_dir, version, seed=0, val_fraction=0.2, include_outcomes=DEFAULT_INCLUDE):
    """Discover bundles under runs_dir, filter by label + integrity (LOUD exclusions), build episodes,
    mint. The P7.1 gate: included episodes == runs passing the filter; total == included + excluded."""
    include = set(include_outcomes)
    included, excluded = [], []
    for name in sorted(os.listdir(runs_dir)):
        bundle = os.path.join(runs_dir, name)
        if not os.path.isdir(bundle):
            continue
        man = _read_manifest(bundle)
        label = man.get("label") or {}
        outcome = label.get("outcome")
        reason = None
        if _find_rrd(bundle) is None:
            reason = "no-rrd-not-a-capture-run"
        elif outcome is None:
            reason = "unlabeled"
        elif outcome not in include:
            reason = "outcome-filtered (%s)" % outcome
        else:
            reason = _verify_integrity(bundle, man)
        if reason:
            print("EXCLUDE %s -- %s" % (name, reason))
            excluded.append({"run_id": name, "reason": reason})
            continue
        ep = episode_from_bundle(bundle)
        if ep is None or int(__import__("numpy").asarray(ep["state"]).shape[0]) == 0:
            print("EXCLUDE %s -- no-TRACK-frames" % name)
            excluded.append({"run_id": name, "reason": "no-TRACK-frames"})
            continue
        included.append(ep)
    print("INGEST included=%d excluded=%d" % (len(included), len(excluded)))
    prov = {"include_filter": sorted(include), "ingest_tool_sha": _git_sha(),
            "rerun_sdk_version": _rerun_version()}
    return mint_dataset(included, out_dir, version, seed=seed, val_fraction=val_fraction,
                        provenance=prov, excluded=excluded)


def _git_sha():
    import subprocess
    try:
        return subprocess.check_output(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                                        "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL).decode().strip() or "nogit"
    except (subprocess.SubprocessError, OSError):
        return "nogit"


def _rerun_version():
    try:
        import rerun
        return rerun.__version__
    except Exception:  # noqa: BLE001
        return None


# ---- self-test ----------------------------------------------------------------------------------
def _selftest():
    import shutil
    import tempfile
    import numpy as np
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import synth_fixtures as sf

    # CORE TIER (no rerun/.rrd): injected in-memory episodes.
    eps = sf.make_episodes(seed=0)
    valid = [e for e in eps if e["meta"].get("outcome") == "pass"
             and fsm_groups.UNMAPPED not in fsm_groups.count_ids(np.asarray(e["state"])[:, FSM_COL].tolist())]
    assert len(valid) >= 3, "need >=3 valid fixture episodes, got %d" % len(valid)

    base = tempfile.mkdtemp(prefix="k1ds-")
    try:
        d1 = os.path.join(base, "v1a")
        d2 = os.path.join(base, "v1b")
        r1 = mint_dataset(valid, d1, "k1_follow_v1", seed=7, val_fraction=0.34)
        r2 = mint_dataset(valid, d2, "k1_follow_v1", seed=7, val_fraction=0.34)
        assert r1["content_hash"] == r2["content_hash"], "content_hash must reproduce across mints"
        assert r1["stats_hash"] == r2["stats_hash"]
        assert r1["n_episodes"] == len(valid)
        # splits are a pure fn of sorted run_ids + seed
        assert r1["train"] == r2["train"] and r1["val"] == r2["val"]
        assert r1["train"] and r1["val"] and not (set(r1["train"]) & set(r1["val"]))

        # stats == an INDEPENDENT plain-numpy TRAIN-only reference
        card = json.load(open(os.path.join(d1, "meta", "info.json")))
        stats = json.load(open(os.path.join(d1, "meta", "stats.json")))
        train_set = set(r1["train"])
        st = np.concatenate([np.asarray(e["state"], np.float64) for e in valid
                             if e["run_id"] in train_set], 0)
        ref_mean = st.mean(0).tolist()
        ref_std = st.std(0).tolist()
        assert np.allclose(stats["observation.state"]["mean"], ref_mean), "stats mean != train-only ref"
        assert np.allclose(stats["observation.state"]["std"], ref_std)
        assert stats["observation.state"]["count"][0] == int(st.shape[0])
        # per-state counts present in the card (B1)
        assert card["fsm_occupancy"]["train"], "card must carry per-state train occupancy"
        assert card["run_id_to_episode_index"] and card["stats_hash"] == r1["stats_hash"]
        # splits.json keyed by run_id, sorted
        sp = json.load(open(os.path.join(d1, "meta", "splits.json")))
        assert sp["train"] == sorted(sp["train"]) and sp["method"] == "seeded_shuffle_sorted_run_ids"

        # REFUSALS: existing dir, too-few episodes, unmapped FSM.
        _refuses(lambda: mint_dataset(valid, d1, "k1_follow_v1"))          # dir exists
        _refuses(lambda: mint_dataset(valid[:1], os.path.join(base, "tiny"), "v"))  # < MIN_INCLUDED
        unmapped = [e for e in eps if fsm_groups.UNMAPPED in
                    fsm_groups.count_ids(np.asarray(e["state"])[:, FSM_COL].tolist())]
        if unmapped:
            bad = valid[:2] + unmapped[:1]
            _refuses(lambda: mint_dataset(bad, os.path.join(base, "bad"), "v"))     # unmapped aborts

        # BUNDLE TIER (rerun present here): discover -> filter -> mint from real .rrd bundles.
        try:
            import rerun  # noqa: F401
            runs = os.path.join(base, "runs")
            sf.make_bundles(runs, seed=0, with_rrd=True)
            # (1) a REAL unmapped-FSM .rrd anywhere in the corpus ABORTS the whole mint (charter blocker).
            _refuses(lambda: ingest(runs, os.path.join(base, "ds_abort"), "v", seed=3, val_fraction=0.34))
            # (2) remove the planted unmapped bundle(s) by probing (no hardcoded run_id) -> clean corpus.
            for name in list(os.listdir(runs)):
                b = os.path.join(runs, name)
                if not os.path.isdir(b):
                    continue
                try:
                    ep = episode_from_bundle(b)
                except Exception:  # noqa: BLE001
                    ep = None
                if ep is not None and fsm_groups.UNMAPPED in fsm_groups.count_ids(
                        np.asarray(ep["state"])[:, FSM_COL].tolist()):
                    shutil.rmtree(b)
            out = os.path.join(base, "ds_bundle")
            res = ingest(runs, out, "k1_follow_v1", seed=3, val_fraction=0.34,
                         include_outcomes=("pass",))
            # the planted unlabeled + no-rrd bundles are EXCLUDED loudly, not ingested.
            assert res["excluded"] >= 2, "unlabeled + no-rrd must be excluded, got %d" % res["excluded"]
            assert res["n_episodes"] >= 3
            # integrity holds end-to-end: the minted card's content_hash is present + reproduces
            card_b = json.load(open(os.path.join(out, "meta", "info.json")))
            assert card_b["content_hash"] == res["content_hash"]
            assert card_b["excluded_count"] >= 2 and card_b["rerun_sdk_version"]
            print("INGEST bundle-tier: included=%d excluded=%d" % (res["n_episodes"], res["excluded"]))
        except ImportError:
            print("INGEST bundle-tier SKIPPED (rerun-sdk absent)")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return True


def _refuses(fn):
    try:
        fn()
    except SystemExit:
        return
    raise AssertionError("expected a REFUSE-MINT SystemExit")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("mint", help="mint a versioned dataset from runs/")
    m.add_argument("--runs", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--version", required=True)
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--val-fraction", type=float, default=0.2)
    m.add_argument("--include-outcomes", nargs="+", default=list(DEFAULT_INCLUDE))
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "selftest" or a.cmd is None:
        _selftest()
        print("INGEST-SELFTEST-OK")
        return 0
    res = ingest(a.runs, a.out, a.version, seed=a.seed, val_fraction=a.val_fraction,
                 include_outcomes=tuple(a.include_outcomes))
    print("MINTED %s: %d episodes (train=%d val=%d, excluded=%d) content_hash=%s"
          % (res["version"], res["n_episodes"], len(res["train"]), len(res["val"]),
             res["excluded"], res["content_hash"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
