"""rrd_read_wsver.py -- read a robot-version .rrd with the WORKSTATION ingest path (rrd_to_lerobot).

Run with the workstation's rerun (>=0.33) on the .rrd produced by rrd_write_robotver.py under rerun
0.23.1. Verifies the robot-write / workstation-read cross-version path loads the CORE primitives the
pipeline uses -- scalars + RGB + depth -- and assembles a valid episode (docs/RERUN_COMPAT.md).

  python eval/compat/rrd_read_wsver.py [in.rrd]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))                  # eval/rrd_to_lerobot.py
import rerun as rr
from rrd_to_lerobot import read_rrd, assemble_episode

inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("TEMP", "."), "robot_ver.rrd")
print("READ rerun", rr.__version__, "<-", inp, os.path.getsize(inp), "bytes")
try:
    scalars, images, depth = read_rrd(inp)
except Exception as e:  # noqa: BLE001
    print("READ-FAILED:", type(e).__name__, e)
    raise SystemExit(1)
print("scalar entities:", sorted(scalars.keys()))
print("rgb frames:", len(images), "| depth frames:", len(depth))
frames, state, action, rgb = assemble_episode(scalars, images)
rgb_ok = sum(1 for r in rgb if r is not None)
print("episode: T=%d state=%s rgb_non_none=%d" % (len(frames), tuple(state.shape), rgb_ok))
ok = ("/follow/range" in scalars and "/fsm/state_id" in scalars and len(images) > 0
      and len(depth) > 0 and len(frames) > 0 and rgb_ok > 0)
print("RRD-CROSSVER-OK" if ok else "RRD-CROSSVER-FAIL (a core primitive did not load)")
raise SystemExit(0 if ok else 1)
