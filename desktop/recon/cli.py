#!/usr/bin/env python3
"""cli.py -- the recon container entrypoint `recon` (charter S3; contract: docs/RECON_CONTRACT.md).

Image v1 is CPU-lean and now implements ALL stages: ingest (bundle validation) + the geometry stages
odom / tsdf / simexport / align (the heavy open3d/coacd/cv2 work lives in geom.py, imported lazily).
`splat` remains reserved for image v2 (P6G.4). Everything here binds to the FROZEN recon_manifest schema
+ stage enum in docs/RECON_CONTRACT.md -- do not drift a field without amending that doc (two other
machines code against it). Metric stages (odom/tsdf) fail-closed on approximate/missing intrinsics unless
--allow-approximate-intrinsics; align's cross-run map merge is DEFERRED (P8.3, data-blocked).

Run in the container (bundle RO at /in, output RW at /out):
  docker run -d --name recon_<id> -v ~/k1/inbox/<id>:/in:ro -v ~/k1/outbox/<id>:/out \
      -e RECON_IMAGE_DIGEST="$(docker inspect --format '{{.Id}}' k1recon:v1)" k1recon:v1 \
      recon --run <id> --stages ingest,odom,tsdf,simexport,align
  docker run --rm k1recon:v1 recon --selftest        # the P6G.2 image smoke (open3d TSDF)

Locally (no container): the ingest + manifest + dispatch logic is testable against a synth bundle --
  python desktop/recon/cli.py --run <id> --stages ingest,tsdf --bundle-dir <bundle> --out-dir <out>
open3d is imported LAZILY (only the selftest's TSDF smoke needs it), so this module imports without it.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import numpy as np

# eval/rrd_to_lerobot.read_rrd is the .rrd decoder (imported, never re-typed). In the container the
# Dockerfile copies eval/ next to this; locally, walk up to the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_HERE, "eval"),                       # eval copied beside cli.py
              os.path.join(_HERE, "..", "eval"),                 # container layout: /app/recon -> /app/eval
              os.path.join(_HERE, "..", "..", "eval")):          # repo layout: desktop/recon -> <root>/eval
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break

# ---- frozen contract constants (docs/RECON_CONTRACT.md) -----------------------------------------
STAGES = ("ingest", "odom", "tsdf", "simexport", "align")        # FROZEN order; splat reserved for v2
METRIC_STAGES = frozenset(("odom", "tsdf"))                      # intrinsics-gated (contract doctrine 2)
V1_IMPLEMENTED = frozenset(STAGES)                               # all stages implemented (geom.py)
DEPTH_MIN_M, DEPTH_MAX_M = 0.15, 15.0
MAX_PAIR_SKEW_S = 0.06
MASK_DILATE_FRAC = 0.15
_FRAME_FIXED = frozenset(("run_local", "map", "camera"))
_ANCHOR_RE = re.compile(r"^anchor_\d+$")
_UTC = "%Y%m%dT%H%M%SZ"


def _now_utc():
    return time.strftime(_UTC, time.gmtime())


def _valid_frame(f):
    return isinstance(f, str) and (f in _FRAME_FIXED or bool(_ANCHOR_RE.match(f)))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bundle_inputs_hash(bundle_manifest):
    """image-v1 inputs_hash: sha256 over sorted `name\\0sha256\\n` from the bundle manifest files[]."""
    lines = sorted("%s\0%s\n" % (f["name"], f["sha256"]) for f in bundle_manifest.get("files", []))
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


# ---- manifest writer (ENFORCES frame tags) ------------------------------------------------------
def _check_artifacts(stage_name, metrics):
    """Every artifact a stage reports must carry a valid `frame` tag (contract section 3). An untagged
    artifact is a BUG -> raise, never a warning."""
    for a in metrics.get("artifacts", []):
        if not _valid_frame(a.get("frame")):
            raise ValueError("stage %s artifact %r has invalid/absent frame %r (vocab: run_local|map|"
                             "camera|anchor_<int>)" % (stage_name, a.get("path"), a.get("frame")))


def _write_manifest(out_dir, manifest):
    """Write recon_manifest.json ALWAYS (even on failure), atomically (.tmp + rename), sort_keys."""
    for st in manifest["stages"]:
        _check_artifacts(st["name"], st.get("metrics") or {})
    path = os.path.join(out_dir, "recon_manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, sort_keys=True, indent=2)
    os.replace(tmp, path)
    return path


# ---- stages -------------------------------------------------------------------------------------
def _find_rrd(bundle):
    for name in sorted(os.listdir(bundle)):
        if name.endswith(".rrd"):
            return os.path.join(bundle, name)
    return None


def _read_intrinsics_source(bundle):
    p = os.path.join(bundle, "intrinsics.json")
    if not os.path.isfile(p):
        return "missing", None
    try:
        with open(p) as f:
            d = json.load(f)
    except (ValueError, OSError):
        return "unknown", None
    if d.get("approximate") is True or d.get("model") == "pinhole_from_hfov":
        return "approximate_hfov", d
    if d.get("model") == "calibrated" or d.get("approximate") is False:
        return "calibrated", d
    return "unknown", d


def stage_ingest(bundle, out_dir, params):
    """Fail-closed bundle validation (contract section 3 / doctrine 2-3). Returns
    (status, exit_code, metrics, intrinsics_source)."""
    metrics = {"artifacts": []}
    # (a) every manifest file's sha256 vs disk; run_id agreement (wrong-mount detector).
    man_path = os.path.join(bundle, "manifest.json")
    if not os.path.isfile(man_path):
        return "failed", 1, {"error": "no manifest.json in bundle", "artifacts": []}, "unknown"
    with open(man_path) as f:
        bman = json.load(f)
    if params.get("expect_run_id") and bman.get("run_id") not in (None, params["expect_run_id"]):
        return ("failed", 1, {"error": "bundle run_id %r != --run %r (wrong mount?)"
                              % (bman.get("run_id"), params["expect_run_id"]), "artifacts": []}, "unknown")
    n_ver = 0
    for rec in bman.get("files", []):
        name, want = rec.get("name"), rec.get("sha256")
        if not name or not want:
            continue
        fp = os.path.join(bundle, name)
        if not os.path.isfile(fp) or _sha256_file(fp) != want:
            return "failed", 1, {"error": "manifest-hash-mismatch: %s" % name, "artifacts": []}, "unknown"
        n_ver += 1
    # (b) intrinsics gate input.
    intr_src, _ = _read_intrinsics_source(bundle)
    # (c) depth-unit sanity via the .rrd read (doctrine 3). No .rrd / no depth -> refuse (not P8-usable).
    rrd = _find_rrd(bundle)
    if rrd is None:
        return "failed", 1, {"error": "no .rrd (not a capture run / not P8-usable)",
                             "intrinsics_source": intr_src, "artifacts": []}, intr_src
    try:
        from rrd_to_lerobot import read_rrd
        _scalars, _images, depth = read_rrd(rrd)
    except Exception as e:  # noqa: BLE001
        return "failed", 1, {"error": "rrd read failed: %s" % e,
                             "intrinsics_source": intr_src, "artifacts": []}, intr_src
    if not depth:
        return "failed", 1, {"error": "no /camera/depth frames in the .rrd (not P8-usable)",
                             "intrinsics_source": intr_src, "artifacts": []}, intr_src
    vals = np.concatenate([np.asarray(d, dtype=np.float64).ravel() for d in depth.values()])
    finite_pos = vals[np.isfinite(vals) & (vals > 0.0)]
    if finite_pos.size == 0:
        return "failed", 1, {"error": "no finite positive depth values",
                             "intrinsics_source": intr_src, "artifacts": []}, intr_src
    med = float(np.median(finite_pos))
    in_range = float(np.mean((finite_pos >= DEPTH_MIN_M) & (finite_pos <= DEPTH_MAX_M)))
    if not (DEPTH_MIN_M <= med <= DEPTH_MAX_M):
        return ("failed", 1, {"error": "depth median %.3f outside [%.2f, %.2f] m -- likely a mm-vs-m "
                             "unit bug" % (med, DEPTH_MIN_M, DEPTH_MAX_M), "depth_median_m": med,
                             "intrinsics_source": intr_src, "artifacts": []}, intr_src)
    metrics.update({"files_verified": n_ver, "depth_frames": len(depth), "depth_median_m": med,
                    "depth_in_range_frac": in_range, "depth_scale_m_per_unit": 1.0,
                    "intrinsics_source": intr_src})
    return "ok", 0, metrics, intr_src


# ---- geometry stages (P6G.2) -- the heavy lifting lives in desktop/recon/geom.py (open3d/coacd/cv2),
# imported lazily so the CPU-lean ingest path never pays for open3d. Each returns (status, code,
# metrics, params). ctx threads shared state (frames/intr/trajectory/mesh) across the pipeline. -------
def _geom():
    import geom                     # same dir as cli.py: /app/recon (container) or desktop/recon (local)
    return geom


def _load_intrinsics(bundle):
    with open(os.path.join(bundle, "intrinsics.json")) as f:
        d = json.load(f)
    for k in ("width", "height", "fx", "fy", "cx", "cy"):
        if k not in d:
            raise ValueError("intrinsics.json missing %r" % k)
    return d


def _ensure_frames(ctx):
    """Read the bundle .rrd once (wall-timed), pair+mask -> frames, and build the intrinsic. Cached."""
    if ctx.get("frames") is not None:
        return ctx["frames"], ctx["intr"]
    from rrd_to_lerobot import read_rrd_frames
    geom = _geom()
    rrd = _find_rrd(ctx["bundle"])
    if rrd is None:
        raise ValueError("no .rrd in bundle (ingest should have caught this)")
    rgb_stream, depth_stream = read_rrd_frames(rrd)
    frames, fstats = geom.prepare_frames(rgb_stream, depth_stream,
                                         dilate_frac=MASK_DILATE_FRAC, max_skew_s=MAX_PAIR_SKEW_S)
    d = _load_intrinsics(ctx["bundle"])
    intr = geom.make_intrinsic(d["width"], d["height"], d["fx"], d["fy"], d["cx"], d["cy"])
    ctx["frames"], ctx["intr"], ctx["frame_stats"] = frames, intr, fstats
    return frames, intr


def _metric_gate(ctx, stage):
    """Metric stages (odom/tsdf) REFUSE approximate/missing intrinsics unless the override is passed
    (contract doctrine 2). Returns a failed 4-tuple to short-circuit, or None to proceed."""
    if ctx["intr_source"] == "calibrated" or ctx["allow_approx"]:
        return None
    return ("failed", 1,
            {"error": "%s refuses intrinsics_source=%r (not calibrated); pass "
                      "--allow-approximate-intrinsics to override" % (stage, ctx["intr_source"]),
             "intrinsics_source": ctx["intr_source"], "artifacts": []},
            {"allow_approximate_intrinsics": False})


def _approx_params(ctx):
    return {"allow_approximate_intrinsics": bool(ctx["allow_approx"])}


def stage_odom(ctx):
    gate = _metric_gate(ctx, "odom")
    if gate:
        return gate
    frames, intr = _ensure_frames(ctx)
    if len(frames) < 2:
        return ("failed", 1, {"error": "odom needs >= 2 paired RGBD frames, got %d (RGBD wall-time "
                "pairing yielded too few)" % len(frames), "artifacts": []}, _approx_params(ctx))
    res = _geom().rgbd_odometry(frames, intr, pose_prior=None, loop_closure=True)
    ctx["trajectory"] = res["trajectory"]
    out = ctx["out"]
    traj_p = os.path.join(out, "trajectory.jsonl")
    with open(traj_p, "w") as f:
        for fr, T in zip(frames, res["trajectory"]):
            f.write(json.dumps({"wall_t": fr["t"], "T_run_local_camera": T.tolist()}) + "\n")
    pg_p = os.path.join(out, "pose_graph.json")
    with open(pg_p, "w") as f:
        json.dump({"nodes": [T.tolist() for T in res["trajectory"]],
                   "loop_edges": [{"i": i, "j": j, "info00": w} for (i, j, w) in res["loop_edges"]]},
                  f, indent=2)
    metrics = dict(res["stats"]); metrics.update(ctx.get("frame_stats", {}))
    metrics["intrinsics_source"] = ctx["intr_source"]
    metrics["artifacts"] = [
        {"path": "trajectory.jsonl", "frame": "run_local", "bytes": os.path.getsize(traj_p)},
        {"path": "pose_graph.json", "frame": "run_local", "bytes": os.path.getsize(pg_p)}]
    return "ok", 0, metrics, _approx_params(ctx)


def stage_tsdf(ctx):
    gate = _metric_gate(ctx, "tsdf")
    if gate:
        return gate
    if ctx.get("trajectory") is None:
        return ("failed", 1, {"error": "tsdf requires the odom trajectory (odom did not run)",
                              "artifacts": []}, _approx_params(ctx))
    frames, intr = _ensure_frames(ctx)
    geom = _geom()
    mesh = geom.tsdf_integrate(frames, ctx["trajectory"], intr)
    ctx["mesh"] = mesh
    mv = os.path.join(ctx["out"], "mesh_visual.ply")
    geom.write_mesh_ply(mesh, mv)
    nverts = len(mesh.vertices)
    metrics = {"vertices": int(nverts), "triangles": int(len(mesh.triangles)),
               "intrinsics_source": ctx["intr_source"],
               "artifacts": [{"path": "mesh_visual.ply", "frame": "run_local",
                              "bytes": os.path.getsize(mv)}]}
    if nverts == 0:
        metrics["error"] = "TSDF produced an empty mesh (no depth integrated along the trajectory)"
        return "failed", 1, metrics, _approx_params(ctx)
    return "ok", 0, metrics, _approx_params(ctx)


def stage_simexport(ctx):
    if ctx.get("mesh") is None:
        return ("failed", 1, {"error": "simexport requires the tsdf mesh (tsdf did not run)",
                              "artifacts": []}, {})
    geom = _geom()
    visual, parts, sx = geom.simplify_and_decompose(ctx["mesh"], seed=ctx["seed"])
    out = ctx["out"]
    vd = os.path.join(out, "mesh_visual_decimated.ply")
    geom.write_mesh_ply(visual, vd)
    arts = [{"path": "mesh_visual_decimated.ply", "frame": "run_local", "bytes": os.path.getsize(vd)}]
    cdir = os.path.join(out, "mesh_collision")
    os.makedirs(cdir, exist_ok=True)
    for k, pm in enumerate(parts):
        pp = os.path.join(cdir, "part_%03d.obj" % k)
        geom.write_obj(pm, pp)
        arts.append({"path": "mesh_collision/part_%03d.obj" % k, "frame": "run_local",
                     "bytes": os.path.getsize(pp)})
    metrics = dict(sx); metrics["artifacts"] = arts
    return "ok", 0, metrics, {"coacd_threshold": sx["coacd_threshold"], "seed": ctx["seed"]}


def stage_align(ctx):
    frames, intr = _ensure_frames(ctx)
    geom = _geom()
    obs = geom.detect_apriltags(frames, intr, ctx["tag_size_m"])
    ap = os.path.join(ctx["out"], "apriltag_observations.json")
    with open(ap, "w") as f:
        json.dump({"tag_dict": geom._TAG_DICT, "tag_size_m": ctx["tag_size_m"],
                   "cross_run_map_alignment": "deferred (P8.3 -- needs >= 2 real runs, different days)",
                   "tags": obs}, f, indent=2)
    metrics = {"tags_detected": sorted(obs.keys()),
               "observations_total": int(sum(v["n"] for v in obs.values())),
               "cross_run_alignment": "deferred",
               "artifacts": [{"path": "apriltag_observations.json", "frame": "camera",
                              "bytes": os.path.getsize(ap)}]}
    return "ok", 0, metrics, {"tag_size_m": ctx["tag_size_m"]}


_GEOM_STAGES = {"odom": stage_odom, "tsdf": stage_tsdf, "simexport": stage_simexport, "align": stage_align}
_STAGE_DEPS = {"tsdf": ["odom"], "simexport": ["odom", "tsdf"]}


# ---- driver -------------------------------------------------------------------------------------
def run(run_id, stages, bundle_dir, out_dir, seed, allow_approx, image_digest, tag_size_m=0.16):
    os.makedirs(out_dir, exist_ok=True)
    # expand deps (tsdf<-odom, simexport<-odom,tsdf), then order by the FROZEN enum with ingest first.
    want = set(stages)
    for s in list(want):
        want.update(_STAGE_DEPS.get(s, []))
    plan = ["ingest"] + [s for s in STAGES if s in want and s != "ingest"]

    ctx = {"bundle": bundle_dir, "out": out_dir, "seed": int(seed), "allow_approx": bool(allow_approx),
           "intr_source": "unknown", "frames": None, "intr": None, "trajectory": None, "mesh": None,
           "tag_size_m": float(tag_size_m)}
    started = _now_utc()
    stage_recs = []
    stopped = False
    for name in plan:
        t0 = time.time()
        if stopped:
            stage_recs.append({"name": name, "status": "skipped", "exit_code": None, "wall_s": 0.0,
                               "params": {}, "metrics": {"note": "skipped: an earlier stage failed",
                                                         "artifacts": []}, "inputs_hash": None})
            continue
        if name == "ingest":
            params = {"depth_min_m": DEPTH_MIN_M, "depth_max_m": DEPTH_MAX_M,
                      "max_pair_skew_s": MAX_PAIR_SKEW_S, "person_mask": "target-box-v0",
                      "mask_dilate_frac": MASK_DILATE_FRAC, "expect_run_id": run_id}
            status, code, metrics, ctx["intr_source"] = stage_ingest(bundle_dir, out_dir, params)
            params.pop("expect_run_id", None)
        else:
            try:
                status, code, metrics, params = _GEOM_STAGES[name](ctx)
            except Exception as e:  # noqa: BLE001 -- a stage bug is a LOUD failure, never a silent hang
                status, code, params = "failed", 1, {}
                metrics = {"error": "%s raised: %s: %s" % (name, type(e).__name__, e), "artifacts": []}
        ihash = None
        try:
            with open(os.path.join(bundle_dir, "manifest.json")) as f:
                ihash = _bundle_inputs_hash(json.load(f))
        except (ValueError, OSError):
            pass
        stage_recs.append({"name": name, "status": status, "exit_code": code,
                           "wall_s": round(time.time() - t0, 3), "params": params,
                           "metrics": metrics, "inputs_hash": ihash})
        if status == "failed":
            stopped = True                                       # subsequent stages -> skipped

    manifest = {"run_id": run_id, "image_digest": image_digest, "stages": stage_recs, "seed": int(seed),
                "intrinsics_source": ctx["intr_source"], "started_utc": started, "finished_utc": _now_utc()}
    _write_manifest(out_dir, manifest)
    statuses = [s["status"] for s in stage_recs]
    if "failed" in statuses:
        return 1
    if "not_implemented" in statuses:
        return 3
    return 0


# ---- selftest (the P6G.2 image smoke; open3d) ---------------------------------------------------
def selftest(out_dir=None):  # pragma: no cover locally -- needs open3d (VERIFY IN CONTAINER)
    import tempfile
    out_dir = out_dir or tempfile.mkdtemp(prefix="recon-st-")
    os.makedirs(out_dir, exist_ok=True)
    try:
        import open3d as o3d
    except ImportError:
        print("RECON-SELFTEST-NEEDS-OPEN3D: the --selftest TSDF smoke needs open3d (present in the "
              "recon image). Build + run it in the container: `docker run --rm k1recon:v1 recon "
              "--selftest`. See docs/RECON_CONTRACT.md section 6.")
        return 2
    # synthetic RGBD of a known plane at z=1.0 m, identity pose (camera == world).
    W = H = 64
    fx = fy = 50.0
    cx, cy = W / 2.0, H / 2.0
    depth = np.full((H, W), 1.0, dtype=np.float32)
    color = np.zeros((H, W, 3), dtype=np.uint8)
    color[:] = (120, 120, 120)
    intr = o3d.camera.PinholeCameraIntrinsic(W, H, fx, fy, cx, cy)
    vol = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=0.01, sdf_trunc=0.04,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)
    rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
        o3d.geometry.Image(color), o3d.geometry.Image(depth), depth_scale=1.0,
        depth_trunc=5.0, convert_rgb_to_intensity=False)
    vol.integrate(rgbd, intr, np.eye(4))
    mesh = vol.extract_triangle_mesh()
    verts = np.asarray(mesh.vertices)
    assert verts.shape[0] > 0, "TSDF produced no vertices"
    rms = float(np.sqrt(np.mean((verts[:, 2] - 1.0) ** 2))) if verts.shape[0] else 1e9
    assert rms < 0.010, "plane-fit RMS %.4f m too high" % rms
    mesh_dir = out_dir
    o3d.io.write_triangle_mesh(os.path.join(mesh_dir, "mesh_visual.ply"), mesh)
    manifest = {
        "run_id": "selftest", "image_digest": os.environ.get("RECON_IMAGE_DIGEST", "unset"),
        "seed": 0, "intrinsics_source": "synthetic",
        "started_utc": _now_utc(), "finished_utc": _now_utc(),
        "stages": [
            {"name": "tsdf", "status": "ok", "exit_code": 0, "wall_s": 0.0, "params": {},
             "metrics": {"vertices": int(verts.shape[0]), "plane_rms_m": rms,
                         "artifacts": [{"path": "mesh_visual.ply", "frame": "camera",
                                        "bytes": os.path.getsize(os.path.join(mesh_dir, "mesh_visual.ply"))}]},
             "inputs_hash": None},
            # a DELIBERATELY-failed align stage -- proves the failure plumbing renders (loud, not fatal).
            {"name": "align", "status": "failed", "exit_code": 1, "wall_s": 0.0, "params": {},
             "metrics": {"error": "selftest: deliberate failure to exercise the failed-stage path",
                         "artifacts": []}, "inputs_hash": None},
        ],
    }
    _write_manifest(out_dir, manifest)                           # exercises the frame-tag enforcement
    print("RECON-IMAGE-SELFTEST-OK (vertices=%d plane_rms=%.4fm, manifest at %s)"
          % (verts.shape[0], rms, out_dir))
    return 0


def main(argv):
    # tolerate a doubled leading `recon` token (docker run k1recon recon --selftest).
    if argv and argv[0] == "recon":
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="recon", description=__doc__.splitlines()[0])
    ap.add_argument("--run", help="run_id (matched against the bundle manifest.json)")
    ap.add_argument("--stages", default="ingest",
                    help="comma list from %s (ingest always runs first)" % ",".join(STAGES))
    ap.add_argument("--allow-approximate-intrinsics", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tag-size-m", type=float, default=0.16,
                    help="printed AprilTag side length in metres (align stage; rig parameter)")
    ap.add_argument("--bundle-dir", default="/in")
    ap.add_argument("--out-dir", default="/out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    digest = os.environ.get("RECON_IMAGE_DIGEST")
    if not digest:
        print("RECON-VERSION-UNPINNED: RECON_IMAGE_DIGEST unset -> recon_version 'unset' (not a "
              "P6G.2-pinned run)")
        digest = "unset"

    if a.selftest:
        return selftest()

    if not a.run:
        ap.error("--run is required (or use --selftest)")       # argparse -> exit 2
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    for s in stages:
        if s == "splat":
            ap.error("stage 'splat' is reserved for image v2 (P6G.4); not in v1")
        if s not in STAGES:
            ap.error("unknown stage %r (valid: %s)" % (s, ",".join(STAGES)))
    return run(a.run, set(stages) | {"ingest"}, a.bundle_dir, a.out_dir, a.seed,
               a.allow_approximate_intrinsics, digest, tag_size_m=a.tag_size_m)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
