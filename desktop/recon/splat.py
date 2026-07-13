#!/usr/bin/env python3
"""splat.py -- P6G.4: depth-supervised 3D Gaussian Splatting (gsplat) for the recon image v2.

The sim twin's PHOTOREAL layer: initialize gaussians from P6G.2's TSDF cloud + odom/align poses (NO
COLMAP -- doctrine), train with photometric + depth loss, refine poses jointly (odometry poses are
geometric- not photometric-grade), evaluate on HELD-OUT contiguous segments (PSNR/SSIM), export
.ply/.splat IN THE MAP FRAME co-registered with the mesh. GPU-stochastic: gate on held-out metrics
with recorded seed + image digest, NEVER byte-identical (the byte-identical doctrine stays robot-side).

STATUS (2026-07-13, this build): v0 == the IMAGE + gsplat SMOKE only (`--selftest` -> SPLAT-SMOKE-OK),
proving the CUDA-devel image builds, gsplat compiles for sm_86, and rasterization runs on the 3080.
The full pipeline (init_from_cloud / train / eval_heldout / export) is stubbed LOUDLY below and authored
next against the live gsplat API in-container (same discipline as geom.py -- never blind-author code that
needs an interpreter to smoke). Real garage splat is data-blocked on P8.3 aligned multi-run data.
"""
import argparse
import sys


def _smoke():
    """Prove the image + gsplat + CUDA rasterization work: build a few random 3D gaussians, render an
    RGB+depth image from a pinhole camera, assert finite output of the right shape. No data, no training."""
    import numpy as np
    import torch
    import gsplat
    print("torch", torch.__version__, "| cuda_available", torch.cuda.is_available(),
          "| gsplat", getattr(gsplat, "__version__", "?"))
    if not torch.cuda.is_available():
        print("SPLAT-SMOKE-FAIL: no CUDA device visible (run with --gpus all)"); return 1
    dev = torch.device("cuda")
    print("device:", torch.cuda.get_device_name(0))

    g = torch.Generator(device="cpu").manual_seed(0)
    N, W, H = 2000, 128, 128
    means = (torch.rand(N, 3, generator=g) * 2 - 1).to(dev)                 # in a [-1,1]^3 box
    means[:, 2] += 4.0                                                      # push in front of the camera
    quats = torch.zeros(N, 4, device=dev); quats[:, 0] = 1.0               # identity rotation (w,x,y,z)
    scales = (torch.ones(N, 3, device=dev) * 0.03)
    opacities = torch.ones(N, device=dev) * 0.5
    colors = torch.rand(N, 3, generator=g).to(dev)                         # per-gaussian RGB

    f = 100.0
    K = torch.tensor([[[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]]], device=dev)   # [1,3,3]
    viewmat = torch.eye(4, device=dev).unsqueeze(0)                            # world->camera = identity

    out = gsplat.rasterization(means, quats, scales, opacities, colors,
                               viewmat, K, W, H, render_mode="RGB+ED")
    render = out[0] if isinstance(out, (tuple, list)) else out                 # [1,H,W,4] (RGB + depth)
    render = render.detach().float().cpu().numpy()
    assert render.ndim == 4 and render.shape[1:3] == (H, W), "unexpected render shape %s" % (render.shape,)
    rgb = render[0, :, :, :3]
    assert np.isfinite(rgb).all(), "render produced non-finite pixels"
    assert rgb.max() > 0, "render is empty (no gaussian projected)"
    print("render shape %s | rgb range [%.3f, %.3f] | nonzero px %.1f%%"
          % (render.shape, float(rgb.min()), float(rgb.max()), 100.0 * (rgb.sum(-1) > 0).mean()))
    print("SPLAT-SMOKE-OK")
    return 0


# ---- full pipeline (stubbed LOUDLY until authored against the live gsplat API in-container) ----------
def init_from_cloud(*a, **k):
    raise NotImplementedError("P6G.4 next pass: seed gaussians from the TSDF cloud (means=points, "
                              "colors=point colors, scales=kNN dist, opacity~0.1) -- NO COLMAP.")


def train(*a, **k):
    raise NotImplementedError("P6G.4 next pass: photometric(L1+SSIM) + depth-L1 loss along the odom "
                              "trajectory; joint pose refinement; sharpness pre-filter; per-image "
                              "appearance embeddings; dilated person masks (reuse geom masks).")


def eval_heldout(*a, **k):
    raise NotImplementedError("P6G.4 next pass: PSNR/SSIM on HELD-OUT CONTIGUOUS segments (temporal "
                              "neighbors leak -- never interleaved), seed + image digest recorded.")


def export_ply(*a, **k):
    raise NotImplementedError("P6G.4 next pass: write .ply/.splat in the MAP frame + a web-viewer "
                              "preview (co-registered with the P6G.2 mesh at the AprilTag anchors).")


def main(argv):
    ap = argparse.ArgumentParser(prog="splat", description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="gsplat + CUDA rasterization smoke")
    ap.add_argument("--run", help="run_id (recon bundle) -- full pipeline, not yet implemented")
    a = ap.parse_args(argv)
    if a.selftest:
        return _smoke()
    print("splat.py v0: only --selftest (the gsplat/CUDA smoke) is wired; the full pipeline is stubbed "
          "(P6G.4 next pass). See the module docstring.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
