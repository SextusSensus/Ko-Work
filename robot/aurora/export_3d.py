#!/usr/bin/env python3
"""Export the Aurora's 3D SLAM map as a point cloud (.ply) + the keyframe camera path.
Run on the LAPTOP with the Aurora connected (the SDK link works there).

  python export_3d.py [device_ip] [out.ply]

get_map_data() returns {'map_points': [ {x,y,z,...}, ... ], 'keyframes': [...], ...} -- the sparse
SLAM landmarks (real 3D structure, not a dense surface). Points white, keyframe positions red.
"""
import sys, time
import numpy as np
import slamtec_aurora_sdk as sdk

ip  = sys.argv[1] if len(sys.argv) > 1 else "192.168.11.1"
out = sys.argv[2] if len(sys.argv) > 2 else "map_3d.ply"
s = sdk.AuroraSDK()
try:
    s.connect(connection_string=ip)
except Exception as e:
    print("CONNECT-FAIL:", type(e).__name__, e); sys.exit(1)
print("connected:", s.is_connected())
try: s.enable_map_data_syncing(True)
except Exception: pass
time.sleep(2.0)

def xyz(dpt):
    """Pull (x,y,z) from a map_point/keyframe dict, however it names them."""
    if not isinstance(dpt, dict): return None
    for kx, ky, kz in (("x","y","z"), ("px","py","pz")):
        if kx in dpt: return (float(dpt[kx]), float(dpt[ky]), float(dpt[kz]))
    for kp in ("position","world_position","translation","pose"):
        v = dpt.get(kp)
        if isinstance(v, (list, tuple)) and len(v) >= 3: return (float(v[0]), float(v[1]), float(v[2]))
        if isinstance(v, dict) and "x" in v: return (float(v["x"]), float(v["y"]), float(v["z"]))
    return None

try:
    d = s.get_map_data()
    mp = [p for p in (xyz(x) for x in d.get("map_points", [])) if p]
    kf = [p for p in (xyz(x) for x in d.get("keyframes", [])) if p]
    if not mp:
        sample = d.get("map_points", [None])[0]
        print("NO-POINTS: map_point keys were", list(sample.keys()) if isinstance(sample, dict) else sample)
        s.disconnect(); sys.exit(1)
    pts = np.array(mp, dtype=np.float64)
    kfp = np.array(kf, dtype=np.float64) if kf else np.empty((0, 3))
    with open(out, "w") as f:
        f.write("ply\nformat ascii 1.0\nelement vertex %d\n" % (len(pts) + len(kfp)))
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
        for p in pts:  f.write("%.4f %.4f %.4f 220 220 220\n" % (p[0], p[1], p[2]))   # landmarks
        for p in kfp:  f.write("%.4f %.4f %.4f 230 60 40\n"   % (p[0], p[1], p[2]))   # camera path
    ext = pts.max(0) - pts.min(0)
    print("EXPORT-OK %d map points + %d keyframes -> %s" % (len(pts), len(kfp), out))
    print("extent (m): x=%.1f y=%.1f z=%.1f" % (ext[0], ext[1], ext[2]))
except Exception as e:
    print("EXPORT-FAIL:", type(e).__name__, e)
finally:
    s.disconnect()
