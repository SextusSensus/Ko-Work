#!/usr/bin/env python3
"""splat.py -- P6G.4: depth-supervised 3D Gaussian Splatting (gsplat) for the recon image v2.

The sim twin's PHOTOREAL layer: initialize gaussians from the recon mesh/TSDF cloud + odom poses (NO
COLMAP -- doctrine), train photometric (L1 + D-SSIM) + depth-L1, evaluate on HELD-OUT CONTIGUOUS
segments (PSNR/SSIM -- temporal neighbours leak), export a standard 3DGS .ply. GPU-stochastic: gate on
held-out metrics with recorded seed + image digest, NEVER byte-identical (that doctrine is robot-side).

Feature set (P6G.4 complete; each validated by --selftest on a synthetic RaycastingScene):
  * adaptive densification  -- gsplat.strategy.DefaultStrategy (3DGS-paper duplicate/split/prune/reset)
  * joint pose refinement   -- per-TRAIN-frame se3 deltas via torch.matrix_exp, seeded from the odom
                               poses and REFINED, never re-estimated; held-out poses are NOT refined
                               (no test-time optimization -- the eval stays honest)
  * appearance embeddings   -- per-TRAIN-frame affine colour (gain+bias) applied to the RENDER before
                               the loss (absorbs exposure drift); identity at eval
  * sharpness pre-filter    -- Laplacian-variance cut (a walking humanoid smears frames)
  * real-bundle driver      -- `--run`: recon outbox (mesh_visual.ply + trajectory.jsonl) + the capture
                               bundle's .rrd (wall-time-paired, person-masked via geom.prepare_frames)
                               -> train -> held-out metrics -> point_cloud.ply + splat_metrics.json

Verified against gsplat 1.5.3 / torch 2.13+cu126 on the RTX 3080 (sm_86). gsplat JIT-compiles its CUDA
extension on first rasterization (~2 min/container) -- mount a torch_extensions cache to iterate.

  docker run --rm --gpus all k1splat:v2 --smoke                     # rasterization smoke
  docker run --rm --gpus all k1splat:v2 --selftest                  # full-pipeline synthetic gate
  docker run --rm --gpus all -v <bundle>:/in:ro -v <recon>:/recon:ro -v <out>:/out k1splat:v2 \
      --run <run_id>                                                # real bundle (VERIFY ON DESKTOP)
"""
import argparse
import json
import math
import os
import sys

import numpy as np

DEFAULTS = {"iters": 4000, "depth_weight": 0.5, "ssim_weight": 0.2, "seed": 0,
            "holdout_frac": 0.15, "sharp_min": 40.0, "refine_pose": True, "appearance": True,
            "depth_min_m": 0.15, "depth_max_m": 5.0}
# per-parameter-group Adam LRs (3DGS practice): geometry barely moves off a good cloud init;
# opacity/colour/scale carry the appearance fit. Pose/appearance groups are separate (not per-gaussian).
_LRS = {"means": 1.6e-4, "quats": 1e-3, "scales": 5e-3, "opacities": 5e-2, "colors": 1e-2}
_LR_POSE, _LR_APP = 1e-3, 5e-3


# ---- model: gsplat-strategy-compatible ParameterDict ----------------------------------------------
def _knn_scale(means, k=4, cap=0.1):
    """Per-gaussian init scale = mean distance to k nearest neighbours. Chunked cdist bounds memory."""
    import torch
    N = means.shape[0]
    out = torch.empty(N, device=means.device)
    step = 4096
    for i in range(0, N, step):
        d = torch.cdist(means[i:i + step], means)
        knn = d.topk(min(k + 1, N), largest=False).values[:, 1:]
        out[i:i + step] = knn.mean(dim=1)
    return out.clamp(1e-3, cap)


def init_params(points, colors, device, init_opacity=0.5):
    """points [N,3] metres, colors [N,3] in [0,1] -> (ParameterDict, optimizers dict). Keys/spaces
    follow the gsplat DefaultStrategy convention: scales in LOG space, opacities in LOGIT space.
    init_opacity 0.5 (not the paper's 0.1): with a good metric cloud init, denser opacity converges
    faster and densification handles the rest."""
    import torch
    means = torch.as_tensor(np.asarray(points), dtype=torch.float32, device=device)
    cols = torch.as_tensor(np.asarray(colors), dtype=torch.float32, device=device).clamp(0, 1)
    N = means.shape[0]
    quats = torch.zeros(N, 4, device=device); quats[:, 0] = 1.0
    scales = torch.log(_knn_scale(means))[:, None].repeat(1, 3)
    opac = torch.logit(torch.full((N,), float(init_opacity), device=device))
    params = torch.nn.ParameterDict({
        "means": torch.nn.Parameter(means), "quats": torch.nn.Parameter(quats),
        "scales": torch.nn.Parameter(scales), "opacities": torch.nn.Parameter(opac),
        "colors": torch.nn.Parameter(cols)})
    opts = {k: torch.optim.Adam([params[k]], lr=_LRS[k]) for k in params}
    return params, opts


def render(params, viewmat, K, W, H):
    """gsplat rasterization -> (rgb [H,W,3], depth [H,W], info). viewmat = world->camera [4,4]."""
    import torch
    import gsplat
    out, _alpha, info = gsplat.rasterization(
        params["means"], torch.nn.functional.normalize(params["quats"], dim=-1),
        torch.exp(params["scales"]), torch.sigmoid(params["opacities"]), params["colors"],
        viewmat[None], K[None], int(W), int(H), render_mode="RGB+ED",
        packed=False)   # gsplat defaults packed=True; DefaultStrategy is driven with packed=False
    return out[0, :, :, :3], out[0, :, :, 3], info


# ---- sharpness pre-filter --------------------------------------------------------------------------
def sharpness(gray_or_rgb):
    """Laplacian variance (the standard blur metric). Input HxW or HxWx3 uint8."""
    import cv2
    img = gray_or_rgb
    if img.ndim == 3:
        img = cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def filter_sharp(frames, sharp_min):
    """Drop frames below the Laplacian-variance floor. Returns (kept, n_dropped)."""
    kept = [f for f in frames if sharpness(f["rgb"]) >= sharp_min]
    return kept, len(frames) - len(kept)


# ---- pose refinement + appearance ------------------------------------------------------------------
def _hat(x):
    """[N,6] se3 (w, v) -> [N,4,4] twist matrices (batched)."""
    import torch
    N = x.shape[0]
    T = torch.zeros(N, 4, 4, dtype=x.dtype, device=x.device)
    wx, wy, wz = x[:, 0], x[:, 1], x[:, 2]
    T[:, 0, 1], T[:, 0, 2] = -wz, wy
    T[:, 1, 0], T[:, 1, 2] = wz, -wx
    T[:, 2, 0], T[:, 2, 1] = -wy, wx
    T[:, :3, 3] = x[:, 3:]
    return T


def refined_viewmat(base_viewmat, delta6):
    """Left-compose a learnable se3 delta onto a fixed base world->camera matrix (differentiable)."""
    import torch
    return torch.matrix_exp(_hat(delta6[None])[0]) @ base_viewmat


# ---- training ---------------------------------------------------------------------------------------
def _ssim(x, y, win=11, sigma=1.5, C1=0.01 ** 2, C2=0.03 ** 2):
    """Differentiable windowed SSIM on two [H,W,3] images in [0,1] -> scalar mean."""
    import torch
    import torch.nn.functional as F
    x = x.permute(2, 0, 1)[None]; y = y.permute(2, 0, 1)[None]
    coords = torch.arange(win, device=x.device, dtype=x.dtype) - win // 2
    g = torch.exp(-(coords ** 2) / (2 * sigma ** 2)); g = g / g.sum()
    k = (g[:, None] * g[None, :])[None, None].expand(3, 1, win, win)
    blur = lambda t: F.conv2d(t, k, padding=win // 2, groups=3)  # noqa: E731
    mx, my = blur(x), blur(y)
    mx2, my2, mxy = mx * mx, my * my, mx * my
    sx, sy, sxy = blur(x * x) - mx2, blur(y * y) - my2, blur(x * y) - mxy
    return (((2 * mxy + C1) * (2 * sxy + C2)) / ((mx2 + my2 + C1) * (sx + sy + C2))).mean()


def train(params, opts, frames, device, iters=4000, depth_weight=0.5, ssim_weight=0.2, seed=0,
          refine_pose=True, appearance=True, pose_reg=5.0, app_reg=1e-2,
          depth_min=0.15, depth_max=5.0, log_every=500):
    """frames: [{rgb uint8 [H,W,3], depth float32 [H,W] (0=invalid), viewmat [4,4], K [3,3], W, H}].
    Loss = (1-ssim_w)*L1 + ssim_w*(1-SSIM) [+ depth_w*L1(depth) on valid px]. Densification via
    gsplat DefaultStrategy; optional per-frame se3 pose refinement + affine appearance.

    GAUGE ANCHORS (load-bearing): unconstrained pose deltas + free appearance gains have a gauge
    freedom -- the whole scene/exposure drifts to a self-consistent frame that no longer matches the
    UNREFINED held-out cameras (observed live: deltas grew to 4x the injected noise; PSNR fell while
    SSIM rose). pose_reg L2-pins deltas near the odometry seed ('refined, never re-estimated');
    app_reg pins gains/biases near identity so eval-at-identity stays well-defined. Returns stats."""
    import torch
    from gsplat.strategy import DefaultStrategy
    torch.manual_seed(seed)
    n0 = params["means"].shape[0]
    scene_scale = float(torch.linalg.norm(
        params["means"].detach().max(0).values - params["means"].detach().min(0).values) / 2)

    strategy = DefaultStrategy(verbose=False, refine_start_iter=200,
                               refine_stop_iter=int(iters * 0.8), refine_every=100,
                               reset_every=max(iters + 1, 3001))   # opacity reset off for short runs
    strategy.check_sanity(params, opts)
    sstate = strategy.initialize_state(scene_scale=scene_scale)

    n = len(frames)
    base_vm = [torch.as_tensor(f["viewmat"], dtype=torch.float32, device=device) for f in frames]
    pose_delta = torch.zeros(n, 6, device=device, requires_grad=refine_pose)
    app = torch.zeros(n, 6, device=device, requires_grad=appearance)   # (log-gain[3], bias[3]) per frame
    extra = []
    if refine_pose:
        extra.append({"params": [pose_delta], "lr": _LR_POSE})
    if appearance:
        extra.append({"params": [app], "lr": _LR_APP})
    opt_extra = torch.optim.Adam(extra) if extra else None

    order = np.random.default_rng(seed).integers(0, n, size=iters)
    last = 0.0
    for it in range(iters):
        i = int(order[it])
        f = frames[i]
        vm = refined_viewmat(base_vm[i], pose_delta[i]) if refine_pose else base_vm[i]
        K = torch.as_tensor(f["K"], dtype=torch.float32, device=device)
        rgb_gt = torch.as_tensor(f["rgb"], dtype=torch.float32, device=device) / 255.0
        rgb_p, depth_p, info = render(params, vm, K, f["W"], f["H"])
        if appearance:
            rgb_p = rgb_p * torch.exp(app[i, :3]) + app[i, 3:]
        l1 = (rgb_p - rgb_gt).abs().mean()
        loss = (1 - ssim_weight) * l1 + ssim_weight * (1 - _ssim(rgb_p.clamp(0, 1), rgb_gt))
        d_gt = torch.as_tensor(f["depth"], dtype=torch.float32, device=device)
        # Supervise ONLY within the mesh's fusion range: far returns on this depth rig are noise (seen
        # to 65 m in a garage) and the TSDF init already truncated at depth_max, so unbounded depth-L1
        # drags gaussians to phantom far surfaces the init never had -> floaters that wreck novel views.
        m = (d_gt > depth_min) & (d_gt < depth_max)
        if depth_weight > 0 and m.any():
            loss = loss + depth_weight * (depth_p[m] - d_gt[m]).abs().mean()
        if refine_pose:
            loss = loss + pose_reg * pose_delta[i].square().sum()      # gauge anchor: stay near odom
        if appearance:
            loss = loss + app_reg * app[i].square().sum()              # gauge anchor: stay near identity

        strategy.step_pre_backward(params, opts, sstate, it, info)
        for o in opts.values():
            o.zero_grad(set_to_none=True)
        if opt_extra:
            opt_extra.zero_grad(set_to_none=True)
        loss.backward()
        strategy.step_post_backward(params, opts, sstate, it, info, packed=False)
        for o in opts.values():
            o.step()
        if opt_extra:
            opt_extra.step()
        with torch.no_grad():
            params["colors"].clamp_(0, 1)
        last = float(loss.detach())
        if log_every and (it + 1) % log_every == 0:
            print("  iter %d/%d loss=%.4f gaussians=%d" % (it + 1, iters, last, params["means"].shape[0]))

    return {"final_loss": round(last, 5), "gaussians_init": int(n0),
            "gaussians_final": int(params["means"].shape[0]),
            "pose_delta_max_m": (float(pose_delta[:, 3:].detach().norm(dim=1).max()) if refine_pose else 0.0)}


# ---- held-out eval ----------------------------------------------------------------------------------
def eval_heldout(params, frames, device):
    """PSNR/SSIM over held-out frames (a CONTIGUOUS tail segment; poses NOT refined, appearance
    identity -- honest novel-view numbers). Returns (mean_psnr, mean_ssim)."""
    import torch
    from skimage.metrics import peak_signal_noise_ratio as psnr
    from skimage.metrics import structural_similarity as ssim
    ps, ss = [], []
    with torch.no_grad():
        for f in frames:
            vm = torch.as_tensor(f["viewmat"], dtype=torch.float32, device=device)
            K = torch.as_tensor(f["K"], dtype=torch.float32, device=device)
            rgb_p, _d, _i = render(params, vm, K, f["W"], f["H"])
            pred = rgb_p.clamp(0, 1).cpu().numpy()
            gt = f["rgb"].astype(np.float32) / 255.0
            ps.append(psnr(gt, pred, data_range=1.0))
            ss.append(ssim(gt, pred, data_range=1.0, channel_axis=2))
    return float(np.mean(ps)), float(np.mean(ss))


# ---- export -----------------------------------------------------------------------------------------
def export_ply(params, path):
    """Standard 3DGS .ply (xyz, f_dc_0..2, opacity, scale_0..2, rot_0..3) -- viewer-compatible."""
    import torch
    with torch.no_grad():
        means = params["means"].detach().cpu().numpy()
        opac = params["opacities"].detach().cpu().numpy()               # logit space (3DGS convention)
        scales = params["scales"].detach().cpu().numpy()                # log space  (3DGS convention)
        quats = torch.nn.functional.normalize(params["quats"], dim=-1).detach().cpu().numpy()
        colors = params["colors"].detach().clamp(0, 1).cpu().numpy()
    N = means.shape[0]
    C0 = 0.28209479177387814
    f_dc = (colors - 0.5) / C0
    with open(path, "wb") as f:
        f.write(("ply\nformat binary_little_endian 1.0\nelement vertex %d\n"
                 "property float x\nproperty float y\nproperty float z\n"
                 "property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n"
                 "property float opacity\nproperty float scale_0\nproperty float scale_1\n"
                 "property float scale_2\nproperty float rot_0\nproperty float rot_1\n"
                 "property float rot_2\nproperty float rot_3\nend_header\n" % N).encode("ascii"))
        arr = np.concatenate([means, f_dc, opac[:, None], scales, quats], axis=1).astype("<f4")
        f.write(arr.tobytes())
    return N


# ---- real-bundle driver (--run) ----------------------------------------------------------------------
def run_bundle(run_id, bundle_dir, recon_dir, out_dir, cfg):
    """The production path: capture bundle (RGBD from the .rrd, wall-time-paired + person-masked via
    geom.prepare_frames) + recon outputs (mesh_visual.ply = the init cloud; trajectory.jsonl = poses)
    -> train -> held-out contiguous metrics -> point_cloud.ply + splat_metrics.json. run_local frame
    (map once P8.3 aligns runs). VERIFY ON DESKTOP against a real garage bundle."""
    import torch
    import open3d as o3d
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import geom
    for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eval"),
                 "/app/eval"):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    from rrd_to_lerobot import read_rrd_frames

    device = torch.device("cuda")
    os.makedirs(out_dir, exist_ok=True)

    # 1. frames: wall-time pairing + target-box person masking (doctrine 4+5, reused from geom)
    rrd = next((os.path.join(bundle_dir, n) for n in sorted(os.listdir(bundle_dir))
                if n.endswith(".rrd")), None)
    if rrd is None:
        raise SystemExit("--run: no .rrd in %s" % bundle_dir)
    rgb_stream, depth_stream = read_rrd_frames(rrd)
    frames, fstats = geom.prepare_frames(rgb_stream, depth_stream)
    with open(os.path.join(bundle_dir, "intrinsics.json")) as f:
        intr = json.load(f)
    K = np.array([[intr["fx"], 0, intr["cx"]], [0, intr["fy"], intr["cy"]], [0, 0, 1]])

    # 2. poses: the odom trajectory, joined to frames by wall_t (same pairing produced both)
    traj = {}
    with open(os.path.join(recon_dir, "trajectory.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            traj[round(r["wall_t"], 4)] = np.asarray(r["T_run_local_camera"], dtype=np.float64)
    matched = []
    for fr in frames:
        T = traj.get(round(fr["t"], 4))
        if T is None:
            continue
        matched.append({"rgb": fr["rgb"], "depth": fr["depth"], "viewmat": np.linalg.inv(T),
                        "K": K, "W": fr["rgb"].shape[1], "H": fr["rgb"].shape[0]})
    if len(matched) < 8:
        raise SystemExit("--run: only %d frames matched trajectory poses (need >= 8)" % len(matched))

    # 3. sharpness pre-filter (walking smears frames), then a CONTIGUOUS held-out tail
    kept, dropped = filter_sharp(matched, cfg["sharp_min"])
    if len(kept) < 8:
        print("SPLAT-NOTE: sharpness floor %.0f left %d frames; keeping all (floor skipped, recorded)"
              % (cfg["sharp_min"], len(kept)))
        kept, dropped = matched, 0
    n_hold = max(2, int(len(kept) * cfg["holdout_frac"]))
    train_fr, held = kept[:-n_hold], kept[-n_hold:]

    # 4. init cloud from the recon visual mesh (the TSDF surface -- co-registered by construction)
    mesh = o3d.io.read_triangle_mesh(os.path.join(recon_dir, "mesh_visual.ply"))
    pts = np.asarray(mesh.vertices)
    cols = np.asarray(mesh.vertex_colors) if len(mesh.vertex_colors) else np.full_like(pts, 0.5)
    if pts.shape[0] > 300_000:                                  # cap init size; densification grows it
        sel = np.random.default_rng(cfg["seed"]).choice(pts.shape[0], 300_000, replace=False)
        pts, cols = pts[sel], cols[sel]
    params, opts = init_params(pts, cols, device)

    # 5. train -> eval -> export
    tstats = train(params, opts, train_fr, device, iters=cfg["iters"],
                   depth_weight=cfg["depth_weight"], ssim_weight=cfg["ssim_weight"],
                   seed=cfg["seed"], refine_pose=cfg["refine_pose"], appearance=cfg["appearance"],
                   depth_min=cfg["depth_min_m"], depth_max=cfg["depth_max_m"])
    p, s = eval_heldout(params, held, device)
    n = export_ply(params, os.path.join(out_dir, "point_cloud.ply"))
    metrics = {"run_id": run_id, "frame": "run_local",
               "image_digest": os.environ.get("RECON_IMAGE_DIGEST",
                                              os.environ.get("JOB_IMAGE_DIGEST", "unset")),
               "seed": cfg["seed"], "frames_train": len(train_fr), "frames_heldout": len(held),
               "frames_dropped_blur": dropped, "pairing": fstats,
               "heldout_psnr_db": round(p, 2), "heldout_ssim": round(s, 4),
               "gaussians": n, "train": tstats, "config": cfg}
    with open(os.path.join(out_dir, "splat_metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2, sort_keys=True)
    print("SPLAT-RUN-OK run=%s heldout PSNR=%.2f dB SSIM=%.3f gaussians=%d (train %d / held %d, "
          "blur-dropped %d)" % (run_id, p, s, n, len(train_fr), len(held), dropped))
    return 0


# ---- synthetic selftest -------------------------------------------------------------------------------
def _smooth_rgb(pts_world):
    """Smooth low-frequency world-coloured texture (representative of real surfaces; geom's hard
    checker exists for photometric ODOMETRY and is adversarial for splats -- texture, not pipeline)."""
    p = pts_world
    r = 0.5 + 0.4 * np.sin(p[..., 0] * 1.1)
    g = 0.5 + 0.4 * np.sin(p[..., 1] * 1.1 + 2.0)
    b = 0.5 + 0.4 * np.cos(p[..., 2] * 1.4 + 1.0)
    return np.clip(np.stack([r, g, b], -1) * 255.0, 0, 255).astype(np.uint8)


def _backproject(frame, stride=2):
    rgb, depth = frame["rgb"], frame["depth"]
    H, W = depth.shape
    K = np.asarray(frame["K"]); T_wc = np.linalg.inv(np.asarray(frame["viewmat"]))
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    ys, xs = np.mgrid[0:H:stride, 0:W:stride]
    d = depth[ys, xs]
    m = d > 0
    xs, ys, d = xs[m], ys[m], d[m]
    cam = np.stack([(xs - cx) * d / fx, (ys - cy) * d / fy, d, np.ones_like(d)], axis=0)
    world = (T_wc @ cam)[:3].T
    return world, rgb[ys, xs].astype(np.float32) / 255.0


def _smoke():
    import torch, gsplat  # noqa: E401
    print("torch", torch.__version__, "| cuda", torch.cuda.is_available(),
          "| gsplat", getattr(gsplat, "__version__", "?"))
    if not torch.cuda.is_available():
        print("SPLAT-SMOKE-FAIL: no CUDA (run with --gpus all)"); return 1
    dev = torch.device("cuda")
    g = torch.Generator().manual_seed(0)
    pts = (torch.rand(2000, 3, generator=g) * 2 - 1); pts[:, 2] += 4
    params, _ = init_params(pts.numpy(), torch.rand(2000, 3, generator=g).numpy(), dev)
    K = torch.tensor([[100., 0, 64], [0, 100., 64], [0, 0, 1]], device=dev)
    rgb, _d, _i = render(params, torch.eye(4, device=dev), K, 128, 128)
    assert rgb.shape == (128, 128, 3) and torch.isfinite(rgb).all()
    print("render ok:", tuple(rgb.shape), "SPLAT-SMOKE-OK"); return 0


def _selftest():
    import cv2
    import torch
    import open3d as o3d
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import geom
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("SELFTEST device:", dev, "-", (torch.cuda.get_device_name(0) if dev.type == "cuda" else "cpu"))
    W = H = 160
    fx = fy = 120.0
    Km = np.array([[fx, 0, W / 2.0], [0, fy, H / 2.0], [0, 0, 1]], dtype=np.float64)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(geom._synth_scene()))
    gt = geom._traj_there_and_back(20)
    rng = np.random.default_rng(0)

    frames = []
    for T in gt:
        rgb, depth, _hit = geom._render_rgbd(scene, T, Km, W, H, color_fn=_smooth_rgb)
        frames.append({"rgb": rgb, "depth": depth, "viewmat": np.linalg.inv(T),
                       "K": Km, "W": W, "H": H})

    # (1) sharpness filter: a deliberately blurred frame is dropped, sharp ones kept
    blurred = dict(frames[0]); blurred["rgb"] = cv2.GaussianBlur(frames[0]["rgb"], (31, 31), 8)
    kept, dropped = filter_sharp(frames + [blurred], sharp_min=40.0)
    assert dropped >= 1 and len(kept) >= len(frames) - 1, "sharpness filter failed (%d dropped)" % dropped
    print("  sharpness filter: dropped %d blurred frame(s), kept %d" % (dropped, len(kept)))

    train_fr, held = frames[:16], frames[16:]
    # exposure jitter on TRAIN frames (what appearance embeddings must absorb; held-out stays clean)
    for k, f in enumerate(train_fr):
        gain = 0.6 + 0.8 * rng.random()                 # 0.6-1.4x, mean ~1 (identity is the gauge)
        f["rgb"] = np.clip(f["rgb"].astype(np.float32) * gain, 0, 255).astype(np.uint8)
    # pose noise on TRAIN viewmats (what joint refinement must recover; held-out poses exact).
    # 0.03 rad (~1.7 deg) / 0.05 m -- odometry-grade error, enough to visibly hurt a frozen baseline.
    noisy = []
    for f in train_fr:
        d = np.eye(4)
        ang = rng.normal(0, 0.03, 3); tr = rng.normal(0, 0.05, 3)
        Rm, _ = cv2.Rodrigues(ang)
        d[:3, :3] = Rm; d[:3, 3] = tr
        g2 = dict(f); g2["viewmat"] = d @ f["viewmat"]
        noisy.append(g2)

    pts, cols = [], []
    for f in noisy[::2]:
        p, c = _backproject(f, stride=1)
        pts.append(p); cols.append(c)
    pts = np.concatenate(pts); cols = np.concatenate(cols)
    print("  init cloud: %d points" % pts.shape[0])

    def run(refine, appear, tag):
        params, opts = init_params(pts, cols, dev)
        st = train(params, opts, noisy, dev, iters=1500, depth_weight=0.5, seed=0,
                   refine_pose=refine, appearance=appear, log_every=0)
        p, s = eval_heldout(params, held, dev)
        print("  [%s] heldout PSNR=%.2f SSIM=%.3f gaussians %d->%d pose_delta_max=%.3fm"
              % (tag, p, s, st["gaussians_init"], st["gaussians_final"], st["pose_delta_max_m"]))
        return p, s, st, params

    p_off, s_off, st_off, _ = run(False, False, "frozen poses, no appearance")
    p_on, s_on, st_on, params = run(True, True, "refined + appearance")

    # (2) densification actually acted (count changed) in at least one run
    assert (st_on["gaussians_final"] != st_on["gaussians_init"]
            or st_off["gaussians_final"] != st_off["gaussians_init"]), "densification never fired"
    # (3) What refinement+appearance actually guarantee under a depth anchor (measured, not hoped):
    # depth supervision already pins METRIC geometry through the noisy poses, so the refinement win
    # shows up as STRUCTURE (less ghosting/blur = SSIM), while PSNR must merely not regress (the
    # gauge anchors' job). Both observed consistently across rounds; asserted as such.
    assert s_on > s_off + 0.005, "refined SSIM %.3f did not beat frozen %.3f" % (s_on, s_off)
    assert p_on > p_off - 1.0, "refined PSNR %.2f regressed >1 dB vs frozen %.2f (gauge drift?)" % (p_on, p_off)
    # (3b) the pose prior held: deltas stay odometry-scale, never re-estimation-scale
    assert st_on["pose_delta_max_m"] < 0.10, "pose deltas %.3f m escaped the prior" % st_on["pose_delta_max_m"]
    # (4) absolute floor: a broken pipeline lands ~8-12 dB; sanity-reconstruction under noise
    assert p_on > 24.0 and s_on > 0.95, "heldout PSNR=%.2f SSIM=%.3f below floor" % (p_on, s_on)

    import tempfile
    ply = os.path.join(tempfile.mkdtemp(prefix="splat-"), "point_cloud.ply")
    npts = export_ply(params, ply)
    assert npts > 0 and os.path.getsize(ply) > 0
    print("  exported %d gaussians -> %s" % (npts, ply))
    print("SPLAT-SELFTEST-OK")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="splat", description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="gsplat + CUDA rasterization smoke")
    ap.add_argument("--selftest", action="store_true", help="synthetic end-to-end feature gate")
    ap.add_argument("--run", help="run_id: real bundle at --bundle-dir + recon at --recon-dir")
    ap.add_argument("--bundle-dir", default="/in")
    ap.add_argument("--recon-dir", default="/recon")
    ap.add_argument("--out-dir", default="/out")
    for k, v in DEFAULTS.items():
        if isinstance(v, bool):
            ap.add_argument("--" + k.replace("_", "-"), type=int, choices=(0, 1), default=int(v))
        else:
            ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    a = ap.parse_args(argv)
    if a.smoke:
        return _smoke()
    if a.selftest:
        return _selftest()
    if a.run:
        cfg = {k: (bool(getattr(a, k)) if isinstance(DEFAULTS[k], bool) else getattr(a, k))
               for k in DEFAULTS}
        return run_bundle(a.run, a.bundle_dir, a.recon_dir, a.out_dir, cfg)
    ap.error("one of --smoke | --selftest | --run is required")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
