#!/usr/bin/env python3
"""infer.py -- the offline ONNX-inference utility for the XDNA2 NPU (docs/NPU_UTILIZATION.md).

Runs one of three project-useful networks over a run bundle's RGB frames and writes an artifact the
rest of the offline pipeline consumes:
  * detect  -> YOLO11n person boxes  -> masks/person_boxes.jsonl   (recon masks BYSTANDERS -- P8.2a)
  * depth   -> Depth-Anything mono   -> depth_mono/<frame>.npy      (validate/fill flaky robot depth)
  * segment -> SegFormer-B0 labels   -> semseg/<frame>.png          (scene semantics for the map)

Provider fallback (fast -> portable): `VitisAIExecutionProvider` (the XDNA2 NPU, via AMD Ryzen AI SW)
-> `DmlExecutionProvider` (DirectML: the 780M iGPU / 5060) -> `CPUExecutionProvider`. So the code runs
EVERYWHERE and is *accelerated* on the NPU once the Ryzen AI Software is installed (VitisAI EP + INT8
Quark-quantized models). The provider actually used is recorded in every result manifest.

RUNS NATIVELY on the laptop worker (a subprocess job), NOT in a container: the VitisAI EP needs the
host Ryzen AI runtime + NPU device access that Docker can't pass through on Windows/WSL. The DML/CPU
fallback COULD containerize, but the NPU acceleration is native. (The CUDA/CPU job types -- train,
recon, ingest -- DO run in Docker; see desktop/train + desktop/recon.)

Self-test (no NPU/models needed -- proves the runtime + provider selection + the pipeline):
  python npu/infer.py selftest        -> NPU-INFER-SELFTEST-OK
Real run (needs the ONNX model + VitisAI EP for NPU accel):
  python npu/infer.py detect --model yolo11n.onnx --bundle runs/<id> --out runs/<id>
"""
import argparse
import json
import os
import sys
import time

import numpy as np

# eval/rrd_to_lerobot.read_rrd decodes the bundle .rrd (imported, never re-typed).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _c in (os.path.join(_HERE, "..", "eval"), os.path.join(_HERE, "eval")):
    if os.path.isdir(_c):
        sys.path.insert(0, _c)
        break

# Provider preference: NPU first, then iGPU/dGPU, then CPU. Filtered to what's actually available.
PROVIDER_PREFERENCE = ("VitisAIExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider")
TASKS = ("detect", "depth", "segment")


def available_providers():
    import onnxruntime as ort
    return list(ort.get_available_providers())


def select_providers():
    """Ordered list of EPs to try, best-available-first. Always ends with CPU (always present)."""
    avail = set(available_providers())
    chosen = [p for p in PROVIDER_PREFERENCE if p in avail]
    if "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")       # guaranteed fallback
    return chosen


class OnnxRunner:
    """Thin ONNX Runtime session wrapper. Picks the best available provider (NPU->DML->CPU); exposes the
    provider actually used + a single-input run(). INT8/BF16 quantization is a MODEL property (done with
    AMD Quark at build time -- docs/NPU_UTILIZATION.md), not this runner's concern."""
    def __init__(self, model_path, providers=None):
        import onnxruntime as ort
        self.providers = providers or select_providers()
        self.sess = ort.InferenceSession(model_path, providers=self.providers)
        self.provider_used = self.sess.get_providers()[0]
        self.in_name = self.sess.get_inputs()[0].name
        self.in_shape = self.sess.get_inputs()[0].shape
        self.out_names = [o.name for o in self.sess.get_outputs()]

    def run(self, x):
        return self.sess.run(self.out_names, {self.in_name: x})


# ---- frame source -------------------------------------------------------------------------------
def bundle_frames(bundle_dir):
    """Yield (frame_idx, HxWx3 uint8 RGB) from a run bundle's .rrd (via read_rrd)."""
    from rrd_to_lerobot import read_rrd, RGB_ENTITY  # noqa: F401
    rrd = None
    for n in sorted(os.listdir(bundle_dir)):
        if n.endswith(".rrd"):
            rrd = os.path.join(bundle_dir, n)
            break
    if rrd is None:
        raise SystemExit("no .rrd in %s" % bundle_dir)
    _scalars, images, _depth = read_rrd(rrd)
    for fi in sorted(images):
        yield fi, images[fi]


# ---- task pre/post (the model I/O contracts; exact parse is VERIFY WITH MODEL) -------------------
def _letterbox(img, size):
    import cv2
    h, w = img.shape[:2]
    s = min(size / h, size / w)
    nh, nw = int(round(h * s)), int(round(w * s))
    r = cv2.resize(img, (nw, nh))
    out = np.full((size, size, 3), 114, np.uint8)
    out[:nh, :nw] = r
    return out, s


def task_detect(runner, frames, out_dir, conf=0.30):
    """YOLO11n -> person (class 0) boxes per frame -> masks/person_boxes.jsonl. Standard YOLO11 output
    [1, 84, N] (4 box + 80 cls, transposed); person = class 0. Greedy NMS (IoU 0.45) after score
    filter. Artifact: one JSON line per frame."""
    size = 640
    os.makedirs(os.path.join(out_dir, "masks"), exist_ok=True)
    path = os.path.join(out_dir, "masks", "person_boxes.jsonl")
    n_frames = 0
    with open(path, "w") as f:
        for fi, img in frames:
            lb, s = _letterbox(img, size)
            x = np.transpose(lb.astype(np.float32) / 255.0, (2, 0, 1))[None]  # [1,3,640,640]
            out = runner.run(x)[0]                    # [1,84,N] (model-dependent)
            boxes = _yolo_person_boxes(out, s, conf)  # [[x1,y1,x2,y2,score],...] in ORIGINAL px
            f.write(json.dumps({"frame_idx": int(fi), "person_boxes": boxes}) + "\n")
            n_frames += 1
    return {"artifact": "masks/person_boxes.jsonl", "frames": n_frames, "note": "all-person (bystanders incl.)"}


def _yolo_person_boxes(out, scale, conf, iou_thres=0.45):
    """Parse YOLO11 output for class-0 (person) boxes, un-letterboxed to original px, then NMS.
    Best-effort + tolerant of the [1,84,N] / [1,N,84] layout ambiguity; exact validation is
    VERIFY WITH MODEL. Council #15: without NMS, duplicate/overlapping boxes poison masks."""
    a = np.asarray(out)
    a = a[0] if a.ndim == 3 else a
    if a.shape[0] in (84, 85):                        # [84,N] -> transpose to [N,84]
        a = a.T
    boxes = []
    for row in a:
        if row.shape[0] < 5:
            continue
        person_score = float(row[4])                  # class-0 score column (84-layout: 4=first class)
        if person_score < conf:
            continue
        cx, cy, w, h = [float(v) / max(scale, 1e-9) for v in row[:4]]
        boxes.append([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, person_score])
    return _nms_xyxy(boxes, iou_thres)


def _nms_xyxy(boxes, iou_thres):
    """Greedy NMS on [x1,y1,x2,y2,score]. Pure numpy; never raises."""
    if not boxes:
        return []
    arr = np.asarray(boxes, dtype=np.float64)
    order = arr[:, 4].argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(boxes[i])
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(arr[i, 0], arr[rest, 0])
        yy1 = np.maximum(arr[i, 1], arr[rest, 1])
        xx2 = np.minimum(arr[i, 2], arr[rest, 2])
        yy2 = np.minimum(arr[i, 3], arr[rest, 3])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        area_i = max(0.0, (arr[i, 2] - arr[i, 0]) * (arr[i, 3] - arr[i, 1]))
        area_r = np.maximum(0.0, (arr[rest, 2] - arr[rest, 0]) * (arr[rest, 3] - arr[rest, 1]))
        union = area_i + area_r - inter + 1e-9
        iou = inter / union
        order = rest[iou <= iou_thres]
    return keep


def task_depth(runner, frames, out_dir):
    """Depth-Anything-V2-Small -> per-frame mono depth (relative) -> depth_mono/<frame>.npy. Input
    typically 518 or 224; output HxW depth. Exact norm is VERIFY WITH MODEL."""
    import cv2
    size = int(runner.in_shape[-1]) if isinstance(runner.in_shape[-1], int) else 518
    os.makedirs(os.path.join(out_dir, "depth_mono"), exist_ok=True)
    n = 0
    for fi, img in frames:
        x = np.transpose(cv2.resize(img, (size, size)).astype(np.float32) / 255.0, (2, 0, 1))[None]
        d = np.asarray(runner.run(x)[0]).squeeze()
        np.save(os.path.join(out_dir, "depth_mono", "%06d.npy" % int(fi)), d.astype(np.float32))
        n += 1
    return {"artifact": "depth_mono/", "frames": n, "note": "relative mono depth (validate/fill robot depth)"}


def task_segment(runner, frames, out_dir):
    """SegFormer-B0 -> per-frame semantic label map -> semseg/<frame>.png (uint8 class ids). Input 512;
    output [1,C,h,w] logits -> argmax. Exact class set is VERIFY WITH MODEL."""
    import cv2
    size = 512
    os.makedirs(os.path.join(out_dir, "semseg"), exist_ok=True)
    n = 0
    for fi, img in frames:
        x = np.transpose(cv2.resize(img, (size, size)).astype(np.float32) / 255.0, (2, 0, 1))[None]
        logits = np.asarray(runner.run(x)[0])
        labels = logits[0].argmax(0).astype(np.uint8) if logits.ndim == 4 else logits.squeeze().astype(np.uint8)
        cv2.imwrite(os.path.join(out_dir, "semseg", "%06d.png" % int(fi)), labels)
        n += 1
    return {"artifact": "semseg/", "frames": n, "note": "scene semantics for the map/geofence"}


_TASK_FN = {"detect": task_detect, "depth": task_depth, "segment": task_segment}


def run_task(task, model_path, bundle_dir, out_dir):
    runner = OnnxRunner(model_path)
    t0 = time.time()
    metrics = _TASK_FN[task](runner, bundle_frames(bundle_dir), out_dir)
    manifest = {"task": task, "model": os.path.basename(model_path),
                "provider_used": runner.provider_used, "providers_available": available_providers(),
                "wall_s": round(time.time() - t0, 2), **metrics}
    with open(os.path.join(out_dir, "npu_%s_manifest.json" % task), "w") as f:
        json.dump(manifest, f, sort_keys=True, indent=2)
    print("NPU %s: provider=%s frames=%s -> %s"
          % (task, runner.provider_used, metrics.get("frames"), metrics.get("artifact")))
    return manifest


# ---- selftest: trivial ONNX proves the runtime + provider selection + the pipeline --------------
def _selftest():
    import tempfile
    import onnx
    from onnx import helper, TensorProto
    print("available EPs:", available_providers())
    print("selected order:", select_providers())
    # a trivial [1,4] -> Relu graph; run it through OnnxRunner (whatever EP is available -> CPU here).
    xi = helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4])
    yo = helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 4])
    node = helper.make_node("Relu", ["x"], ["y"])
    graph = helper.make_graph([node], "t", [xi], [yo])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    d = tempfile.mkdtemp(prefix="npu-st-")
    try:
        mp = os.path.join(d, "trivial.onnx")
        onnx.save(model, mp)
        r = OnnxRunner(mp)
        out = r.run(np.array([[-1.0, 2.0, -3.0, 4.0]], np.float32))[0]
        assert np.allclose(out, [[0.0, 2.0, 0.0, 4.0]]), out
        assert r.provider_used in available_providers()
        assert select_providers()[-1] == "CPUExecutionProvider"     # CPU always the final fallback
        # Council #15: NMS suppresses a near-duplicate person box.
        raw = [
            [0.0, 0.0, 10.0, 10.0, 0.9],
            [1.0, 1.0, 11.0, 11.0, 0.8],   # high IoU with first
            [50.0, 50.0, 60.0, 60.0, 0.7],
        ]
        kept = _nms_xyxy(raw, 0.45)
        assert len(kept) == 2 and kept[0][4] == 0.9 and kept[1][4] == 0.7, kept
        print("  ran a trivial ONNX via", r.provider_used, "-> OK")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    for tk in TASKS:
        p = sub.add_parser(tk)
        p.add_argument("--model", required=True, help="the INT8/BF16-quantized ONNX model")
        p.add_argument("--bundle", required=True, help="a run bundle dir (reads its .rrd RGB)")
        p.add_argument("--out", required=True, help="output dir (usually the same runs/<id>)")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "selftest" or a.cmd is None:
        _selftest(); print("NPU-INFER-SELFTEST-OK"); return 0
    run_task(a.cmd, a.model, a.bundle, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
