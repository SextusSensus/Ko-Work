#!/usr/bin/env python3
"""Pre-build the OSNet TRT FP16 engines through the NODE'S OWN ReidEngine, so the first field
follow never stalls the control loop on an on-the-fly TensorRT build, then sanity-check the
embeddings through the real embed path (letterbox + ImageNet norm + L2)."""
import os, sys, time
sys.path.insert(0, "/home/booster")
import numpy as np
import cv2
import identity

MODEL = "/home/booster/reid/osnet_x0_25_msmt17.onnx"
CACHE = "/home/booster/reid"

t0 = time.time()
eng = identity.ReidEngine(MODEL, input_hw=(256, 128), fp16=True, cache_dir=CACHE,
                          letterbox=True, batch=True)
print("[stage] ok=%s dyn_batch=%s providers=%s cpu_ep_degraded=%s  build+warm=%.1fs"
      % (eng.ok, eng._dyn_batch, ",".join(eng.providers_active), eng.cpu_ep_degraded,
         time.time() - t0))
if not eng.ok:
    raise SystemExit("[stage] FAIL: ReidEngine not ok")
if not eng._dyn_batch:
    print("[stage] WARN: batch axis not dynamic -> only batch=1 engine warmed")

rng = np.random.default_rng(0)
frame = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
cv2.rectangle(frame, (250, 120), (330, 400), (40, 60, 200), -1)    # torso
cv2.rectangle(frame, (265, 130), (315, 200), (200, 190, 170), -1)  # head/shoulders
box = (245, 110, 335, 410)

a = eng.embed(frame, box)
b = eng.embed(frame, box)
print("[stage] same-crop cos=%.6f            (expect 1.000000)" % float(np.dot(a, b)))
b2 = eng.embed(frame, (250, 115, 340, 415))
print("[stage] shifted-crop cos=%.4f          (expect high)" % float(np.dot(a, b2)))
frame2 = frame.copy()
cv2.rectangle(frame2, (250, 120), (330, 400), (200, 220, 60), -1)  # different clothing colour
c = eng.embed(frame2, box)
print("[stage] recoloured-crop cos=%.4f       (expect clearly lower than same-crop)" % float(np.dot(a, c)))

boxes = [box, (100, 100, 180, 380), (400, 150, 470, 420), (50, 50, 120, 300), (500, 200, 560, 430)]
t1 = time.time()
embs = eng.embed_batch(frame, boxes)
print("[stage] embed_batch n=5 -> %d embeddings in %.1fms (chunked %s -- all pre-warmed shapes)"
      % (sum(1 for e in embs if e is not None), (time.time() - t1) * 1000.0,
         identity.ReidEngine._chunk_sizes(5, (1, 2, 4))))

ts = []
for _ in range(25):
    t2 = time.time()
    eng.embed_batch(frame, boxes[:2])
    ts.append((time.time() - t2) * 1000.0)
print("[stage] steady-state embed_batch(2): p50=%.1fms p90=%.1fms p99=%.1fms"
      % (float(np.percentile(ts, 50)), float(np.percentile(ts, 90)), float(np.percentile(ts, 99))))

print("[stage] /home/booster/reid contents:")
tot = 0
for f in sorted(os.listdir(CACHE)):
    n = os.path.getsize(os.path.join(CACHE, f)); tot += n
    print("   %-72s %d" % (f, n))
print("[stage] total %d bytes" % tot)
print("[stage] DONE")
