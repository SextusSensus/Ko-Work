#!/usr/bin/env python3
#
# PROVENANCE (verified on the robot 2026-09-02, aarch64 / torch 2.7.0 / onnx 1.18.0):
#   osnet.py   raw.githubusercontent.com/KaiyangZhou/deep-person-reid/master/torchreid/models/osnet.py
#              17037 bytes  sha256 c7c1c29187d6330f859c91da229271531920464c7011aec13842a086b2263cae
#   weights    huggingface.co/kaiyangzhou/osnet (the OSNet author's own repo), file
#              osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth
#              9336983 bytes  sha256 cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18
#   ->         osnet_x0_25_msmt17.onnx
#              891526 bytes  sha256 3f18b69c57773eb0c26f271128cdbf1bb80b8e427a24f63c39a54646f4af88ed
"""Export osnet_x0_25 (MSMT17) to a dynamic-batch ONNX for the K1 follow's ReidEngine.

Inputs (downloaded alongside this script, see fetch step):
  osnet.py  -- the OFFICIAL torchreid architecture (KaiyangZhou/deep-person-reid, self-contained)
  <ckpt>    -- the OFFICIAL osnet_x0_25 MSMT17 checkpoint (huggingface.co/kaiyangzhou/osnet)

Output: osnet_x0_25_msmt17.onnx with input x:(N,3,256,128) float32 (ImageNet-normalized RGB) and
output feat:(N,512), batch axis DYNAMIC so identity.ReidEngine pre-builds its (1,2,4) TRT engines.
Verifies the ONNX numerically against PyTorch before writing the final file.
"""
import sys, os, hashlib
import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CKPT = sys.argv[1]
OUT  = sys.argv[2]

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

print("[export] osnet.py   sha256 %s" % sha(os.path.join(HERE, "osnet.py")))
print("[export] checkpoint sha256 %s (%d bytes)" % (sha(CKPT), os.path.getsize(CKPT)))

import osnet as osnet_mod
model = osnet_mod.osnet_x0_25(num_classes=1000, pretrained=False, loss="softmax")

# torchreid checkpoints are {'state_dict':..., 'epoch':...} with optional 'module.' prefixes.
try:
    ck = torch.load(CKPT, map_location="cpu", weights_only=True)
    print("[export] torch.load weights_only=True OK")
except Exception as e:
    print("[export] weights_only=True failed (%s) -> retrying weights_only=False" % e)
    ck = torch.load(CKPT, map_location="cpu", weights_only=False)
sd = ck.get("state_dict", ck) if isinstance(ck, dict) else ck
sd = {(k[7:] if k.startswith("module.") else k): v for k, v in sd.items()}

# The MSMT17 head is dataset-specific (4101 ids) and unused -- the follow reads the 512-d
# feature, never the logits. Drop any shape-mismatched key (classifier only) before loading.
msd = model.state_dict()
dropped = [k for k, v in sd.items() if k in msd and tuple(msd[k].shape) != tuple(v.shape)]
if dropped:
    print("[export] dropping shape-mismatched keys: %s" % dropped)
sd = {k: v for k, v in sd.items() if k not in dropped}
missing, unexpected = model.load_state_dict(sd, strict=False)
missing = [k for k in missing]; unexpected = [k for k in unexpected]
print("[export] load_state_dict missing=%d unexpected=%d" % (len(missing), len(unexpected)))
if missing:
    print("[export]   missing:   %s" % ", ".join(missing[:8]))
if unexpected:
    print("[export]   unexpected:%s" % ", ".join(unexpected[:8]))
# Only the classifier may differ (the follow uses the 512-d feature, never the logits).
bad = [k for k in missing if not k.startswith("classifier.")]
if bad:
    raise SystemExit("[export] FAIL: backbone weights did not load: %s" % bad[:8])

model.eval()
dummy = torch.randn(2, 3, 256, 128)
with torch.no_grad():
    ref = model(dummy)
if isinstance(ref, (tuple, list)):
    raise SystemExit("[export] FAIL: model returned a tuple (training mode?) -- need the feature tensor")
print("[export] torch output shape %s (expect (2,512) features, NOT (2,1000) logits)" % (tuple(ref.shape),))
if ref.shape[1] == 1000:
    raise SystemExit("[export] FAIL: got class logits, not the embedding")

tmp_out = OUT + ".tmp"
kw = dict(input_names=["x"], output_names=["feat"],
          dynamic_axes={"x": {0: "batch"}, "feat": {0: "batch"}},
          opset_version=17, do_constant_folding=True)
try:
    torch.onnx.export(model, dummy, tmp_out, dynamo=False, **kw)
except TypeError:
    torch.onnx.export(model, dummy, tmp_out, **kw)
print("[export] wrote %s (%d bytes)" % (tmp_out, os.path.getsize(tmp_out)))

import onnx
m = onnx.load(tmp_out)
onnx.checker.check_model(m)
i0 = m.graph.input[0]
dims = [(d.dim_param or d.dim_value) for d in i0.type.tensor_type.shape.dim]
print("[export] onnx input '%s' dims=%s" % (i0.name, dims))
if not isinstance(dims[0], str):
    raise SystemExit("[export] FAIL: batch axis is not dynamic (%r) -- the (1,2,4) TRT warm set needs it" % dims[0])

# Numeric parity: PyTorch vs onnxruntime (CPU EP) on the same input, at batch 1 and 4.
import onnxruntime as ort
sess = ort.InferenceSession(tmp_out, providers=["CPUExecutionProvider"])
name = sess.get_inputs()[0].name
for bs in (1, 2, 4):
    x = torch.randn(bs, 3, 256, 128)
    with torch.no_grad():
        a = model(x).numpy()
    b = sess.run(None, {name: x.numpy()})[0]
    d = float(np.max(np.abs(a - b)))
    cos = float(np.mean(np.sum(a * b, 1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))))
    print("[export] parity batch=%d  max|torch-onnx|=%.3e  cos=%.8f" % (bs, d, cos))
    if not (d < 1e-3 and cos > 0.9999):
        raise SystemExit("[export] FAIL: ONNX does not match PyTorch at batch=%d" % bs)

# Non-degenerate check: two different inputs must NOT produce the same embedding.
x1 = sess.run(None, {name: np.random.randn(1, 3, 256, 128).astype(np.float32)})[0][0]
x2 = sess.run(None, {name: np.random.randn(1, 3, 256, 128).astype(np.float32)})[0][0]
c = float(np.dot(x1, x2) / (np.linalg.norm(x1) * np.linalg.norm(x2)))
print("[export] distinct-input cosine=%.4f (must be < 0.99 -- a constant/dead graph would be ~1.0)" % c)
if c > 0.99:
    raise SystemExit("[export] FAIL: embedding looks degenerate")

os.replace(tmp_out, OUT)
print("[export] OK -> %s (%d bytes) sha256 %s" % (OUT, os.path.getsize(OUT), sha(OUT)))
