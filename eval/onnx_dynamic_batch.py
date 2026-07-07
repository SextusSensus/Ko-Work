#!/usr/bin/env python3
"""P2 #13: make a DYNAMIC-batch copy of the OSNet ReID ONNX (graph surgery, no re-export),
then pre-warm the ORT TensorRT engine cache for batch sizes 1/2/4 so the first crowd frame
never stalls on an on-the-fly engine build. Writes results to stdout (caller redirects).
Leaves the original model and the app default UNTOUCHED -- the dynamic model is opt-in via
--reid-engine /home/booster/reid/osnet_x0_25_msmt17_dynN.onnx.
"""
import sys, time

SRC = "/home/booster/reid/osnet_x0_25_msmt17.onnx"
DST = "/home/booster/reid/osnet_x0_25_msmt17_dynN.onnx"

try:
    import onnx
    print("onnx", onnx.__version__)
except Exception as e:
    print("NO_ONNX_PKG:", e)
    sys.exit(2)
import onnxruntime as ort
print("ort", ort.__version__)

m = onnx.load(SRC)
g = m.graph
inp = g.input[0]
dims = inp.type.tensor_type.shape.dim
print("input:", inp.name, [d.dim_value if d.dim_value else d.dim_param for d in dims])
# batch axis -> symbolic 'N' (dynamic). Outputs too, so shape inference stays consistent.
dims[0].ClearField("dim_value"); dims[0].dim_param = "N"
for out in g.output:
    od = out.type.tensor_type.shape.dim
    if len(od) and od[0].dim_value:
        od[0].ClearField("dim_value"); od[0].dim_param = "N"
onnx.checker.check_model(m)
onnx.save(m, DST)
print("saved:", DST)

# Pre-warm: build + cache TRT engines for the batch shapes the follow will actually see.
# (ORT TRT-EP caches per-shape engines in the model dir; without this, the FIRST 2- or
# 4-person frame in the field would stall the loop for the engine build.)
import numpy as np
so = ort.SessionOptions()
providers = [("TensorrtExecutionProvider", {
    "trt_fp16_enable": True,
    "trt_engine_cache_enable": True,
    "trt_engine_cache_path": "/home/booster/reid",
}), "CUDAExecutionProvider", "CPUExecutionProvider"]
sess = ort.InferenceSession(DST, so, providers=providers)
name = sess.get_inputs()[0].name
print("active providers:", sess.get_providers())
for b in (1, 2, 4):
    x = np.random.rand(b, 3, 256, 128).astype(np.float32)
    t0 = time.monotonic()
    y = sess.run(None, {name: x})
    t1 = time.monotonic()
    # second run = steady-state latency for this shape
    t2 = time.monotonic(); y = sess.run(None, {name: x}); t3 = time.monotonic()
    print("batch=%d out=%s build+run1=%.0fms steady=%.1fms" % (
        b, tuple(y[0].shape), (t1 - t0) * 1000.0, (t3 - t2) * 1000.0))
print("OK_DYNBATCH")
