# TRAIN_CONTRACT.md -- the frozen P7.2 train-side contract (charter S5, spec-only)

**Status:** S5 (`train_act.py`, the ACT training skeleton) is **DEFERRED** per
`docs/SCAFFOLD_CHARTER.md` -- the training code is authored **on the desktop** against THIS
frozen contract; this document plus `eval/checkpoint_contract.py` ship blind from the laptop.
Everything marked FROZEN below is binding on three consumers: the future `train.py` (P7.2),
the ONNX export (P7.3), and the Jetson shadow node (P7.4). Changing a FROZEN item requires a
version bump here AND in `eval/checkpoint_contract.py`, with a migration note.

**Enforcement module -- read this first:** `eval/checkpoint_contract.py` (stdlib-only,
torch-agnostic). `train.py` builds its save payload with `build_payload(...)`; P7.3 and P7.4
call `validate_payload(payload, expect_stats_hash=<dataset card's stats_hash>)` BEFORE using
the weights. Because **all three import the ONE module**, the norm-mismatch bug (checkpoint
z-scored against stats A, deployed against stats B -- the classic "great in training, dead on
the robot" failure named in `docs/COMPUTE_PLACEMENT.md` section 3) is **structurally
impossible**, not procedurally avoided. Never re-type these checks inline; import them.

Self-test (no torch, no data): `python eval/checkpoint_contract.py selftest` ->
`CKPT-CONTRACT-SELFTEST-OK`. Per the charter, run it on the desktop immediately after the
Python env exists and before any real data work -- blind-authored code is presumed broken
until its self-test passes there.

---

## 1. Checkpoint payload (FROZEN)

The checkpoint is `torch.save()` of ONE plain dict. Every value except `state_dict` must be
JSON-serializable (the selftest asserts the non-tensor payload round-trips through `json`
byte-stably), so tooling without torch can still audit a payload's provenance fields.

| key | type | meaning |
|---|---|---|
| `state_dict` | dict | Model weights (`model.state_dict()`). The contract module only checks dict-ness -- it stays torch-blind. |
| `norm_stats` | dict | The **verbatim parsed content** of the dataset's `meta/stats.json` (`json.load` of the exact file that was hashed). Carried whole so deployment never re-reads a possibly-different file. |
| `stats_hash` | str | sha256 hex of the **exact file bytes** of `meta/stats.json` (definition lives in `sha256_file()`; identical to the hash `eval/batch_ingest.py` writes into the dataset card). |
| `dataset_version` | str | e.g. `k1_follow_v1`. Consumers refuse on mismatch (section 6). |
| `splits_source` | str | Identity of the `splits.json` consumed, e.g. `k1_follow_v1/meta/splits.json`. RECOMMENDED: append `@sha256:<sha256_file(splits.json)>`. |
| `config` | dict | The complete training config, including the four MUST-DECIDE fields of section 5 (presence enforced by `validate_payload`). |
| `git_sha` | str | Repo SHA of the training code, or the honest string `nogit` -- never a fabricated SHA (same rule as the dataset card). |
| `torch_version` | str | e.g. `2.12.1+cu126`. |
| `env` | dict | e.g. `{"python": ..., "platform": ..., "cuda": ..., "cudnn": ...}`. |

**Hash comparison rule (load-bearing):** `stats_hash` is compared as a **string** --
checkpoint vs dataset-card vs `sha256_file()` of an on-disk `meta/stats.json`. Nobody ever
re-serializes the parsed `norm_stats` dict to recompute a hash: key order and float
formatting would silently diverge and turn the gate into noise.

---

## 2. Dataset consumption rules (FROZEN)

The dataset is the RAW LeRobot-v3 layout minted by `eval/batch_ingest.py` (parquet + mp4 +
meta json -- see `eval/rrd_to_lerobot.py::write_raw` for the field shapes). Feature contract
(frozen in `docs/COMPUTE_PLACEMENT.md` section 3): `observation.state` = `[range_m,
bearing_deg, rsrc_is_depth, anchor_sim, conf, depth_fps, fsm_state_id]` float32 `[7]`;
`action` = `[vx, vyaw]` float32 `[2]` (`vy` is identically 0 and **omitted** -- do not re-add
it); `observation.images.head_rgb` mp4.

- **`meta/splits.json` is READ, NEVER RE-SPLIT.** Schema (charter S1): `{"version": 1,
  "dataset_version": "k1_follow_vN", "seed": <int>, "method":
  "seeded_shuffle_sorted_run_ids", "val_fraction": <f>, "train": [run_id...], "val":
  [run_id...]}`, both lists sorted, keyed by `run_id` (not `episode_index`). `train.py` maps
  run_ids to episodes via the card's `run_id -> episode_index` map. No fallback random
  split, no `--resplit` flag, ever: the split is episode-level BECAUSE adjacent ~10 Hz
  frames are near-duplicates and a frame-level split leaks val into train. Missing
  `splits.json`, an empty `val` list, or a run_id absent from the data dir = **refuse**.
- **`meta/stats.json` is TRAIN-split-only** (minted that way by `batch_ingest`). Val frames
  are normalized with these same TRAIN stats -- never recomputed over val.
- **Never hardcode channel indices.** `stats.json` carries a `normalize` block naming which
  state channels are z-scored vs passthrough (`rsrc_is_depth` idx 2 and `fsm_state_id` idx 6
  are passthrough -- a binary flag and a categorical id must not be z-scored). `train.py`
  reads that block; if it is absent, refuse -- do not guess.
- **Record `stats_hash` at load time** with `sha256_file("<dataset>/meta/stats.json")` and
  carry both the hash and the parsed dict into the payload via `build_payload`.
- **FSM sanity:** every `fsm_state_id` value consumed must map to a frozen id in
  `eval/fsm_states.json` (via `eval/fsm_groups.py`, section 3). `batch_ingest` aborts the
  mint on any unmapped value, so seeing `UNMAPPED` here means a corrupted or hand-edited
  dataset -- **refuse**, do not skip frames.

---

## 3. Per-FSM-state validation report (required output of every training run)

An aggregate val MSE hides exactly the failure that matters on this robot: a policy that is
great in TRACK and garbage in REACQUIRE looks "fine" on average because TRACK dominates the
frames. Every training run therefore emits a per-FSM-state val report (stdout + a
`<checkpoint>.val_report.json` written next to the checkpoint).

- **Grouping comes from `eval/fsm_groups.py` -- the PINNED shared helper** (also used by the
  `batch_ingest` card and the future P7.5 grader; one source of grouping truth). Interface:
  `ID_TO_NAME`, `name_for_id(value, tol=1e-6) -> str`, `count_ids(values) -> dict
  name->int`. Canonical-name rules: id `1.0` reports as **`SEARCH_MARKER`** (`SEARCH` is a
  documented alias for the same logical state); `SEARCHING` = `1.5` is a **distinct** state;
  anything not within tol of a frozen id (including `-1.0`) returns `UNMAPPED` and means a
  data bug -> abort per section 2. Never hand-roll a second id->name map.
- **Per state, report:** the val frame count AND the val MSE (per action dim, `vx` in m/s
  and `vyaw` in rad/s -- raw units, so the numbers are physically readable; normalized-space
  MSE may be reported additionally).
- **Frozen metric definition:** val MSE is **one-step** -- the first predicted action of the
  chunk against the pairing-convention target (section 5). This makes the number comparable
  across `chunk_consumption` choices instead of quietly changing meaning with the executor.
- **INSUFFICIENT floors:** a state with fewer than `per_state_floor` val frames (config key;
  default 30, recorded in the report) renders as `INSUFFICIENT (n=<count> < floor=<N>)` --
  never an MSE computed from a handful of samples, and never silently pooled into another
  state. An INSUFFICIENT row is honest information for run-scheduling (which FSM states need
  more capture runs), not a failure.
- **Expected shape of v1 reports:** `SEARCH_MARKER` counts will be near-zero **by
  construction** -- the episode trim (inherited from `rrd_to_lerobot.assemble_episode`)
  drops pre-lock SEARCH frames so `_ffill`'s 0.0 seed cannot fabricate `range=0.0` rows. An
  INSUFFICIENT `SEARCH_MARKER` row is therefore expected, not a bug; the card documents the
  same trim rule.

---

## 4. Image normalization (FROZEN mechanism; value decided once, then immutable per checkpoint)

One fixed image-normalization constant, recorded in the checkpoint as `config.image_norm`
(e.g. `{"scale": 255.0}` for scale-to-[0,1], optionally with `mean`/`std` lists). Whatever
the desktop picks: P7.3 bakes the **same** constants into the exported ONNX (in-graph or in
ONNX metadata -- but decided from `config.image_norm`, not re-chosen), and the P7.4 shadow
node applies them **from the checkpoint/export**, never from its own default. There are no
dataset-computed image stats in v1 (charter S1: state+action stats only), so this constant
is the ONLY image normalization -- a mismatch anywhere in the chain is silent garbage.

---

## 5. MUST-DECIDE-BEFORE-P7.2/P7.3 fields (named config keys; presence enforced)

These four decisions change the effective train/test distribution and the Jetson latency
budget. They are **not decided in this document** (that is the desktop's call, made with an
interpreter and the real data in hand) -- but every checkpoint MUST record them as `config`
keys, and `eval/checkpoint_contract.py::CONFIG_MUST_DECIDE` refuses any payload missing one.

| config key | decision to make | why it must be decided before P7.2/P7.3 |
|---|---|---|
| `chunk_size` | K, the number of future actions an ACT forward pass predicts. | Fixes the model output shape; P7.3 exports that shape. |
| `chunk_consumption` | How the executor consumes a chunk. Pick ONE: `replan_every_tick` (predict K, execute 1, re-predict), `execute_full_chunk` (execute all K, then re-predict), `temporal_ensemble` (ACT-paper exponential blending over overlapping chunks). | Changes closed-loop behavior AND the P7.3b latency budget -- `replan_every_tick` costs a forward pass every control tick on the Orin; `temporal_ensemble` needs a K-deep buffer in the shadow node. P7.4 must implement the SAME rule the number was trained/validated under. |
| `obs_action_pairing` | The supervision target relative to the dataset's tick convention. The dataset (charter S1 card) pairs `action_t` = the command emitted at the **same control tick** as `obs_t`. Decide whether the model's first predicted action for `obs_t` is `action_t` (same-tick: imitates "what control.py output given this obs") or `action_(t+1)` (next-tick). | The P7.4 shadow comparison must pair shadow output against the live command stream with the **identical** convention, or the P7.5 agreement number is off-by-one and meaningless. |
| `image_norm` | The fixed image-normalization constant (section 4). | P7.3/P7.4 replicate it verbatim. |

---

## 6. Refusal matrix (fail-closed -- same doctrine as the rest of the repo)

| condition | who detects | action |
|---|---|---|
| Requested dataset_version != card's dataset_version | `train.py` at load | Refuse. Never "close enough". |
| `meta/splits.json` missing, empty `val`, or run_id not in data | `train.py` at load | Refuse. Never re-split. |
| `normalize` block absent from `meta/stats.json` | `train.py` at load | Refuse. Never guess channel indices. |
| `UNMAPPED` FSM id in any consumed frame | `train.py` / val report | Refuse -- data bug (`batch_ingest` should have made this impossible). |
| Payload missing a required key / MUST-DECIDE config field | `validate_payload` (all consumers) | Refuse with the LOUD `CKPT-CONTRACT VIOLATION` message. |
| Checkpoint `stats_hash` != dataset card's `stats_hash` | `validate_payload(expect_stats_hash=...)` in P7.3 export and P7.4 shadow load | Refuse -- the norm-mismatch gate. The shadow node refuses to START; it never degrades to "run anyway". |

---

## 7. Environment (restated from `docs/WORKSTATION_SETUP.md` -- read it before installing)

- **Do NOT install lerobot.** It pins `torch>=2.7,<2.12`, which directly conflicts with the
  P7 torch pin; the RAW LeRobot-v3 layout is consumed directly (pyarrow + mp4 via
  imageio-ffmpeg). `train.py` must never `import lerobot`.
- **torch for the 3080 (Ampere sm_86) comes from the cu126 index:**
  `torch==2.12.1+cu126` (verified fallback `2.11.0+cu126`). NEVER bare `pip install torch`
  -- the default 2.12-line wheel is CUDA-13.0/Blackwell and wrong for this GPU.
- Python 3.12 is the keystone pin (Open3D constraint; torch/pyarrow fine on it).
- **No TensorRT on the desktop.** P7.3 stops at ONNX + an ORT-vs-torch parity check; the
  FP16 engine is built on the Jetson only (P7.3b), per the plan invariants.

---

## 8. Consumers and their obligations

| consumer | obligation |
|---|---|
| `train.py` (P7.2, desktop-authored) | Load dataset per section 2; save every checkpoint via `build_payload(...)`; emit the section-3 val report; record MUST-DECIDE fields in `config`. |
| P7.3 ONNX export (desktop) | `validate_payload(ckpt, expect_stats_hash=<card hash>)` before export; bake `image_norm` + carry `dataset_version`/`stats_hash`/`git_sha` into ONNX metadata; parity-check ORT vs torch on fixed inputs. |
| P7.4 shadow node (Jetson) | `validate_payload` at startup with the deployed dataset card's hash; refuse to start on any violation; apply `image_norm` + normalization from the payload's `norm_stats` verbatim; implement `chunk_consumption` / `obs_action_pairing` exactly as recorded. **Shadow never actuates -- no bridge code path, absent not flagged off** (plan invariant, restated for completeness). |

---

## 9. Deferred / non-binding (so nobody mistakes guidance for contract)

- Model internals (CVAE wiring, encoder choice, layer sizes, optimizer, schedules) are the
  desktop's call -- recorded in `config`, not constrained here.
- Checkpoint file naming: `checkpoints/<dataset_version>/...` is suggested, not binding.
- `train.py` selftest expectations (from the charter DEFER row, shipped from the desktop):
  CPU overfit on a tiny dataset minted by `eval/batch_ingest.py` over `eval/synth_fixtures.py`
  bundles; assert loss decreases; round-trip the checkpoint through `validate_payload`
  including a deliberate stats-hash mismatch raising loudly; render the per-state report
  including an INSUFFICIENT row (the S6 under-floor fixture exists for exactly this).
