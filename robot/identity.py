"""Appearance identity (P3.2/seam-5): the anchored appearance gallery + distractor bank
(TargetGallery), the model-free colour/striped descriptors, and the OSNet deep-ReID engine
(ReidEngine, onnxruntime TensorRT EP, lazily imported). Extracted verbatim (pure move).
"""
import collections
import math
import os

import numpy as np
import cv2

from common import clamp, log

class TargetGallery:
    def __init__(self, anchor_feat, feat_fn, sim_fn,
                 gallery_size, distractor_size,
                 anchor_floor, bank_floor, admit_conf, admit_spacing):
        self.anchor_feat = anchor_feat          # may be None; tolerated
        self.feat_fn = feat_fn
        self.sim_fn = sim_fn
        self.gallery = collections.deque(maxlen=max(0, gallery_size - 1))
        self.distractors = collections.deque(maxlen=max(0, distractor_size))
        self._anchor_floor = float(anchor_floor)
        self._bank_floor = float(bank_floor)
        self._admit_conf = float(admit_conf)
        self._admit_spacing = int(admit_spacing)
        self.last_admit_frame = -10**9

    def anchor_pass(self, feat):
        """Slot-0 HARD gate. Mirrors the legacy veto disable path: True when the
        veto is disabled (anchor_floor <= 0) or no anchor exists."""
        try:
            if self.anchor_feat is None or self._anchor_floor <= 0.0:
                return True
            return self.sim_fn(self.anchor_feat, feat) >= self._anchor_floor
        except Exception:  # noqa: BLE001
            return False   # feature fault -> caller vetoes -> degrade safe

    def score(self, feat):
        """Return (anchor_sim, g_sim, d_sim). anchor_sim FIRST and SEPARATE so the
        caller applies the AND-veto on anchor_sim ALONE (never on g_sim).
        g_sim = max(anchor_sim, gallery sims) >= anchor_sim -> can only LOWER cost."""
        try:
            a = self.sim_fn(self.anchor_feat, feat) if self.anchor_feat is not None else 0.0
            g = a
            for v in self.gallery:
                s = self.sim_fn(v, feat)
                if s > g:
                    g = s
            d = 0.0
            for v in self.distractors:
                s = self.sim_fn(v, feat)
                if s > d:
                    d = s
            return a, g, d
        except Exception:  # noqa: BLE001
            return 0.0, 0.0, 0.0

    def relock_score(self, feat, view_floor, dedup_ceiling):
        """Multi-shot re-ID primitive for ARMED markerless re-lock. Returns
        (anchor_sim, g_sim, d_sim, k); a/g/d are IDENTICAL to score(), and k = the count
        of MUTUALLY-DISSIMILAR admitted gallery views that are BOTH (i) strong frozen-anchor
        matches (their own anchor sim >= view_floor -- recomputed, deterministic since both
        feats are immutable) AND (ii) match the candidate >= view_floor. anchor_feat is
        READ-ONLY here. Fails CLOSED -> (0,0,0,0): a fault can only DENY a re-lock, never
        grant one. k is the independent-evidence count the caller needs to relax the anchor
        slot-0 gate WITHOUT relying on g_sim (which collapses anchor+gallery into one max)."""
        try:
            a = self.sim_fn(self.anchor_feat, feat) if self.anchor_feat is not None else 0.0
            g = a
            for v in self.gallery:
                s = self.sim_fn(v, feat)
                if s > g:
                    g = s
            d = 0.0
            for v in self.distractors:
                s = self.sim_fn(v, feat)
                if s > d:
                    d = s
            matched = []                      # STRONG admits that also match the candidate
            for v in self.gallery:
                if self.anchor_feat is not None and self.sim_fn(self.anchor_feat, v) < view_floor:
                    continue                  # weak admit (banked near bank_floor) cannot vouch
                if self.sim_fn(v, feat) >= view_floor:
                    matched.append(v)
            kept = []                         # collapse near-duplicate viewpoints -> independence
            for vf in matched:
                if all(self.sim_fn(vf, kf) < dedup_ceiling for kf in kept):
                    kept.append(vf)
            return a, g, d, len(kept)
        except Exception:  # noqa: BLE001
            return 0.0, 0.0, 0.0, 0

    def admit(self, feat, conf, jump, ema_max_jump, isolated, frame_idx):
        """Append a same-person view. NEVER touches anchor_feat. Isolation is
        necessary, NOT sufficient (conf + non-teleport + spacing + bank_floor too)."""
        try:
            if feat is None or not isolated:
                return False
            if conf < self._admit_conf:
                return False
            if jump > ema_max_jump:
                return False
            if (frame_idx - self.last_admit_frame) < self._admit_spacing:
                return False
            if self.anchor_feat is not None and self.sim_fn(self.anchor_feat, feat) < self._bank_floor:
                return False
            if self.gallery.maxlen == 0:      # --gallery-size 1 -> anchor-only
                return False
            self.gallery.append(feat)
            self.last_admit_frame = frame_idx
            return True
        except Exception:  # noqa: BLE001
            return False

    def add_distractor(self, feat, isolated):
        """Bank a confidently-other-person view. Require sim < anchor_floor (NOT
        just < bank_floor) so a color-shifted target view that still clears the
        follow-veto can never be banked against itself. Caller ALSO guards on
        track_id != seed.track_id."""
        try:
            if feat is None or not isolated:
                return False
            if self.distractors.maxlen == 0:
                return False
            if self.anchor_feat is not None:
                s = self.sim_fn(self.anchor_feat, feat)
                if s >= self._bank_floor or s >= self._anchor_floor:
                    return False
            self.distractors.append(feat)
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Color signature: normalized HS histogram of the bbox (HSV). cv2.calcHist.
# ---------------------------------------------------------------------------
def color_hist(frame, box):
    try:
        h_img, w_img = frame.shape[:2]
        x1, y1, x2, y2 = box
        x1 = int(clamp(x1, 0, w_img - 1)); x2 = int(clamp(x2, 1, w_img))
        y1 = int(clamp(y1, 0, h_img - 1)); y2 = int(clamp(y2, 1, h_img))
        if x2 <= x1 or y2 <= y1:
            return None
        roi = frame[y1:y2, x1:x2]
        if roi.size == 0:
            return None
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return hist
    except Exception:  # noqa: BLE001
        return None


def hist_similarity(h1, h2):
    """Correlation similarity in [0,1] (clamped). 1 = identical color profile."""
    if h1 is None or h2 is None:
        return 0.0
    try:
        s = cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL)
        if math.isnan(s):
            return 0.0
        return clamp(s, 0.0, 1.0)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Stage 4 (MODEL-FREE) -- PART-BASED appearance descriptor. The single global HS
# histogram (color_hist) is blind to color LAYOUT: a red-top/blue-bottom person and
# a blue-top/red-bottom one hash to nearly the same global histogram, so they tie
# (failure mode A). Splitting the bbox into vertical bands and histogramming each
# captures the layout that separates similarly-coloured people. Pure OpenCV/numpy --
# NO new model file. Side-background leak is suppressed by keeping only a central
# horizontal slab. The feature is ONE flat float32 vector, so it EMA-blends and
# stores exactly like the global hist; similarity is cosine in [0,1] -- a drop-in for
# the feat_fn/sim_fn swap point built in Stage 2. (A learned embedding (OSNet) would
# be strictly stronger, but that is the "new model" this stage deliberately avoids;
# identical-uniform crowds therefore stay hard -- see --arm-reacquire.)
# ---------------------------------------------------------------------------
STRIPE_BANDS  = 3      # vertical bands (head+shoulders / torso / legs)
STRIPE_HBINS  = 16     # H bins per band
STRIPE_SBINS  = 16     # S bins per band
STRIPE_KEEP_W = 0.7    # central width fraction kept (drops side-background)


def striped_feat(frame, box):
    """Vertical-band HS histogram over a central slab, returned as one L1-normalized
    float32 vector (STRIPE_BANDS*STRIPE_HBINS*STRIPE_SBINS long). None on failure --
    callers already treat None as 'no signature this frame' (safe)."""
    try:
        h_img, w_img = frame.shape[:2]
        x1, y1, x2, y2 = box
        x1 = int(clamp(x1, 0, w_img - 1)); x2 = int(clamp(x2, 1, w_img))
        y1 = int(clamp(y1, 0, h_img - 1)); y2 = int(clamp(y2, 1, h_img))
        if (x2 - x1) < 4 or (y2 - y1) < STRIPE_BANDS * 4:
            return None
        # Drop the side margins (most likely background) before histogramming.
        mx = int((x2 - x1) * (1.0 - STRIPE_KEEP_W) * 0.5)
        cx1, cx2 = x1 + mx, x2 - mx
        if (cx2 - cx1) < 2:
            cx1, cx2 = x1, x2
        roi = frame[y1:y2, cx1:cx2]
        if roi.size == 0:
            return None
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        bh = hsv.shape[0]
        parts = []
        for b in range(STRIPE_BANDS):
            ya = (bh * b) // STRIPE_BANDS
            yb = (bh * (b + 1)) // STRIPE_BANDS
            band = hsv[ya:yb]
            if band.size == 0:
                parts.append(np.zeros(STRIPE_HBINS * STRIPE_SBINS, dtype=np.float32))
                continue
            hbnd = cv2.calcHist([band], [0, 1], None,
                                [STRIPE_HBINS, STRIPE_SBINS], [0, 180, 0, 256])
            parts.append(hbnd.flatten())
        feat = np.concatenate(parts).astype(np.float32)
        s = float(feat.sum())
        if s > 0.0:
            feat /= s                      # L1-normalize (area / exposure invariant)
        return feat
    except Exception:  # noqa: BLE001
        return None


def striped_sim(f1, f2):
    """Cosine similarity in [0,1] over two striped feature vectors. 0.0 on None /
    shape-mismatch / degenerate. Computes the norms internally, so it stays correct
    on an EMA-blended (no longer unit-norm) vector."""
    if f1 is None or f2 is None:
        return 0.0
    try:
        if f1.shape != f2.shape:
            return 0.0
        n1 = float(np.linalg.norm(f1)); n2 = float(np.linalg.norm(f2))
        if n1 <= 0.0 or n2 <= 0.0:
            return 0.0
        c = float(np.dot(f1, f2) / (n1 * n2))
        if math.isnan(c):
            return 0.0
        return clamp(c, 0.0, 1.0)
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Stage 4 (DEEP / OPTIONAL) -- person-ReID embedding backend (e.g. OSNet-x0.25).
# Runs an ONNX model through onnxruntime's TensorRT execution provider at FP16
# (== TRT FP16, the requested acceleration) with CUDA then CPU fallback, reusing
# the onnxruntime dep already present for YOLO -- no pycuda / raw-TRT requirement.
# The TRT EP builds + caches the FP16 engine ON the device on first run (slow once,
# cached after). A learned embedding separates identities a colour histogram cannot
# (failure mode A) and is what makes armed markerless re-acquire (E) trustworthy in
# crowds. Loaded ONCE, warmed up, CRASH-SAFE: if onnxruntime / the model / the
# providers are unavailable, ok=False and the caller falls back to the histogram.
# embed() never raises into the control loop. Similarity is cosine (striped_sim),
# so it drops straight into the feat_fn/sim_fn swap point.
#
# Get an OSNet ONNX (e.g. osnet_x0_25_msmt17.onnx from torchreid / BoxMOT), put it on
# the robot, and run: --appearance osnet --reid-engine /path/to/osnet.onnx
# ---------------------------------------------------------------------------
REID_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
REID_STD  = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)


class ReidEngine:
    """ONNX person-ReID embedder via onnxruntime (TensorRT EP, FP16). ok=False on any
    load failure so the caller degrades to the histogram/striped descriptor."""

    def __init__(self, model_path, input_hw=(256, 128), fp16=True, cache_dir=None,
                 letterbox=True, batch=True):
        self.ok = False
        self.session = None
        self.input_name = None
        self.in_h, self.in_w = int(input_hw[0]), int(input_hw[1])
        self.letterbox = bool(letterbox)
        self._batch_ok = bool(batch)
        self._dyn_batch = False        # True only if the model's batch axis is dynamic AND batch on
        self.providers_active = []
        # ITEM 3: True iff a GPU EP (TensorRT/CUDA) was AVAILABLE but onnxruntime fell back to the
        # CPU EP as the active provider. OSNet re-ID on CPU is far too slow to be trustworthy for an
        # ARMED (driving) re-lock, so the node folds this into forcing re-lock -> audit-only.
        self.cpu_ep_degraded = False
        try:
            if not model_path or not os.path.exists(model_path):
                raise FileNotFoundError("reid model not found: %s" % model_path)
            import onnxruntime as ort
            avail = ort.get_available_providers()
            providers = []
            if "TensorrtExecutionProvider" in avail:
                trt_opts = {"trt_fp16_enable": bool(fp16), "trt_engine_cache_enable": True}
                if cache_dir:
                    trt_opts["trt_engine_cache_path"] = cache_dir
                providers.append(("TensorrtExecutionProvider", trt_opts))
            if "CUDAExecutionProvider" in avail:
                providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            so.log_severity_level = 3
            self.session = ort.InferenceSession(model_path, sess_options=so, providers=providers)
            _in0 = self.session.get_inputs()[0]
            self.input_name = _in0.name
            # A FIXED batch axis (the standard OSNet export is batch=1) forces a per-row loop
            # in embed_batch; a dynamic/symbolic axis lets us run all crops in one inference.
            _b0 = _in0.shape[0] if _in0.shape else 1
            self._dyn_batch = (not (isinstance(_b0, int) and _b0 >= 1)) and self._batch_ok
            self.providers_active = list(self.session.get_providers())
            # ITEM 3 (EP assertion): a GPU EP was AVAILABLE but the ACTIVE first provider is the CPU
            # EP == onnxruntime silently fell back (missing TRT/CUDA runtime, engine-build failure,
            # etc.). Flag it so the node can refuse ARMED (driving) re-lock. avail was computed above.
            _gpu_avail = ("TensorrtExecutionProvider" in avail or "CUDAExecutionProvider" in avail)
            _active0 = self.providers_active[0] if self.providers_active else ""
            # An OSNet running on the CPU EP is too slow to trust for a DRIVING re-lock -- refuse arming
            # whether it FELL BACK from an available GPU EP or is a CPU-only ORT build (both cases). Only
            # ever DENIES arming (audit-only); TRACK/COAST identity is unaffected. On a GPU host that keeps
            # a GPU EP active this stays False (byte-identical).
            if _active0 == "CPUExecutionProvider":
                self.cpu_ep_degraded = True
                _why = "cpu-ep fell-back" if _gpu_avail else "cpu-ep cpu-only-build"
                log("REID-DEGRADED %s active=%s avail=%s -> armed re-lock refused (audit-only)"
                    % (_why, _active0, ",".join(avail)))
            # Warm up -- the FIRST infer builds/loads the TRT engine (slow, once).
            dummy = np.zeros((1, 3, self.in_h, self.in_w), dtype=np.float32)
            for _ in range(3):
                self.session.run(None, {self.input_name: dummy})
            self.ok = True
            log("REID-ENGINE ok model=%s providers=%s in=%dx%d"
                % (os.path.basename(model_path), ",".join(self.providers_active),
                   self.in_h, self.in_w))
        except Exception as e:  # noqa: BLE001 -- any failure -> safe histogram fallback
            log("REID-ENGINE load FAILED (%s) -> appearance falls back to histogram" % e)
            self.ok = False

    def _preprocess(self, frame, box):
        h_img, w_img = frame.shape[:2]
        x1, y1, x2, y2 = box
        x1 = int(clamp(x1, 0, w_img - 1)); x2 = int(clamp(x2, 1, w_img))
        y1 = int(clamp(y1, 0, h_img - 1)); y2 = int(clamp(y2, 1, h_img))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        if self.letterbox:
            # Aspect-preserving resize + gray(114) pad. Person crops are tall/narrow, so a
            # straight stretch to 256x128 distorts the body OSNet was trained on -> low
            # same-person cosine. Letterboxing keeps proportions -> stronger, separable embeddings.
            ch, cw = crop.shape[:2]
            s = min(self.in_w / float(cw), self.in_h / float(ch))
            nw = max(1, int(round(cw * s))); nh = max(1, int(round(ch * s)))
            resized = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_LINEAR)
            canvas = np.full((self.in_h, self.in_w, 3), 114, dtype=np.uint8)
            ox = (self.in_w - nw) // 2; oy = (self.in_h - nh) // 2
            canvas[oy:oy + nh, ox:ox + nw] = resized
            crop = canvas
        else:
            crop = cv2.resize(crop, (self.in_w, self.in_h), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        chw = np.transpose(rgb, (2, 0, 1))             # HWC -> CHW
        chw = (chw - REID_MEAN) / REID_STD             # ImageNet normalize
        return chw.astype(np.float32)

    @staticmethod
    def _l2(v):
        v = np.asarray(v, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(v))
        if n <= 0.0 or not np.isfinite(n):
            return None
        return v / n

    def embed(self, frame, box):
        """L2-normalized embedding for one box, or None on any failure (safe)."""
        return self.embed_batch(frame, [box])[0]

    def embed_batch(self, frame, boxes):
        """Return a list (len == len(boxes)) of L2-normalized embeddings, None per failed box.
        ONE session.run((M,3,H,W)) when the model's batch axis is DYNAMIC, else a per-row loop
        (the standard OSNet export is fixed batch=1). NEVER raises -- a fault yields all-None so
        the caller vetoes those candidates (fail-CLOSED, the safe direction). Scatters results
        back by the valid-input index so candidate k can never inherit another crop's vector."""
        out = [None] * len(boxes)
        if not self.ok or not boxes:
            return out
        try:
            tensors = []; idx = []
            for i, b in enumerate(boxes):
                x = self._preprocess(frame, b)
                if x is not None:
                    tensors.append(x); idx.append(i)
            if not tensors:
                return out
            if self._dyn_batch and len(tensors) > 1:
                batch = np.stack(tensors, axis=0)                  # (M,3,H,W) -- one inference
                res = self.session.run(None, {self.input_name: batch})[0]
                res = np.asarray(res, dtype=np.float32).reshape(len(tensors), -1)
                for k, i in enumerate(idx):
                    out[i] = self._l2(res[k])
            else:
                for k, i in enumerate(idx):                        # fixed-batch export -> per row
                    r = self.session.run(None, {self.input_name: tensors[k][None, ...]})[0]
                    out[i] = self._l2(r)
            return out
        except Exception:  # noqa: BLE001 -- never raise into the control loop
            return [None] * len(boxes)


# ---------------------------------------------------------------------------
