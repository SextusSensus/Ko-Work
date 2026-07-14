#!/usr/bin/env python3
"""calibration.py -- load a P8.1 calibrated intrinsic for the recorder (docs/FRAMES.md 2.2).

Pure stdlib (NO ros, NO cv2) so it imports + self-tests OFF the robot. eval/calibrate_camera.py PRODUCES
`models/calibration.json`; this LOADS it at capture time so the recorded run's intrinsics.json is the
REAL calibrated intrinsic (approximate:false) instead of the --hfov-deg seed -- which is what stops the
recon mesh from warping. Recording-only: this never touches the control law (same gate as the seed).

  python robot/calibration.py selftest   ->  CALIB-LOAD-SELFTEST-OK
"""
import json
import os
import sys

_REQUIRED = ("fx", "fy", "cx", "cy", "width", "height")


def load_calibration(path, w_img, h_img):
    """Return the calibrated intrinsics dict if `path` is a valid calibration whose resolution matches
    the LIVE frame (w_img, h_img); else None so the caller falls back to the hfov seed. Fail-closed:
    a missing field or a resolution mismatch returns None -- a wrong-resolution calibration silently
    warps geometry worse than the honest seed, so we refuse it rather than trust it."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            c = json.load(f)
    except (ValueError, OSError):
        return None
    for k in _REQUIRED:
        if k not in c:
            return None
    if int(c["width"]) != int(w_img) or int(c["height"]) != int(h_img):
        return None
    c["approximate"] = bool(c.get("approximate", False))
    return c


def _selftest():
    import tempfile
    d = tempfile.mkdtemp(prefix="calibload-")
    good = {"width": 544, "height": 448, "fx": 420.0, "fy": 415.0, "cx": 270.0, "cy": 224.0,
            "model": "opencv_pinhole", "approximate": False, "distortion": [0.01, -0.02, 0, 0, 0]}
    p = os.path.join(d, "calibration.json")
    with open(p, "w") as f:
        json.dump(good, f)
    assert load_calibration(p, 544, 448)["fx"] == 420.0, "a valid, resolution-matched calib must load"
    assert load_calibration(p, 544, 448)["approximate"] is False, "calibrated -> approximate False"
    assert load_calibration(p, 640, 480) is None, "resolution mismatch MUST reject"
    assert load_calibration(None, 544, 448) is None, "no path -> None (fall back to seed)"
    assert load_calibration(os.path.join(d, "nope.json"), 544, 448) is None, "absent file -> None"
    bad = dict(good); del bad["fx"]
    pb = os.path.join(d, "bad.json")
    with open(pb, "w") as f:
        json.dump(bad, f)
    assert load_calibration(pb, 544, 448) is None, "missing fx MUST reject"
    print("CALIB-LOAD-SELFTEST-OK")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest() if (len(sys.argv) > 1 and sys.argv[1] == "selftest") else 0)
