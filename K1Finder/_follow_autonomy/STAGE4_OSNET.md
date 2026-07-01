# Stage 4 (deep) — OSNet ReID embedding, TRT FP16

The optional learned-embedding appearance backend. Drops into the same `feat_fn`/`sim_fn` swap point as `global`/`striped`, behind `--appearance osnet`. **Default-off; falls back to the histogram if anything is missing — nothing here changes behavior unless you pass `--appearance osnet --reid-engine …`.**

## Why / what it buys
A 30×32 colour histogram (or even the striped descriptor) cannot tell apart two people in similar/identical clothing. A person-ReID embedding (OSNet) is trained to be identity-discriminative, so it is what actually makes **failure mode A (look-alikes)** robust and **armed markerless re-acquire (E, `--arm-reacquire`)** trustworthy in crowds. Cost: a deep model + a TRT engine on the robot.

## How it runs (the engineering choice)
Served through **onnxruntime's TensorRT execution provider at FP16** (`trt_fp16_enable`), not hand-rolled pycuda/TRT. Rationale:
- **Reuses the confirmed dep** — onnxruntime is already used for YOLO; pycuda/`tensorrt` Python are *not* confirmed in the BoosterRos2 env.
- It **is** TRT FP16 — the TRT-EP compiles the ONNX to a TensorRT FP16 engine under the hood.
- **Builds + caches the engine on the device** automatically (`trt_engine_cache_enable`, cache dir = the model's folder) — honours the "build on the Orin, engines don't port" rule without a manual `trtexec` step.
- **Graceful provider fallback**: TensorRT-EP → CUDA-EP → CPU-EP. If even that fails (or the model is missing), `ReidEngine.ok=False` and the follower falls back to the colour histogram.

## Get an OSNet ONNX onto the robot
Any person-ReID ONNX with an `(N,3,256,128)` input works. Easiest sources:

**Option A — BoxMOT / torchreid prebuilt** (recommended): grab `osnet_x0_25_msmt17.onnx` (≈ a few MB) from the `boxmot` / `deep-person-reid` model zoo and copy it to the robot, e.g. `/opt/booster/reid/osnet_x0_25_msmt17.onnx`.

**Option B — export it yourself** (on a dev box with torch + torchreid):
```python
import torch, torchreid
m = torchreid.models.build_model('osnet_x0_25', num_classes=1000, pretrained=True)
m.eval()
# return the embedding, not the classifier logits:
torchreid.utils.load_pretrained_weights(m, 'osnet_x0_25_msmt17.pth')  # if you have weights
dummy = torch.randn(1, 3, 256, 128)
torch.onnx.export(m, dummy, 'osnet_x0_25.onnx', input_names=['x'], output_names=['feat'],
                  dynamic_axes={'x': {0: 'n'}, 'feat': {0: 'n'}}, opset_version=12)
```
Make sure the exported graph outputs the **feature vector** (e.g. 512-d), not class logits. (Per `policy-engineer`: verify the ONNX numerically against PyTorch on a couple of crops before trusting it.)

## Run it
```
# preview (no motion) — first run is SLOW while the TRT engine builds + caches:
python3 follow_person_k1.py --preview --stream --track --gallery-size 8 \
    --appearance osnet --reid-engine /opt/booster/reid/osnet_x0_25_msmt17.onnx
```
- `--reid-input 256x128` (default; match the model), `--reid-fp16/--no-reid-fp16` (default on).
- First run: the `__init__` warm-up triggers the TRT engine build — **expect a one-time delay of up to minutes** (it blocks startup, *not* the control loop; subsequent runs load the cached engine fast). The log line `REID-ENGINE ok … providers=…` confirms which EP is active.
- If you see `REID-ENGINE load FAILED … -> falls back to histogram`, fix the path / confirm onnxruntime has the TensorRT EP (`python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"`).

## Tune & gate (same discipline as striped)
- OSNet uses **cosine** similarity — **re-tune** `--anchor-floor` / `--bank-floor` / `--reloc-floor` / `--hiconf` from the `GALLERY-*` / `RELOC-*` audit logs (embedding cosines distribute differently from histogram correlation; same-person cosines are typically high, look-alike cosines clearly lower — better separation).
- **This is the backend that makes `--arm-reacquire` defensible.** Still: prove re-acquire precision on the replay eval (A/E clip families) before arming, and arm only after on-robot validation.

## Known limitation / next optimization (not a bug)
The embedding is currently computed **per candidate per frame** (no batching, no every-N caching). For the small person counts in a follow scenario this should hold 10 Hz alongside YOLO, but **measure end-to-end on the actual Orin** (the `SLOW-LOOP` guard will surface an overrun in `--preview`). If it can't hold the rate, the next step is: batch all candidate crops into one `session.run`, and/or cache per-`track_id` embeddings refreshed every N frames (`--reid-every-n`). Deferred until measurement says it's needed.
