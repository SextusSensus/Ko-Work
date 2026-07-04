# Item-labeling intelligence — plan

**Status:** design (2026-07-04). Beefs up the offline labeling brain past the fixed COCO-80 ceiling.
Extends `OBSTACLE_LABELING_PLAN.md` / `eval/rrd_label.py`. Governed by embodied-ai-advisor (model-fit
+ honest thesis framing) and the data-pipeline lens. Offline-first: this is the capture-QUALITY half
(the thesis-aligned direction), where compute is relaxed — heavy models are fine.

## The ceiling today
`rrd_label.py` = **COCO-80** (person/chair/fridge/bottle…) + **SegFormer-ADE20K** structure. That is
commodity recognition of common objects. "Labeling intelligence" means breaking four ceilings:
1. **Long tail** — domain items COCO never saw (pallets, produce, tools, SKUs, deli trays).
2. **Fine-grained ID** — "bottle" → "1 L detergent", "box" → "case of 24".
3. **Text** — labels, signs, price tags, SKUs, expiry dates.
4. **Attributes** — colour, state (open/closed, full/empty), count.

## The toolkit (verified mid-2026)
- **Open-vocab detection** (breadth, no retraining): **YOLO-World** (fast; runs through the
  ultralytics we already use via `YOLOWorld(...).set_classes([prompts])` — VERIFIED working) and
  **Grounding-DINO 1.5** (heavier, phrase-grounded).
- **VLM per-item understanding**: **Qwen2.5-VL** (localization + OCR + multilingual), **Molmo**
  (1B/7B, grounded *pointing*) — fine-grained label + attributes + state.
- **OCR** (text/SKUs): **GLM-OCR** (0.9B, beats Gemini 3.1 Pro on OCR), **DeepSeek-OCR** (grounding +
  detection, ~97% acc, 20× visual compression).
- **Instance masks**: **SAM2** — precise per-item extent (better 3D than a box).
- **Taxonomy**: a small structured ontology (category → subcategory → attributes) so labels are
  consistent + queryable, not free-text.

## Architecture — a TIERED CASCADE (escalate only where it pays)
Never run a 7B VLM on every pixel. Per region, cheap → expensive:
```
 T0  COCO YOLO (have it)          common objects, fast                       every frame
 T1  Open-vocab (YOLO-World)      the domain long tail, by text prompt       every frame (cheap-ish)
 T2  VLM (Qwen2.5-VL / Molmo)     fine-grained label + attributes + point    novel/uncertain regions only
 T3  OCR (GLM-OCR)                SKUs / signs / labels                      text-bearing regions only
 T+  SAM2                         instance mask -> precise 3D extent         the items that matter
```
- **Output: a structured item record** —
  `{coco_class, open_vocab_label, fine_label, attributes[], text, mask, xyz, range_m, conf, tier}` —
  keyed to the map ([[k1-odometry-mapping]]) so each item has a world position.
- **Rerun becomes the QA/review viewer**: surface low-confidence / novel labels for human-in-the-loop
  correction — the labeling *and* the labeling-review both live on the scrubber.
- Feeds downstream: the obstacle set (Phase 2), the map (`rrd_map.py`), and the capture dataset.

## Phases
### Phase A — Open-vocab breadth (T1) — ✅ BUILDING 2026-07-04 (effort S)
`rrd_label.py --open-vocab "pallet,cardboard box,forklift,..."` → YOLO-World detects the prompt list,
fused with depth + tracked exactly like the COCO path. Highest leverage, cleanest integration
(ultralytics-native). Domain prompt-lists become a per-environment config.

### Phase B — Structured item records + taxonomy (effort S)
Merge T0+T1 into one item record against a small taxonomy; dedup in the world frame (via the map).
The representation the dataset + any downstream consumer reads.

### Phase C — VLM enrichment (T2), region-gated (effort M)
Run a VLM only on novel/uncertain/interesting regions → fine-grained label + attributes + a grounded
point. Cloud/HF for the 7B+ models; Molmo-1B/Florence-2 laptop-local for a cheaper pass.

### Phase D — OCR (T3) + masks (SAM2) (effort M)
OCR on text-bearing regions (SKUs/signs); SAM2 masks for precise 3D extent on the items that matter.

### Phase E — Active review + capture (effort S)
Rerun QA pass over low-confidence labels (human-in-the-loop) → a corrected, structured item dataset.
Pipeline-proof for the real dual-POV/LiDAR/skeletal rig's labeling ingest.

## Thesis-fit (honest — embodied-ai-advisor)
- **TRUE / SELLABLE:** rich, structured spatial-semantic item records on *real-work* environments
  (deli/warehouse/retail) — the differentiated long tail the world-model moat rests on.
- **CAVEAT (diligence-killer):** generic open-vocab labeling is becoming commodity. The moat is the
  **fusion** (label + 3D + attributes + text, anchored in the map) × **breadth of environments**, not
  the detector. Do NOT pitch "we label objects" — pitch "we capture structured spatial-semantic
  records of real work." The single-forward-POV + flaky-depth caveat still holds until the real rig.

## Compute
Offline, laptop-first for the small models (YOLO-World, GLM-OCR-0.9B, Molmo-1B, Florence-2 — like
SegFormer); cloud / Hugging Face (Inference / Jobs batch) for the 7B+ VLMs; the K1 Orin GPU is an
option for idle-time batch labeling BUT the `.rrd` read path needs rerun 0.33 (the robot is pinned
0.23.1 — see [[k1-odometry-mapping]]/[[rerun-integration]]), so the label pipeline runs on the laptop.

## Open decisions
1. Domain prompt-lists: which environments first (deli / warehouse / retail / home)?
2. VLM host: laptop-local small (Molmo-1B/Florence-2) vs cloud/HF (Qwen2.5-VL-7B+)?
3. Taxonomy: adopt an existing ontology or a minimal custom one keyed to the target environments?
