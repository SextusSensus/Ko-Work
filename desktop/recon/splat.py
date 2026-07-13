#!/usr/bin/env python3
"""splat.py -- P6G.4: depth-supervised 3D Gaussian Splatting (gsplat) for the recon image v2.

The sim twin's PHOTOREAL layer: initialize gaussians from P6G.2's TSDF cloud + odom/align poses (NO
COLMAP -- doctrine), train with photometric (L1) + depth (L1) loss along the trajectory, evaluate on
HELD-OUT CONTIGUOUS segments (PSNR/SSIM -- temporal neighbors leak, never interleaved), export .ply in
the run/map frame co-registered with the mesh. GPU-stochastic: gate on held-out metrics with recorded
seed + image digest, NEVER byte-identical (that doctrine stays robot-side).

Verified against gsplat 1.5.3 / torch 2.13+cu126 on the RTX 3080 (sm_86). The gsplat CUDA extension
JIT-compiles on first rasterization (~115 s/container -- mount a torch_extensions cache to iterate fast).

  docker run --rm --gpus all k1splat:v2 --smoke                 # gsplat/CUDA rasterization smoke
  docker run --rm --gpus all k1splat:v2 --selftest              # synthetic end-to-end -> SPLAT-SELFTEST-OK

STILL DEFERRED (flagged, next increment): joint POSE REFINEMENT (odom poses are geometric- not
photometric-grade), per-image APPEARANCE EMBEDDINGS (exposure drift), SHARPNESS pre-filter (walking
smears frames), adaptive DENSIFICATION, dilated PERSON MASKS (reuse geom masks), and the real-bundle
`--run` driver (init from mesh_visual.ply + trajectory.jsonl). Real garage splat is data-blocked on P8.3.
"""
import argparse
import math
import sys

import numpy as np


# ---- model ---------------------------------------------------------------------------------------
def _knn_scale(means, k=4, cap=0.1):
    """Per-gaussian init scale = mean distance to k nearest neighbours (isotropic). Chunked cdist to
    bound memory. Returns [N] float32 (metres)."""
    import torch
    N = means.shape[0]
    out = torch.empty(N, device=means.device)
    step = 4096
    for i in range(0, N, step):
        d = torch.cdist(means[i:i + step], means)          # [b, N]
        knn = d.topk(min(k + 1, N), largest=False).values[:, 1:]   # drop self
        out[i:i + step] = knn.mean(dim=1)
    return out.clamp(1e-3, cap)


class SplatModel:
    """Gaussian params as leaf tensors (means, quats, log-scales, logit-opacities, RGB). Plain RGB (no
    SH) -- the follow camera is a single fixed head cam; view-dependent colour buys little here."""
    def __init__(self, means, colors, device):
        import torch
        self.means = means.to(device).clone().requires_grad_(True)
        q = torch.zeros(means.shape[0], 4, device=device); q[:, 0] = 1.0     # identity (w,x,y,z)
        self.quats = q.requires_grad_(True)
        s = _knn_scale(means.to(device))
        self.log_scales = torch.log(s[:, None].repeat(1, 3)).requires_grad_(True)
        # init opacity 0.5 (not 3DGS's 0.1): WITHOUT adaptive densification (deferred), faint gaussians
        # give thin coverage -- a denser opacity floor fills the surface off a good cloud init.
        self.logit_opacities = torch.logit(torch.full((means.shape[0],), 0.5, device=device)).requires_grad_(True)
        self.colors = colors.to(device).clone().clamp(0, 1).requires_grad_(True)

    def params(self):
        return [self.means, self.quats, self.log_scales, self.logit_opacities, self.colors]

    def render(self, viewmat, K, W, H):
        """gsplat rasterization -> (rgb [H,W,3], depth [H,W]). viewmat = world->camera [4,4]."""
        import torch
        import gsplat
        out = gsplat.rasterization(
            self.means, torch.nn.functional.normalize(self.quats, dim=-1),
            torch.exp(self.log_scales), torch.sigmoid(self.logit_opacities), self.colors,
            viewmat[None], K[None], int(W), int(H), render_mode="RGB+ED")
        render = out[0] if isinstance(out, (tuple, list)) else out             # [1,H,W,4]
        return render[0, :, :, :3], render[0, :, :, 3]

    def to_ply_arrays(self):
        import torch
        with torch.no_grad():
            return (self.means.detach().cpu().numpy(),
                    torch.sigmoid(self.logit_opacities).detach().cpu().numpy(),
                    torch.exp(self.log_scales).detach().cpu().numpy(),
                    torch.nn.functional.normalize(self.quats, dim=-1).detach().cpu().numpy(),
                    self.colors.detach().clamp(0, 1).cpu().numpy())


# ---- init from a point cloud (the real path: TSDF cloud; the selftest: backprojected RGBD) --------
def init_from_cloud(points, colors, device):
    """points [N,3] world/run_local metres, colors [N,3] in [0,1] -> a SplatModel."""
    import torch
    return SplatModel(torch.as_tensor(np.asarray(points), dtype=torch.float32),
                      torch.as_tensor(np.asarray(colors), dtype=torch.float32), device)


# ---- training ------------------------------------------------------------------------------------
# per-parameter-group Adam LRs (3DGS practice): geometry (means/quats) barely moves off a good cloud
# init; opacity/colour/scale carry the appearance fit. A single global LR badly under-fits colour.
_LRS = {"means": 1e-4, "quats": 1e-3, "log_scales": 5e-3, "logit_opacities": 5e-2, "colors": 1e-2}


def _ssim(x, y, win=11, sigma=1.5, C1=0.01 ** 2, C2=0.03 ** 2):
    """Differentiable windowed SSIM on two [H,W,3] images in [0,1] -> scalar mean."""
    import torch
    import torch.nn.functional as F
    x = x.permute(2, 0, 1)[None]; y = y.permute(2, 0, 1)[None]            # [1,3,H,W]
    coords = torch.arange(win, device=x.device, dtype=x.dtype) - win // 2
    g = torch.exp(-(coords ** 2) / (2 * sigma ** 2)); g = g / g.sum()
    k = (g[:, None] * g[None, :])[None, None].expand(3, 1, win, win)      # [3,1,win,win]
    blur = lambda t: F.conv2d(t, k, padding=win // 2, groups=3)
    mx, my = blur(x), blur(y)
    mx2, my2, mxy = mx * mx, my * my, mx * my
    sx, sy, sxy = blur(x * x) - mx2, blur(y * y) - my2, blur(x * y) - mxy
    smap = ((2 * mxy + C1) * (2 * sxy + C2)) / ((mx2 + my2 + C1) * (sx + sy + C2))
    return smap.mean()


def train(model, frames, device, iters=1000, depth_weight=0.5, ssim_weight=0.2, seed=0, log_every=100):
    """frames: list of {rgb [H,W,3] uint8, depth [H,W] float32 (0=invalid), viewmat [4,4], K [3,3], W, H}.
    Loss = (1-ssim_weight)*L1(rgb) + ssim_weight*(1-SSIM) + depth_weight*L1(depth) on valid pixels.
    Photometric + depth-supervised; NO pose refinement / densification yet (flagged in the docstring)."""
    import torch
    torch.manual_seed(seed)
    opt = torch.optim.Adam([{"params": [getattr(model, n)], "lr": lr} for n, lr in _LRS.items()])
    order = np.random.default_rng(seed).integers(0, len(frames), size=iters)
    last = 0.0
    for it in range(iters):
        f = frames[int(order[it])]
        vm = torch.as_tensor(f["viewmat"], dtype=torch.float32, device=device)
        K = torch.as_tensor(f["K"], dtype=torch.float32, device=device)
        rgb_gt = torch.as_tensor(f["rgb"], dtype=torch.float32, device=device) / 255.0
        rgb_p, depth_p = model.render(vm, K, f["W"], f["H"])
        l1 = (rgb_p - rgb_gt).abs().mean()
        loss = (1 - ssim_weight) * l1 + ssim_weight * (1 - _ssim(rgb_p.clamp(0, 1), rgb_gt))
        d_gt = torch.as_tensor(f["depth"], dtype=torch.float32, device=device)
        m = d_gt > 0
        if m.any():
            loss = loss + depth_weight * (depth_p[m] - d_gt[m]).abs().mean()
        opt.zero_grad(); loss.backward(); opt.step()
        with torch.no_grad():                              # keep params in valid ranges
            model.colors.clamp_(0, 1)
        last = float(loss.detach())
        if log_every and (it + 1) % log_every == 0:
            print("  iter %d/%d loss=%.4f" % (it + 1, iters, last))
    return last


# ---- held-out eval -------------------------------------------------------------------------------
def eval_heldout(model, frames, device):
    """PSNR/SSIM over held-out frames (caller passes a CONTIGUOUS segment). Returns (mean_psnr, mean_ssim)."""
    import torch
    from skimage.metrics import peak_signal_noise_ratio as psnr
    from skimage.metrics import structural_similarity as ssim
    ps, ss = [], []
    with torch.no_grad():
        for f in frames:
            vm = torch.as_tensor(f["viewmat"], dtype=torch.float32, device=device)
            K = torch.as_tensor(f["K"], dtype=torch.float32, device=device)
            rgb_p, _ = model.render(vm, K, f["W"], f["H"])
            pred = rgb_p.clamp(0, 1).cpu().numpy()
            gt = f["rgb"].astype(np.float32) / 255.0
            ps.append(psnr(gt, pred, data_range=1.0))
            ss.append(ssim(gt, pred, data_range=1.0, channel_axis=2))
    return float(np.mean(ps)), float(np.mean(ss))


# ---- export --------------------------------------------------------------------------------------
def export_ply(model, path):
    """Write the standard 3DGS .ply (x,y,z, f_dc_0..2 as SH DC, opacity, scale_0..2, rot_0..3).
    Colours stored as SH band-0 DC so common 3DGS viewers render them."""
    means, opac, scales, quats, colors = model.to_ply_arrays()
    N = means.shape[0]
    C0 = 0.28209479177387814                              # SH band-0 constant
    f_dc = (colors - 0.5) / C0
    with open(path, "wb") as f:
        hdr = ("ply\nformat binary_little_endian 1.0\nelement vertex %d\n"
               "property float x\nproperty float y\nproperty float z\n"
               "property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n"
               "property float opacity\nproperty float scale_0\nproperty float scale_1\n"
               "property float scale_2\nproperty float rot_0\nproperty float rot_1\n"
               "property float rot_2\nproperty float rot_3\nend_header\n" % N)
        f.write(hdr.encode("ascii"))
        opac_logit = np.log(np.clip(opac, 1e-6, 1 - 1e-6) / (1 - np.clip(opac, 1e-6, 1 - 1e-6)))
        arr = np.concatenate([means, f_dc, opac_logit[:, None], np.log(scales), quats],
                             axis=1).astype("<f4")
        f.write(arr.tobytes())
    return N


# ---- synthetic selftest (RaycastingScene, reused from geom) --------------------------------------
def _smooth_rgb(pts_world):
    """A SMOOTH low-frequency world-coloured texture for the splat selftest -- representative of real
    surfaces (walls/floor), unlike geom's hard checker (which exists for photometric ODOMETRY). Smooth
    gradients are what an isotropic no-densification splat can faithfully reconstruct, so held-out PSNR
    reflects the PIPELINE working, not the texture fighting it. View-consistent (a function of world xyz)."""
    p = pts_world
    r = 0.5 + 0.4 * np.sin(p[..., 0] * 1.1)
    g = 0.5 + 0.4 * np.sin(p[..., 1] * 1.1 + 2.0)
    b = 0.5 + 0.4 * np.cos(p[..., 2] * 1.4 + 1.0)
    return np.clip(np.stack([r, g, b], -1) * 255.0, 0, 255).astype(np.uint8)


def _backproject(frame, stride=2):
    """RGBD frame -> world points + colours (mimics the TSDF cloud the real path gets from P6G.2)."""
    rgb, depth = frame["rgb"], frame["depth"]
    H, W = depth.shape
    K = np.asarray(frame["K"]); T_wc = np.linalg.inv(np.asarray(frame["viewmat"]))
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    ys, xs = np.mgrid[0:H:stride, 0:W:stride]
    d = depth[ys, xs]
    m = d > 0
    xs, ys, d = xs[m], ys[m], d[m]
    cam = np.stack([(xs - cx) * d / fx, (ys - cy) * d / fy, d, np.ones_like(d)], axis=0)   # [4,N]
    world = (T_wc @ cam)[:3].T
    col = rgb[ys, xs].astype(np.float32) / 255.0
    return world, col


def _smoke():
    import torch, gsplat
    print("torch", torch.__version__, "| cuda", torch.cuda.is_available(), "| gsplat", getattr(gsplat, "__version__", "?"))
    if not torch.cuda.is_available():
        print("SPLAT-SMOKE-FAIL: no CUDA (run with --gpus all)"); return 1
    dev = torch.device("cuda")
    g = torch.Generator().manual_seed(0)
    means = (torch.rand(2000, 3, generator=g) * 2 - 1).to(dev); means[:, 2] += 4
    m = SplatModel(means, torch.rand(2000, 3, generator=g).to(dev), dev)
    K = torch.tensor([[100., 0, 64], [0, 100., 64], [0, 0, 1]], device=dev)
    rgb, depth = m.render(torch.eye(4, device=dev), K, 128, 128)
    assert rgb.shape == (128, 128, 3) and torch.isfinite(rgb).all()
    print("render ok:", tuple(rgb.shape), "SPLAT-SMOKE-OK"); return 0


def _selftest():
    import torch
    import geom                                            # /app/recon/geom.py (in the image)
    import open3d as o3d
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("SELFTEST device:", dev, "-", (torch.cuda.get_device_name(0) if dev.type == "cuda" else "cpu"))
    W = H = 160
    fx = fy = 120.0
    K = np.array([[fx, 0, W / 2.0], [0, fy, H / 2.0], [0, 0, 1]], dtype=np.float64)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(geom._synth_scene()))
    gt = geom._traj_there_and_back(20)
    frames = []
    for T in gt:
        rgb, depth, hit = geom._render_rgbd(scene, T, K, W, H, color_fn=_smooth_rgb)
        frames.append({"rgb": rgb, "depth": depth, "viewmat": np.linalg.inv(T), "K": K, "W": W, "H": H})
    # held-out = a CONTIGUOUS tail segment (temporal neighbours leak if interleaved)
    train_fr, held = frames[:16], frames[16:]

    # init cloud by backprojecting TRAIN frames (mimics the P6G.2 TSDF cloud)
    pts, cols = [], []
    for f in train_fr[::2]:
        p, c = _backproject(f, stride=1)
        pts.append(p); cols.append(c)
    pts = np.concatenate(pts); cols = np.concatenate(cols)
    print("init cloud: %d points" % pts.shape[0])
    model = init_from_cloud(pts, cols, dev)

    train(model, train_fr, dev, iters=1500, depth_weight=0.5, seed=0, log_every=500)
    psnr, ssim = eval_heldout(model, held, dev)
    print("HELD-OUT: PSNR=%.2f dB  SSIM=%.3f (n=%d contiguous)" % (psnr, ssim, len(held)))
    # Pipeline-mechanics + sanity-reconstruction gate on a smooth synthetic scene (NOT the real-quality
    # gate -- that's held-out PSNR on the real garage bundle with densification, data-blocked). A broken
    # pipeline (misaligned render / no learning) lands ~8-12 dB; this floor catches that.
    assert psnr > 24.0 and ssim > 0.80, "held-out PSNR=%.2f SSIM=%.3f below floor (splat broken)" % (psnr, ssim)

    import tempfile, os
    ply = os.path.join(tempfile.mkdtemp(prefix="splat-"), "point_cloud.ply")
    n = export_ply(model, ply)
    assert n > 0 and os.path.getsize(ply) > 0, "empty .ply"
    print("exported %d gaussians -> %s (%d bytes)" % (n, ply, os.path.getsize(ply)))
    print("SPLAT-SELFTEST-OK")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="splat", description=__doc__.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="gsplat + CUDA rasterization smoke")
    ap.add_argument("--selftest", action="store_true", help="synthetic end-to-end (init->train->PSNR->export)")
    ap.add_argument("--run", help="run_id (real recon bundle) -- full driver, next increment")
    a = ap.parse_args(argv)
    if a.smoke:
        return _smoke()
    if a.selftest:
        return _selftest()
    print("splat.py: --smoke | --selftest wired; the real-bundle --run driver is the next increment.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
