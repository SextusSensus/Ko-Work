#!/usr/bin/env python3
"""Export the Aurora's 3D SLAM map as a point cloud (.ply). Run on the LAPTOP with the Aurora
plugged in and powered -- the SDK connection is proven to work there (it is how map.vslam was
pulled). The robot-side link does not yet serve the SDK, so this is laptop-only for now.

  python export_3d.py [device_ip] [out.ply]

get_map_data() returns the sparse SLAM map (3D feature landmarks) -- the map's real 3D structure,
not a dense cloud (a dense cloud needs aurora_map_densifier.exe + an SfM recording). First run also
prints the returned structure so the extraction can be refined if the shape differs.
"""
import sys, time
import slamtec_aurora_sdk as sdk

ip  = sys.argv[1] if len(sys.argv) > 1 else None
out = sys.argv[2] if len(sys.argv) > 2 else "map_3d.ply"
s = sdk.AuroraSDK()

dev = None
try:
    found = s.discover_devices(timeout=5.0)
    print("discovered", len(found), "device(s)")
    if found: dev = found[0]
except Exception as e:
    print("discover error:", type(e).__name__, e)
try:
    if dev is not None: s.connect(device_info=dev)
    else: s.connect(connection_string=ip or "192.168.11.1")
except Exception as e:
    print("CONNECT-FAIL:", type(e).__name__, e); sys.exit(1)
print("connected:", s.is_connected())

# Ask the device to sync its map to the client, then pull it.
try:
    s.enable_map_data_syncing(True)
except Exception:
    pass
time.sleep(2.0)

def extract_xyz(obj):
    """Best-effort: pull a list of (x,y,z) from whatever get_map_data returns. Prints the shape on
    the first call so we can tighten this if needed."""
    import numpy as np
    # common shapes: numpy Nx3; object with .points/.map_points; list of point objects with x/y/z
    if isinstance(obj, np.ndarray):
        a = obj.reshape(-1, obj.shape[-1]); return a[:, :3]
    for attr in ("points", "map_points", "positions", "vertices"):
        v = getattr(obj, attr, None)
        if v is not None: return extract_xyz(v)
    if isinstance(obj, (list, tuple)) and obj:
        first = obj[0]
        if all(hasattr(first, c) for c in ("x", "y", "z")):
            return np.array([[p.x, p.y, p.z] for p in obj], dtype=np.float64)
        if isinstance(first, (list, tuple, np.ndarray)):
            return np.array([list(p)[:3] for p in obj], dtype=np.float64)
    return None

try:
    data = s.get_map_data()
    print("get_map_data ->", type(data).__name__, "attrs:", [a for a in dir(data) if not a.startswith('_')][:20])
    pts = extract_xyz(data)
    if pts is None or len(pts) == 0:
        print("NO-POINTS: could not extract xyz from the returned structure (see attrs above)")
    else:
        import numpy as np
        pts = np.asarray(pts, dtype=np.float64)
        with open(out, "w") as f:
            f.write("ply\nformat ascii 1.0\nelement vertex %d\n" % len(pts))
            f.write("property float x\nproperty float y\nproperty float z\nend_header\n")
            for p in pts:
                f.write("%.4f %.4f %.4f\n" % (p[0], p[1], p[2]))
        print("EXPORT-OK %d points -> %s" % (len(pts), out))
except Exception as e:
    print("EXPORT-FAIL:", type(e).__name__, e)
finally:
    s.disconnect()
