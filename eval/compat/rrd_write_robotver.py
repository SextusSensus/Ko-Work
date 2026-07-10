"""rrd_write_robotver.py -- write a .rrd with the ROBOT's exact RerunSink (robot/k1_rerun.py).

Run this with a rerun-sdk **0.23.1** interpreter (the robot's pinned version; needs numpy<2), then read
the output with rrd_read_wsver.py under the workstation's rerun (>=0.33). Together they VERIFY the
robot-write / workstation-read cross-version path (docs/RERUN_COMPAT.md). Also confirms the P6.1
pinhole() call is accepted by the 0.23 Pinhole API.

  # in a rerun==0.23.1 env (numpy<2):
  python eval/compat/rrd_write_robotver.py [out.rrd]
"""
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "robot"))   # robot/k1_rerun.py
import rerun as rr
from k1_rerun import RerunSink

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("TEMP", "."), "robot_ver.rrd")
print("WRITE rerun", rr.__version__, "numpy", np.__version__, "->", out)
if os.path.exists(out):
    os.remove(out)
sink = RerunSink(enabled=True, app_id="k1_follow", mode="save", path=out,
                 image_every_n=1, min_safe=0.6, standoff=1.2, max_follow=3.0)
assert sink.ok, "RerunSink failed to open (see RERUN-FAULT above)"
sink.refs_once()
sink.pinhole("/camera/rgb", 640, 480, 500.0, 500.0, 320.0, 240.0)          # P6.1 addition
sink.write_intrinsics({"model": "pinhole_from_hfov", "approximate": True, "width": 640, "height": 480,
                       "fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0})
faults_after_pinhole = sink._faults
for i in range(10):
    sink.frame(i, 1000.0 + i * 0.1)
    sink.scalar("/follow/range", 1.4 + 0.01 * i)
    sink.scalar("/follow/bearing", 2.0)
    sink.scalar("/cmd/vx", 0.1)
    sink.scalar("/cmd/vyaw", -0.05)
    sink.scalar("/reid/sim", 0.95)
    sink.scalar("/track/conf", 0.9)
    sink.state("/fsm/state", "TRACK")
    bgr = (np.random.RandomState(i).rand(48, 64, 3) * 255).astype(np.uint8)
    sink.image("/camera/rgb", bgr, seq=i)
    sink.depth("/camera/depth", np.ones((48, 64), np.float32) * 1.5, seq=i)
sink.close()
try:
    rr.rerun_shutdown()
except Exception:
    pass
print("PINHOLE-FAULTS:", faults_after_pinhole, "(0 == the 0.23 Pinhole API accepted the P6.1 call)")
print("TOTAL-FAULTS:", sink._faults)
assert sink._faults == 0, "the robot sink faulted under this rerun version -- investigate before trusting"
print("WROTE", out, os.path.getsize(out), "bytes -- now read it with rrd_read_wsver.py under rerun>=0.33")
