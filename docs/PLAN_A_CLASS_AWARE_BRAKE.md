# Plan A — Class-aware obstacle brake

**Status:** implemented (2026-09-11), **default off**. Code path:
`--obstacle-class-brake on` / K1Finder “Class brake” (unticked). Geometry still
triggers; class only tightens. Selftest: `eval/class_brake_selftest.py`.
Field confidence still gated on Batch-Label of laptop runs.
**Never authorizes forward velocity.**

**Gate before field drive:** Batch-Label quality on Todd’s existing `runs\`
(`desktop/Batch-Label-Runs.ps1` → `autotune/batch_label_tally.json`). Do not
enable live class modulation until PASS rate + low `suspect_iddrift` look sane.

---

## 1. Goal

Today `_obstacle_vx_cap` grades `vx` from depth corridor clearance only
(`robot/follow_person_k1.py`). Plan A adds an optional **class modifier**:

- Depth still decides *whether* something is in the way (trigger).
- COCO class of the nearest in-corridor obstacle may only **tighten** the
  effective brake (earlier start / harder stop) — never loosen it.
- Followed operator stays excluded via `obstacle_target_margin` (unchanged).
- Gap steer is **out of scope** (that is Plan B).

Honest name: *class-modulated forward-corridor brake*, not “semantic navigation.”

---

## 2. Non-negotiable invariants

| ID | Rule |
|---|---|
| A1 | Geometry triggers; class only modulates. No depth return → no class-only brake. |
| A2 | Class path may only **reduce** `vx` further (`cap_class ≤ cap_geom`). Never raise clearance / never raise cap. |
| A3 | Compose with `forbid_forward` before **and** after slew (INV-1). |
| A4 | Yaw untouched. Gap steer unchanged. |
| A5 | Label / YOLO fault → degrade to geometry-only (same contract as Rerun `_NullRR`). |
| A6 | Byte-identical when `--obstacle-class-brake off` (default). |
| A7 | Followed person is not an obstacle (`obstacle_target_margin`). Class “person” on the **operator** must not fire; bystander person closer than target may. |
| A8 | Suspect / ID-drift ranges must not drive a harder class brake (aged-median + ignore `suspect_iddrift`-class tips offline; live: require class–depth agreement). |

---

## 3. Class policy (v1 table)

COCO ids via the existing YOLO11n forward pass (today filtered to `PERSON_CLS=0`
in `perception.PersonDetector.detect`).

| Tier | Classes (COCO) | Effect vs geometry-only |
|---|---|---|
| **P0 person** | `person` (0), when **not** the followed target | Tighten: effective `start' = start + Δ_person` (e.g. +0.25 m), same stop |
| **P1 furniture** | chair(56), couch(57), bed(59), dining table(60), toilet(61) | Default geometry (or mild +0.10 m) |
| **P2 appliance / boxy** | tv(62), laptop, suitcase, refrigerator(72), oven, sink, book, vase… curated short list | Default geometry |
| **unknown / no box** | no detection overlapping the clearance blob | Geometry-only |

**Not in v1:** walls/floor from SegFormer (offline-only), open-vocab, animals as special cases.

Knobs (YAML / CLI, all under a feature flag):

```text
obstacle_class_brake: off          # on | off
obstacle_class_delta_person: 0.25  # metres added to start when P0
obstacle_class_delta_furniture: 0.0
obstacle_class_min_iou: 0.15       # box↔corridor-column overlap to claim the blob
obstacle_class_min_conf: 0.35
obstacle_class_every_n: 2          # detect decimation (loop budget)
```

---

## 4. Architecture

```
cam-spin / control tick
  ├─ depth → _corridor_clearance() → clr (aged median)     [unchanged]
  ├─ YOLO multi-class (decimated) → boxes+cls               [new, side of detect]
  └─ _obstacle_vx_cap(target_range)
        cap_g, clr = geometry grade(clr)                    [unchanged math]
        if class_brake off: return cap_g, clr
        cls = class of detection best-overlapping nearest corridor support
        if cls is followed-person → treat as operator exclusion (A7)
        start_eff = start + delta(cls)                      # delta ≥ 0 only
        cap_c = re-grade(clr, start_eff, stop)
        return min(cap_g, cap_c) or whichever is tighter, clr
```

**Detector change:** extend `PersonDetector` (or add `ObstacleDetector` wrapper) to
optionally return multi-class boxes without breaking person-follow (follow path
keeps `classes=[PERSON_CLS]` **or** filters persons out of the multi-class list
for tracking). Prefer **one** YOLO forward pass when class-brake is on: predict
without class filter, then split person boxes → follow / other classes → brake.

**Loop budget:** class path must be measurable (`/diag/class_brake_ms`). If p99
blows the follow budget, auto-shed class-brake (log `CLASS-BRAKE-DISABLED-SLOW`)
like Rerun never-shed policy — geometry brake stays.

---

## 5. Observability

Rerun (when `--rerun`):

- `/reflex/clearance_m`, `/reflex/vx_cap` (existing)
- `/reflex/class_id`, `/reflex/class_delta_m`, `/reflex/vx_cap_geom` vs `/reflex/vx_cap`

Logs (throttled): `CLASS-BRAKE person delta=+0.25 clr=1.05 cap=0.08` / `CLASS-BRAKE shed`.

Offline: Batch-Label already emits `label_validation` + obstacles; add a dry-run
script later `eval/class_brake_replay.py` that scores “would tighten” frames on
`.rrd` without actuating.

---

## 6. Implementation slices

| Slice | Work | Proof |
|---|---|---|
| **A0** | This doc + update `OBSTACLE_LABELING_PLAN` “never steer” note | Done |
| **A1** | Multi-class detect + CLI/YAML (`obstacle_class_*`) | Done |
| **A2** | `_obstacle_vx_cap` tighten-only + Rerun scalars | Done |
| **A3** | Operator exclusion + tighten contracts | `eval/class_brake_selftest.py` |
| **A4** | Loop-cost shed + field checklist | Open (on-robot) |
| **A5** | K1Finder “Class brake” (default unticked) | Done |

**Enable on the robot only after** Batch-Label tally on real runs shows acceptable pairing PASS rate and low furniture `suspect_iddrift` (readiness gate below). Code may ship default-off before that.

---

## 7. Readiness gate (from laptop runs)

After `Batch-Label-Runs.ps1 -Newest 10` (or larger):

| Signal | Ship A2 only if… |
|---|---|
| `label_validation` PASS rate | Majority PASS; investigate FAIL/INC before relying on class |
| `depth_pairing.frac_paired` | Clean runs ≥ ~0.88 (gate floor 0.85) |
| `suspect_iddrift` | Rare; if common, keep deltas at 0 for furniture until Phase-2 filter is trusted live |
| Human scrub | Spot-check PASS `labeled.rrd`: person/chair false positives in corridor |

Until then: A0–A1 only (plumbing + log-only).

---

## 8. Explicit non-goals

- Class-aware **gap steer** (Plan B)
- SegFormer / wall semantics in the 10 Hz loop
- Raising clearance or skipping geometry brake because “class says free”
- Replacing depth corridor with box range alone
- Untethered / deadman changes

---

## 9. Suggested first commit sequence (when greenlit)

1. Docs: this file + small OBSTACLE_LABELING_PLAN amendment.
2. `PersonDetector` multi-class optional API + CLI/YAML flag (log-only consumer).
3. `_obstacle_vx_cap` tighten + selftests + Rerun.
4. K1Finder checkbox.
5. Field night: class-brake ON, deliberate chair + bystander in corridor, scrub Rerun.
