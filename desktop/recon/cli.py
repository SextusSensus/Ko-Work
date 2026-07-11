#!/usr/bin/env python3
"""cli.py -- the recon container entrypoint `recon` (charter S3; contract: docs/RECON_CONTRACT.md).

Image v1 is CPU-lean: only `ingest` is implemented; odom/tsdf/simexport/align are registered and exit
`not_implemented` LOUDLY (never a fake ok). Everything here binds to the FROZEN recon_manifest schema +
stage enum in docs/RECON_CONTRACT.md -- do not drift a field without amending that doc (two other
machines code against it).

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
METRIC_STAGES = frozenset(("odom", "tsdf"))
V1_IMPLEMENTED = frozenset(("ingest",))
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


def stage_not_implemented(name, intr_src, allow_approx):
    """v1 stub: loud not_implemented (exit_code 3), distinct from ok/failed; never halts later stages.
    A metric stage records the intrinsics-gate posture it WILL enforce when implemented (contract 5.2)."""
    metrics = {"note": "image v1 stub -- not implemented", "artifacts": []}
    if name in METRIC_STAGES:
        metrics["intrinsics_source"] = intr_src
        metrics["intrinsics_gate"] = ("would-refuse (approximate/missing intrinsics; pass "
                                      "--allow-approximate-intrinsics)" if intr_src != "calibrated"
                                      and not allow_approx else "would-pass")
    return "not_implemented", 3, metrics


# ---- driver -------------------------------------------------------------------------------------
def run(run_id, stages, bundle_dir, out_dir, seed, allow_approx, image_digest):
    os.makedirs(out_dir, exist_ok=True)
    # effective plan: ingest ALWAYS first (its validation gates everything), then requested enum order.
    requested = [s for s in STAGES if s in stages]
    plan = ["ingest"] + [s for s in STAGES if s in requested and s != "ingest"]

    started = _now_utc()
    stage_recs = []
    intr_src = "unknown"
    stopped = False
    for name in plan:
        t0 = time.time()
        if stopped:
            rec = {"name": name, "status": "skipped", "exit_code": None, "wall_s": 0.0,
                   "params": {}, "metrics": {"note": "skipped: an earlier stage failed", "artifacts": []},
                   "inputs_hash": None}
            stage_recs.append(rec)
            continue
        if name == "ingest":
            params = {"depth_min_m": DEPTH_MIN_M, "depth_max_m": DEPTH_MAX_M,
                      "max_pair_skew_s": MAX_PAIR_SKEW_S, "person_mask": "target-box-v0",
                      "mask_dilate_frac": MASK_DILATE_FRAC, "expect_run_id": run_id}
            status, code, metrics, intr_src = stage_ingest(bundle_dir, out_dir, params)
            params.pop("expect_run_id", None)
        else:
            params = {}
            if name in METRIC_STAGES and allow_approx:
                params["allow_approximate_intrinsics"] = True
            status, code, metrics = stage_not_implemented(name, intr_src, allow_approx)
        # inputs_hash (v1) = the bundle hash from its manifest files[].
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
                "intrinsics_source": intr_src, "started_utc": started, "finished_utc": _now_utc()}
    _write_manifest(out_dir, manifest)
    # exit code: 1 if any failed; 3 if none failed but any not_implemented; else 0.
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
               a.allow_approximate_intrinsics, digest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
