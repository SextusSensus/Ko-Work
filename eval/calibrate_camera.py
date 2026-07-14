#!/usr/bin/env python3
"""calibrate_camera.py -- P8.1 head-camera intrinsic calibration (OpenCV chessboard).

Turns ~15-25 views of a checkerboard into the REAL intrinsic the recon trusts, written in the exact
`models/calibration.json` format (docs/FRAMES.md 2.2): approximate:false + plumb_bob distortion. Once
this exists, the recon metric gate accepts odom/tsdf WITHOUT --allow-approximate-intrinsics, and the
mesh stops warping from the hfov guess.

Board convention (OpenCV): --cols/--rows are INNER corners, NOT squares. A board with 10x7 squares has
9x6 inner corners -> --cols 9 --rows 6. Square size does NOT affect the intrinsic matrix (only board
pose), so --square-mm is optional; measure it if you also want the extrinsics sane.

Capture tips: a dimmed TABLET showing the pattern is a great flat target; ~20 views spanning near/far,
tilted, and every corner of the frame (not all centered); avoid glare + getting close enough to moire.

  # from a folder of frames (JP/PNG grabbed off the Live View, or extracted):
  python eval/calibrate_camera.py --images ./calib_frames --cols 9 --rows 6 --out models/calibration.json
  # or straight from a short capture .rrd of you waving the board:
  python eval/calibrate_camera.py --rrd run/k1_follow_*.rrd --cols 9 --rows 6 --out models/calibration.json
  python eval/calibrate_camera.py --selftest        # -> CALIB-SELFTEST-OK (no camera, no images)

Runs in the recon/train image (opencv-contrib-python-headless). GATE: RMS reprojection error < ~0.6 px
and >= 10 accepted views, else it warns loudly -- a bad calibration is worse than the honest hfov seed.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

RMS_WARN_PX = 0.6
MIN_VIEWS = 10


def _object_points(cols, rows, square_m):
    """Inner-corner grid in the board frame (Z=0). square_m only scales board pose, not the intrinsic."""
    objp = np.zeros((rows * cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    return objp * float(square_m)


def _detect(images, cols, rows):
    """Find + subpixel-refine chessboard inner corners in each image. Returns (objpts, imgpts, size, used)."""
    import cv2
    objp = _object_points(cols, rows, 1.0)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK
    objpts, imgpts, used, size = [], [], [], None
    for name, img in images:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        if size is None:
            size = (gray.shape[1], gray.shape[0])
        elif (gray.shape[1], gray.shape[0]) != size:
            print("  skip %s: resolution %dx%d != %dx%d" % (name, gray.shape[1], gray.shape[0], *size))
            continue
        ok, corners = cv2.findChessboardCorners(gray, (cols, rows), flags)
        if not ok:
            print("  no board: %s" % name)
            continue
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), crit)
        objpts.append(objp.copy()); imgpts.append(corners); used.append(name)
    return objpts, imgpts, size, used


def calibrate(images, cols, rows, square_m):
    """Full calibration -> (calib_dict, rms, per_view_err). Raises SystemExit on too-few detections."""
    import cv2
    objpts, imgpts, size, used = _detect(images, cols, rows)
    if len(objpts) < MIN_VIEWS:
        raise SystemExit("REFUSE: only %d boards detected (need >= %d). Capture more varied views "
                         "(near/far, tilted, board in the frame CORNERS)." % (len(objpts), MIN_VIEWS))
    objpts = [o * float(square_m) for o in objpts]     # scale now (pose only; K unaffected)
    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(objpts, imgpts, size, None, None)
    per_view = []
    for i in range(len(objpts)):
        proj, _ = cv2.projectPoints(objpts[i], rvecs[i], tvecs[i], K, dist)
        e = float(np.sqrt(np.mean(np.sum((imgpts[i] - proj) ** 2, axis=2))))
        per_view.append((used[i], round(e, 3)))
    calib = {
        "model": "opencv_pinhole", "approximate": False,
        "width": int(size[0]), "height": int(size[1]),
        "fx": float(K[0, 0]), "fy": float(K[1, 1]), "cx": float(K[0, 2]), "cy": float(K[1, 2]),
        "distortion_model": "plumb_bob",
        "distortion": [float(x) for x in dist.ravel()[:5]],
        "rms_reproj_px": float(rms), "n_views": len(objpts),
        "note": "P8.1 OpenCV chessboard calibration; replaces the hfov seed (docs/FRAMES.md 2.2)",
    }
    return calib, float(rms), per_view


def _report(calib, rms, per_view):
    print("\nfx=%.2f fy=%.2f  cx=%.2f cy=%.2f  (%dx%d)" %
          (calib["fx"], calib["fy"], calib["cx"], calib["cy"], calib["width"], calib["height"]))
    print("distortion (k1,k2,p1,p2,k3):", ["%.4f" % d for d in calib["distortion"]])
    print("RMS reprojection: %.3f px over %d views" % (rms, calib["n_views"]))
    worst = sorted(per_view, key=lambda x: -x[1])[:3]
    print("worst views:", worst)
    if rms > RMS_WARN_PX:
        print("\nWARN: RMS %.3f px > %.2f -- calibration is loose. Common causes: a warped/non-flat "
              "board, screen glare/moire, too few tilt angles, or wrong --cols/--rows. A loose "
              "calibration can be WORSE than the hfov seed -- recapture before trusting it." % (rms, RMS_WARN_PX))
    else:
        print("CALIB-OK (RMS within %.2f px)" % RMS_WARN_PX)


def _load_images(images_dir, rrd):
    import cv2
    out = []
    if images_dir:
        for p in sorted(glob.glob(os.path.join(images_dir, "*"))):
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                im = cv2.imread(p)
                if im is not None:
                    out.append((os.path.basename(p), im))
    if rrd:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from rrd_to_lerobot import read_rrd
        _s, images, _d = read_rrd(rrd)             # RGB frames from the capture
        for fi, img in sorted(images.items()):
            out.append(("rrd#%d" % fi, cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)))
    return out


def _selftest():
    """No camera/images: project a known board under known intrinsics from several poses, feed the
    2D points to calibrateCamera, and assert the recovered intrinsic matches. Validates the math +
    the output format end to end."""
    import cv2
    cols, rows = 9, 6
    W, H = 544, 448
    K_true = np.array([[420.0, 0, 270.0], [0, 415.0, 224.0], [0, 0, 1]], np.float64)
    dist_true = np.zeros(5)
    objp = _object_points(cols, rows, 0.024)
    rng = np.random.default_rng(0)
    objpts, imgpts = [], []
    for _ in range(18):
        rvec = rng.uniform(-0.5, 0.5, 3)
        tvec = np.array([rng.uniform(-0.1, 0.1), rng.uniform(-0.1, 0.1), rng.uniform(0.5, 1.2)])
        proj, _ = cv2.projectPoints(objp, rvec, tvec, K_true, dist_true)
        # keep only poses whose corners land inside the image (a real detection would)
        if proj[:, 0, 0].min() < 5 or proj[:, 0, 0].max() > W - 5 or proj[:, 0, 1].min() < 5 or proj[:, 0, 1].max() > H - 5:
            continue
        objpts.append(objp.copy()); imgpts.append(proj.astype(np.float32))
    assert len(objpts) >= MIN_VIEWS, "selftest produced too few in-frame poses (%d)" % len(objpts)
    rms, K, dist, _r, _t = cv2.calibrateCamera(objpts, imgpts, (W, H), None, None)
    assert abs(K[0, 0] - 420.0) < 2.0 and abs(K[1, 1] - 415.0) < 2.0, "fx/fy off: %s" % K.diagonal()
    assert abs(K[0, 2] - 270.0) < 2.0 and abs(K[1, 2] - 224.0) < 2.0, "cx/cy off: %s" % (K[0, 2], K[1, 2])
    assert rms < 0.1, "clean synthetic RMS too high: %.4f" % rms
    print("CALIB-SELFTEST-OK (recovered fx=%.1f fy=%.1f cx=%.1f cy=%.1f from %d synthetic views, rms=%.4f)"
          % (K[0, 0], K[1, 1], K[0, 2], K[1, 2], len(objpts), rms))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--images", help="folder of checkerboard frames (jpg/png)")
    ap.add_argument("--rrd", help="a capture .rrd of the board (uses its RGB frames)")
    ap.add_argument("--cols", type=int, default=9, help="INNER corners across (squares-1)")
    ap.add_argument("--rows", type=int, default=6, help="INNER corners down (squares-1)")
    ap.add_argument("--square-mm", type=float, default=25.0, help="square size (pose only; K unaffected)")
    ap.add_argument("--out", default="models/calibration.json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if not (a.images or a.rrd):
        ap.error("need --images <dir> or --rrd <file> (or --selftest)")
    imgs = _load_images(a.images, a.rrd)
    print("loaded %d frames; detecting %dx%d inner corners ..." % (len(imgs), a.cols, a.rows))
    calib, rms, per_view = calibrate(imgs, a.cols, a.rows, a.square_mm / 1000.0)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(calib, f, indent=2, sort_keys=True)
    _report(calib, rms, per_view)
    print("\nwrote %s -- drop it in as the run's intrinsics.json (approximate:false) and the recon "
          "metric gate accepts it without --allow-approximate-intrinsics." % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
