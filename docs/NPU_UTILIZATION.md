# NPU utilization — the laptop XDNA2 NPU as an offline-inference pool worker

**Scan (2026-07-10):** `NPU Compute Accelerator Device` (AMD, driver `IpuMcdmDriver`, Status OK) is
present and healthy — the **XDNA2 NPU** in the Ryzen AI 9 270 APU. (The scan also found a **Radeon 780M
iGPU** alongside the RTX 5060 dGPU — a DirectML fallback target.) **No inference stack is installed**
(no onnxruntime, no Ryzen AI / VitisAI, no local ONNX models).

## What the NPU is for (and isn't)

The XDNA2 NPU is an **INT8/BF16 inference** accelerator — not for training (that's the 5060, see
`COMPUTE_PLACEMENT.md`). It excels at **small vision models run in batch, offline**, at low power. That
maps perfectly onto three OPEN items in this project's offline pipeline — so the NPU becomes a
`backend: npu` worker in the LAN pool (`jobspec.BACKENDS` already lists `npu`), running these as job
types on captured `runs/`. None of this touches the robot.

## The three neural networks (each closes a real project gap)

| # | Network | Runs on NPU to… | Closes |
|---|---|---|---|
| **1** | **YOLO11n person detection** (the robot's own `yolo11n.onnx`) | Re-detect **ALL** person boxes offline over each run's captured RGB, so recon can mask **bystanders**, not just the logged target box. | **DECISIONS.md P8.2a** — the person-masking-v0 bystander gap (option (b): re-detect offline). |
| **2** | **Depth-Anything-V2-Small** (monocular depth ViT) | Estimate/validate depth offline where the robot's head depth was **flaky or dead** — cross-check + gap-fill so recon isn't blind on bad-depth runs. | The flaky-depth risk in `RUN_ARTIFACTS.md` / `RECON_CONTRACT.md` (a dead-depth run is currently "not P8-usable"). |
| **3** | **SegFormer-B0** (semantic segmentation) | Label floor / wall / obstacle / person per frame offline → scene semantics for the persistent map + geofence. | The **obstacle-labeling / map-semantics** track (`OBSTACLE_LABELING_PLAN.md`, `rrd_label.py` — which already names SegFormer). |

All three: small, INT8-quantizable, offline-batch — the NPU's sweet spot. They are **consumers of the
same captured `.rrd`/RGB** the rest of P7/P8 uses (a second consumer, per the plan's "same offload,
more consumers" idea), and they **feed** recon (masks + depth) and the map (semantics).

## How to actually run them on the NPU (the setup — VERIFY ON NPU)

1. **Install AMD Ryzen AI Software 1.7.1** (the Unified Installer) from
   [ryzenai.docs.amd.com](https://ryzenai.docs.amd.com/en/latest/) — it provides ONNX Runtime with the
   **VitisAI Execution Provider** (+ the NPU driver/runtime) and a dedicated conda env. This is an AMD
   installer, **not** a pip package.
2. **Quantize each model to INT8** with the **AMD Quark** quantizer
   ([XINT8 recipe](https://ryzenai.docs.amd.com/en/latest/model_quantization.html): symmetric INT8,
   power-of-two scales, MinMSE calibration) using a **calibration set of ~100–300 captured frames** from
   `runs/` (real garage imagery = correct calibration distribution). BF16 is also supported if INT8
   accuracy drops too far.
3. **Run via ONNX Runtime + VitisAIExecutionProvider** — the EP auto-partitions the graph onto the NPU;
   the rest falls to CPU. Our inference utility (below) selects `VitisAIExecutionProvider` if present,
   else falls back `DmlExecutionProvider` (the 780M iGPU / 5060) → `CPUExecutionProvider`, so the code
   runs everywhere and is *accelerated* on the NPU.

**Sources:**
[AMD Ryzen AI Software](https://www.amd.com/en/developer/resources/ryzen-ai-software.html) ·
[Vitis AI EP (onnxruntime)](https://onnxruntime.ai/docs/execution-providers/Vitis-AI-ExecutionProvider.html) ·
[Model Quantization (Quark)](https://ryzenai.docs.amd.com/en/latest/model_quantization.html) ·
[AI inference on Ryzen AI with Quark](https://www.amd.com/en/developer/resources/technical-articles/2025/ai-inference-acceleration-on-ryzen-ai-with-quark.html)

## Pool integration (how these become jobs)

- The laptop registers a worker with `backends: ["cpu", "cuda", "npu"]` (5060 CUDA + XDNA2 NPU +
  CPU). A job with `requires.backend == "npu"` routes here.
- Three job types: `detect` (YOLO person boxes → `runs/<id>/masks/`), `depth` (mono depth →
  `runs/<id>/depth_mono/`), `segment` (SegFormer → `runs/<id>/semseg/`). Each writes a frame-tagged
  artifact recon/the map consumes, with a manifest recording the model + quant recipe + EP used.
- **Resource rule:** the NPU is separate silicon from the 5060, so an `npu` inference job and a `cuda`
  training job can run **concurrently** on the laptop — but both share the 16 GB system RAM, so don't
  also run a memory-heavy recon job here at the same time (`COMPUTE_PLACEMENT.md` rule).

## Status / honesty

- **NPU + driver: confirmed present + OK.**
- **Inference stack: NOT installed** — full NPU execution is `VERIFY ON NPU` after the Ryzen AI SW
  install + per-model INT8 quantization above.
- The **inference utility + the three job types** are authorable now (import-verifiable on CPU/DML;
  the VitisAI EP swaps in after the install) — see the follow-up work. The models themselves: YOLO11n is
  the robot's own; Depth-Anything-V2-Small + SegFormer-B0 are small public ONNX exports.
